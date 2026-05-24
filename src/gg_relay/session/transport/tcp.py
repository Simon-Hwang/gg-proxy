"""TcpTransport — NDJSON over TCP with token handshake.

Plan 9 D9.8. Mirrors :mod:`gg_relay.session.transport.unixsocket` for the
K8s ``Job``-per-session executor: the runner container can't accept a
host-bound AF_UNIX socket across the Pod boundary, so the runner pod
listens on TCP and the host connects after the K8s Job watcher resolves
the Pod IP.

Wire format is byte-for-byte identical to UnixSocketTransport — line-
oriented JSON with ``StreamReader.readline()`` and ``_limit=16 MiB``.
That lets the runner-side wire bridge code (``session/client.py``)
treat both transports interchangeably.

Auth contract (BLOCKER B8 in Plan 9 Round 3):

1. Server (runner) listens on ``host:port``.
2. Client (host gg-relay) connects, sends ONE auth frame as the very
   first NDJSON line:

       {"v": 1, "type": "auth", "token": "<32-byte secret>"}

3. Server validates ``token`` against ``expected_token`` (sourced from
   ``RELAY_RUNNER_AUTH_TOKEN`` in the runner env, which itself comes
   from a per-Job K8s Secret).
4. Server replies with:

       {"v": 1, "type": "auth.ack", "ok": true}   # or ``ok: false``

5. From the next frame onwards the connection is symmetric NDJSON
   exactly like UnixSocketTransport. A failed auth closes the socket
   immediately; the client raises :class:`AuthFailed` and never
   gets a usable transport.

Why first-frame auth instead of TLS-client-cert: K8s Secret token is
much easier to provision per-Job (`ownerReferences` GC) and the
runner Pod is on a NetworkPolicy that already restricts ingress to
the relay's own ServiceAccount; mTLS would buy little extra security
against the in-cluster threat model for v0.9.0.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from typing import cast

from gg_relay.session.transport.protocol import (
    ControlFrame,
    EventFrame,
    TransportClosed,
)

_READER_LIMIT = 16 * 1024 * 1024  # 16 MiB — matches UnixSocketTransport.


class AuthFailed(Exception):
    """Raised on the client side when the server rejects the token
    handshake or the connection drops before ``auth.ack`` arrives."""


class TcpTransport:
    """One bidirectional NDJSON stream over a TCP connection.

    Construction is via :meth:`connect` (client / host side) or the
    :class:`TcpServer` ``_on_accept`` callback (server / runner side);
    callers never instantiate ``TcpTransport`` directly.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._r = reader
        self._w = writer
        self._closing = False

    @classmethod
    async def connect(
        cls,
        host: str,
        port: int,
        *,
        auth_token: str,
        retry_timeout: float = 10.0,
        handshake_timeout: float = 5.0,
    ) -> TcpTransport:
        """Open a TCP connection + perform the auth handshake.

        Retries while the runner pod hasn't bound the listener yet
        (``ConnectionRefusedError`` / ``OSError``). Raises
        :class:`ConnectionError` if ``retry_timeout`` elapses or
        :class:`AuthFailed` if the server rejects the token.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + retry_timeout
        last_err: BaseException | None = None
        reader: asyncio.StreamReader | None = None
        writer: asyncio.StreamWriter | None = None
        while loop.time() < deadline:
            try:
                reader, writer = await asyncio.open_connection(
                    host, port, limit=_READER_LIMIT
                )
                break
            except (ConnectionRefusedError, OSError) as e:
                last_err = e
                await asyncio.sleep(0.05)
        if reader is None or writer is None:
            raise ConnectionError(
                f"could not connect to {host}:{port} within {retry_timeout}s"
            ) from last_err

        auth_payload = (
            json.dumps(
                {"v": 1, "type": "auth", "token": auth_token},
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        writer.write(auth_payload)
        try:
            await asyncio.wait_for(writer.drain(), timeout=handshake_timeout)
            ack_line = await asyncio.wait_for(
                reader.readline(), timeout=handshake_timeout
            )
        except (TimeoutError, ConnectionError) as e:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            raise AuthFailed(f"handshake failed: {e}") from e
        if not ack_line:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            raise AuthFailed("server closed before sending auth.ack")
        try:
            ack = json.loads(ack_line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            raise AuthFailed(f"malformed auth.ack: {e}") from e
        if not (
            isinstance(ack, dict) and ack.get("type") == "auth.ack" and ack.get("ok")
        ):
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            raise AuthFailed(f"auth rejected: {ack}")

        return cls(reader, writer)

    @property
    def is_alive(self) -> bool:
        return not self._closing

    async def send(self, frame: ControlFrame | EventFrame) -> None:
        if self._closing:
            raise TransportClosed("transport is closing")
        data = (
            json.dumps(frame, separators=(",", ":"), default=str).encode() + b"\n"
        )
        self._w.write(data)
        try:
            await self._w.drain()
        except (ConnectionResetError, BrokenPipeError) as e:
            self._closing = True
            raise TransportClosed("peer closed during send") from e

    async def recv(self) -> EventFrame:
        """Read one NDJSON frame; raises ``TransportClosed`` on EOF
        or malformed JSON (same semantics as ``UnixSocketTransport``)."""
        try:
            line = await self._r.readline()
        except (ConnectionResetError, BrokenPipeError) as e:
            self._closing = True
            raise TransportClosed("peer closed during recv") from e
        if not line:
            self._closing = True
            raise TransportClosed("peer closed (EOF)")
        try:
            return cast(EventFrame, json.loads(line.decode("utf-8")))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self._closing = True
            raise TransportClosed(f"malformed frame: {e}") from e

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        with contextlib.suppress(Exception):
            self._w.close()
            await self._w.wait_closed()


class TcpServer:
    """Runner-side TCP listener with token-handshake gate.

    Lifecycle::

        server = await TcpServer.listen(
            "0.0.0.0", 9001, expected_token=os.environ["RELAY_RUNNER_AUTH_TOKEN"]
        )
        transport = await server.accept(timeout=30.0)
        ...
        await server.close()

    A connection that fails the handshake is closed BEFORE the
    accepted-queue is touched, so the calling runner never sees an
    un-authenticated transport.
    """

    def __init__(self, host: str, port: int, *, expected_token: str) -> None:
        self._host = host
        self._port = port
        self._expected_token = expected_token
        self._server: asyncio.AbstractServer | None = None
        self._accepted: asyncio.Queue[TcpTransport] = asyncio.Queue()
        self._accepted_set: set[TcpTransport] = set()
        self._closed = False

    @classmethod
    async def listen(
        cls, host: str, port: int, *, expected_token: str
    ) -> TcpServer:
        if not expected_token:
            raise ValueError("expected_token must be a non-empty string")
        self = cls(host, port, expected_token=expected_token)
        self._server = await asyncio.start_server(
            self._on_accept, host=host, port=port, limit=_READER_LIMIT
        )
        return self

    @property
    def port(self) -> int:
        """Resolved listening port — useful when ``listen(..., port=0)``
        picks an ephemeral port (tests)."""
        if self._server is None:
            raise RuntimeError("server not started")
        sockets = getattr(self._server, "sockets", None)
        if not sockets:
            raise RuntimeError("server has no bound sockets")
        return int(sockets[0].getsockname()[1])

    async def _on_accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        except (TimeoutError, ConnectionError):
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        if not line:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        try:
            payload = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        token_ok = (
            isinstance(payload, dict)
            and payload.get("type") == "auth"
            and payload.get("token") == self._expected_token
        )
        ack = {"v": 1, "type": "auth.ack", "ok": bool(token_ok)}
        writer.write(json.dumps(ack, separators=(",", ":")).encode() + b"\n")
        try:
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        if not token_ok:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        transport = TcpTransport(reader, writer)
        self._accepted_set.add(transport)
        await self._accepted.put(transport)

    async def accept(self, *, timeout: float = 30.0) -> TcpTransport:
        return await asyncio.wait_for(self._accepted.get(), timeout=timeout)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for transport in list(self._accepted_set):
            with contextlib.suppress(Exception):
                await transport.close()
        self._accepted_set.clear()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(TimeoutError, Exception):
                await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
