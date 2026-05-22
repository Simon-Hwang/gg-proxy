"""UnixSocketTransport — NDJSON over AF_UNIX SOCK_STREAM.

Bidirectional, drain-then-close semantics (spec §6.4). One transport pair is
established by:

    server = await UnixSocketServer.listen(path)
    # ... pass `path` to the container via env
    server_side = await server.accept()
    # in the container:
    client_side = await UnixSocketTransport.connect(path)

The transport implements ``SessionTransport`` Protocol; both sides agree on the
NDJSON frame format defined in ``transport/protocol.py``.

Notes:
- Socket file is created with mode 0o666 so non-root container UIDs can connect
  (D3.5). Plan 3 README documents SELinux ``:z`` mount mode.
- ``recv()`` drains buffered frames before raising ``TransportClosed`` on EOF —
  matches ``InMemoryTransport`` semantics so bridge code is symmetric across
  backends.
- The wire is line-oriented JSON. We use ``StreamReader.readline()`` directly;
  the asyncio default ``_limit`` is 64 KiB but we extend it to 16 MiB so 1 MiB
  payloads (and the rare oversize one) survive without raising
  ``LimitOverrunError``.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import cast

from gg_relay.session.transport.protocol import (
    ControlFrame,
    EventFrame,
    TransportClosed,
)

_READER_LIMIT = 16 * 1024 * 1024  # 16 MiB — well above the 1 MiB payload test.


class UnixSocketTransport:
    """One bidirectional NDJSON stream over an AF_UNIX SOCK_STREAM connection.

    Construction is via ``connect()`` (client side) or by the
    ``UnixSocketServer._on_accept`` callback (server side); callers do not
    instantiate ``UnixSocketTransport`` directly.
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
        cls, path: Path, *, retry_timeout: float = 10.0
    ) -> UnixSocketTransport:
        """Open a connection to ``path``, retrying while it doesn't exist yet.

        The retry loop tolerates two race windows that always exist with AF_UNIX:
          - ``FileNotFoundError`` when the server hasn't bound yet
          - ``ConnectionRefusedError`` on some kernels right after bind but
            before listen+accept is ready

        Raises ``ConnectionError`` if ``retry_timeout`` elapses before the
        first successful connection.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + retry_timeout
        last_err: BaseException | None = None
        while loop.time() < deadline:
            try:
                reader, writer = await asyncio.open_unix_connection(
                    str(path), limit=_READER_LIMIT
                )
                return cls(reader, writer)
            except (FileNotFoundError, ConnectionRefusedError) as e:
                last_err = e
                await asyncio.sleep(0.05)
        raise ConnectionError(
            f"could not connect to {path} within {retry_timeout}s"
        ) from last_err

    @property
    def is_alive(self) -> bool:
        return not self._closing

    async def send(self, frame: ControlFrame | EventFrame) -> None:
        if self._closing:
            raise TransportClosed("transport is closing")
        # ``default=str`` keeps Path / datetime objects safe — the in-memory
        # transport accepted them without coercion so we mirror that surface.
        data = json.dumps(frame, separators=(",", ":"), default=str).encode() + b"\n"
        self._w.write(data)
        try:
            await self._w.drain()
        except (ConnectionResetError, BrokenPipeError) as e:
            self._closing = True
            raise TransportClosed("peer closed during send") from e

    async def recv(self) -> EventFrame:
        """Read one NDJSON frame.

        Drains buffered frames before raising ``TransportClosed`` on EOF (peer
        close). A malformed line (non-JSON) is treated as a protocol violation
        and surfaces as ``TransportClosed`` so the calling bridge tears down.
        """
        try:
            line = await self._r.readline()
        except (ConnectionResetError, BrokenPipeError) as e:
            self._closing = True
            raise TransportClosed("peer closed during recv") from e
        if not line:
            # Standard EOF — peer closed cleanly.
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
        # Idempotent close — never re-raise on the teardown path.
        with contextlib.suppress(Exception):
            self._w.close()
            await self._w.wait_closed()


class UnixSocketServer:
    """Host-side AF_UNIX listener.

    Lifecycle:
        server = await UnixSocketServer.listen(path)
        transport = await server.accept(timeout=...)
        ...
        await server.close()  # closes listener + unlinks the socket file
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._server: asyncio.AbstractServer | None = None
        self._accepted: asyncio.Queue[UnixSocketTransport] = asyncio.Queue()
        # Track every accepted transport so close() can tear them down. In
        # Python 3.12+ AbstractServer.wait_closed() blocks until every active
        # connection's task finishes; if the caller forgot to close one we
        # would hang. Doing the cleanup here keeps tests deterministic.
        self._accepted_set: set[UnixSocketTransport] = set()
        self._closed = False

    @classmethod
    async def listen(cls, path: Path) -> UnixSocketServer:
        """Create the parent directory, unlink any stale file, bind+listen."""
        path.parent.mkdir(parents=True, exist_ok=True)
        # Stale file from a previous (possibly crashed) process must go;
        # otherwise bind() with EADDRINUSE.
        if path.exists() or path.is_symlink():
            path.unlink()
        self = cls(path)
        self._server = await asyncio.start_unix_server(
            self._on_accept, path=str(path), limit=_READER_LIMIT
        )
        # 0o666: container runs as non-root (UID 1000 in our base image) and
        # must still be able to connect to a socket bind()-ed by the host
        # gg-relay process (often root). chmod after bind is the documented
        # AF_UNIX idiom. SELinux: mount the parent dir with `:z` (Plan 3 README).
        path.chmod(0o666)
        return self

    async def _on_accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        transport = UnixSocketTransport(reader, writer)
        self._accepted_set.add(transport)
        await self._accepted.put(transport)

    async def accept(self, *, timeout: float = 30.0) -> UnixSocketTransport:
        """Block until the next inbound connection is ready (or ``timeout``)."""
        return await asyncio.wait_for(self._accepted.get(), timeout=timeout)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Close every transport we've accepted. Required because Python 3.12+
        # ``AbstractServer.wait_closed()`` blocks until each active connection
        # task finishes; without this, a test that forgets to close one
        # accepted transport (or one whose recv raised TransportClosed first)
        # would deadlock the fixture teardown.
        for transport in list(self._accepted_set):
            with contextlib.suppress(Exception):
                await transport.close()
        self._accepted_set.clear()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(TimeoutError, Exception):
                await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()
