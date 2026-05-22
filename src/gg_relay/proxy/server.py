"""MinimalProxy — strict allow-list HTTP/HTTPS forward proxy (raw asyncio).

Why raw asyncio instead of aiohttp.web (per Plan 3 §6 Task 12 plan):
``aiohttp.web`` does not natively support the HTTP ``CONNECT`` method
that clients use to tunnel TLS through a proxy. CONNECT requires us to
hand the client socket back to the caller for raw byte-pumping after the
``200 Connection Established`` line — a pattern aiohttp's request/response
model fights. ``asyncio.start_server`` gives us the StreamReader /
StreamWriter pair directly, so we can:

  1. parse one request line + headers
  2. validate the upstream host against ALLOWED_HOSTS
  3. on CONNECT: open an upstream socket, echo
     ``HTTP/1.1 200 Connection Established`` back, then bidirectionally
     pipe bytes until either side closes
  4. on plain HTTP method: same gate, then proxy the request straight
     through (one-shot, no keep-alive — claude CLI always uses HTTPS so
     this branch is defensive)

Decisions are recorded into an :class:`AuditLog` keyed by the
``X-Relay-Session-Id`` header (default ``"unknown"`` if the client
didn't set one). Plan 4 will surface these on the dashboard.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable
from typing import Final

from gg_relay.proxy.audit import AuditLog

logger = logging.getLogger("gg_relay.proxy")

ALLOWED_HOSTS_DEFAULT: Final[frozenset[str]] = frozenset({"api.anthropic.com"})
"""Plan 3 D3.13 — only Anthropic's API is reachable through this proxy.
Anything else returns 403 + an audit deny entry."""

DEFAULT_PROXY_PORT: Final[int] = 8888
"""Plan 3 spike default; documented in DockerExecutor.proxy_url examples."""

_BUF_SIZE = 64 * 1024
_HEADER_LIMIT = 8 * 1024
_CONNECT_READ_TIMEOUT = 10.0
_PIPE_GRACE = 0.1


class MinimalProxy:
    """Forward proxy with a frozen allow-list of upstream hosts.

    Construction does not start the server; call :meth:`start` (or use as
    an ``async`` context manager) to bind the listening port.
    """

    def __init__(
        self,
        *,
        audit: AuditLog,
        allowed_hosts: Iterable[str] = ALLOWED_HOSTS_DEFAULT,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PROXY_PORT,
    ) -> None:
        self._audit = audit
        self._allowed = frozenset(allowed_hosts)
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        """Resolved listening port. If constructed with ``port=0`` the actual
        port chosen by the OS is only known after :meth:`start`."""
        if self._server is None:
            return self._port
        sockets = self._server.sockets or ()
        if sockets:
            return int(sockets[0].getsockname()[1])
        return self._port

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return self._allowed

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._on_connection, host=self._host, port=self._port
        )
        logger.info(
            "MinimalProxy listening on %s:%d; allowed=%s",
            self._host,
            self.port,
            sorted(self._allowed),
        )

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
        self._server = None

    async def __aenter__(self) -> MinimalProxy:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ── connection handling ────────────────────────────────────────────

    async def _on_connection(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        try:
            await self._handle(client_reader, client_writer)
        except (ConnectionError, BrokenPipeError):
            logger.debug("proxy: client connection dropped", exc_info=True)
        except Exception:
            logger.exception("proxy: unhandled error in connection handler")
        finally:
            with contextlib.suppress(Exception):
                client_writer.close()
                await client_writer.wait_closed()

    async def _handle(
        self,
        cr: asyncio.StreamReader,
        cw: asyncio.StreamWriter,
    ) -> None:
        """Parse the first request line + headers, then dispatch."""
        request_line = await asyncio.wait_for(
            cr.readuntil(b"\r\n"), timeout=_CONNECT_READ_TIMEOUT
        )
        parts = request_line.rstrip(b"\r\n").split(b" ", 2)
        if len(parts) < 3:
            await self._write_status(cw, 400, "Bad Request")
            return
        method, target, _version = parts
        headers = await self._read_headers(cr)
        session_id = headers.get(b"x-relay-session-id", b"unknown").decode(
            errors="replace"
        ) or "unknown"

        if method == b"CONNECT":
            await self._handle_connect(cr, cw, target.decode(), session_id)
        else:
            await self._handle_plain(
                cr, cw, method, target, headers, session_id
            )

    async def _read_headers(
        self, cr: asyncio.StreamReader
    ) -> dict[bytes, bytes]:
        """Read until the blank-line header terminator."""
        headers: dict[bytes, bytes] = {}
        total = 0
        while True:
            line = await asyncio.wait_for(
                cr.readuntil(b"\r\n"), timeout=_CONNECT_READ_TIMEOUT
            )
            total += len(line)
            if total > _HEADER_LIMIT:
                raise ValueError("header section too large")
            if line in (b"\r\n", b""):
                return headers
            if b":" not in line:
                continue
            k, _, v = line.partition(b":")
            headers[k.strip().lower()] = v.strip().rstrip(b"\r\n")

    async def _handle_connect(
        self,
        cr: asyncio.StreamReader,
        cw: asyncio.StreamWriter,
        host_port: str,
        session_id: str,
    ) -> None:
        host, _, port_str = host_port.partition(":")
        try:
            port = int(port_str) if port_str else 443
        except ValueError:
            await self._write_status(cw, 400, "Bad Request")
            await self._audit.deny(
                session_id=session_id, host=host, reason="bad_port"
            )
            return

        if host not in self._allowed:
            await self._audit.deny(
                session_id=session_id,
                host=host,
                reason="host_not_in_whitelist",
                port=port,
            )
            await self._write_status(cw, 403, "Forbidden")
            return

        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=10.0
            )
        except (TimeoutError, OSError) as exc:
            logger.warning(
                "proxy: upstream connect failed: %s:%d (%s)", host, port, exc
            )
            await self._audit.deny(
                session_id=session_id,
                host=host,
                reason=f"upstream_unreachable:{exc.__class__.__name__}",
                port=port,
            )
            await self._write_status(cw, 502, "Bad Gateway")
            return

        await self._audit.allow(session_id=session_id, host=host, port=port)
        await self._write_status(cw, 200, "Connection Established")

        # Bidirectional pipe until either side closes.
        try:
            await asyncio.gather(
                self._pump(cr, upstream_writer),
                self._pump(upstream_reader, cw),
                return_exceptions=True,
            )
        finally:
            with contextlib.suppress(Exception):
                upstream_writer.close()
                await upstream_writer.wait_closed()

    async def _handle_plain(
        self,
        cr: asyncio.StreamReader,
        cw: asyncio.StreamWriter,
        method: bytes,
        target: bytes,
        headers: dict[bytes, bytes],
        session_id: str,
    ) -> None:
        """One-shot HTTP proxy for non-CONNECT requests. Mostly defensive —
        claude CLI uses HTTPS exclusively."""
        # Parse absolute URI target ``http://host:port/path``.
        if not target.startswith(b"http://"):
            await self._write_status(cw, 400, "Bad Request")
            return
        rest = target[len(b"http://"):]
        host_part, _, path_q = rest.partition(b"/")
        host_str, _, port_str = host_part.partition(b":")
        host = host_str.decode()
        port = int(port_str or b"80")

        if host not in self._allowed:
            await self._audit.deny(
                session_id=session_id,
                host=host,
                reason="host_not_in_whitelist",
                port=port,
            )
            await self._write_status(cw, 403, "Forbidden")
            return

        await self._audit.allow(session_id=session_id, host=host, port=port)

        try:
            ur, uw = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=10.0
            )
        except (TimeoutError, OSError):
            await self._write_status(cw, 502, "Bad Gateway")
            return

        # Reconstruct an origin-form request line and forward.
        new_path = b"/" + path_q if path_q else b"/"
        out = bytearray()
        out.extend(method)
        out.extend(b" ")
        out.extend(new_path)
        out.extend(b" HTTP/1.1\r\n")
        for k, v in headers.items():
            # Skip hop-by-hop headers the upstream shouldn't see.
            if k in (b"proxy-connection", b"connection"):
                continue
            out.extend(k)
            out.extend(b": ")
            out.extend(v)
            out.extend(b"\r\n")
        out.extend(b"\r\n")
        uw.write(out)
        try:
            await uw.drain()
            await asyncio.gather(
                self._pump(cr, uw),
                self._pump(ur, cw),
                return_exceptions=True,
            )
        finally:
            with contextlib.suppress(Exception):
                uw.close()
                await uw.wait_closed()

    @staticmethod
    async def _pump(
        src: asyncio.StreamReader, dst: asyncio.StreamWriter
    ) -> None:
        """Copy from src → dst until EOF or peer closes. Swallow expected
        connection-reset / broken-pipe so the sibling task gets its turn."""
        try:
            while True:
                chunk = await src.read(_BUF_SIZE)
                if not chunk:
                    return
                dst.write(chunk)
                try:
                    await dst.drain()
                except (ConnectionResetError, BrokenPipeError):
                    return
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            return
        finally:
            with contextlib.suppress(Exception):
                if dst.can_write_eof():
                    dst.write_eof()

    @staticmethod
    async def _write_status(
        cw: asyncio.StreamWriter, code: int, reason: str
    ) -> None:
        line = f"HTTP/1.1 {code} {reason}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
        cw.write(line.encode("ascii"))
        with contextlib.suppress(ConnectionResetError, BrokenPipeError):
            await cw.drain()
        # Give the kernel one event-loop tick to actually flush, then signal
        # EOF so the client sees a clean shutdown.
        await asyncio.sleep(_PIPE_GRACE)
