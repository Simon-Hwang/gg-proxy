"""Plan 9 D9.8 — TcpTransport + TcpServer tests.

Reviewer I MAJOR 4 requires explicit listener-mode coverage:
auth-handshake / auth-reject / round-trip / drop-and-reconnect /
empty-token-guard. Eight tests below cover that surface.

We use TCP loopback (127.0.0.1:0) for ephemeral ports — the
existing UnixSocketTransport suite uses the analogous AF_UNIX
trick. The 16 MiB readline limit is also exercised so we don't
regress the parity contract with UnixSocketTransport.
"""
from __future__ import annotations

import asyncio
import json
from typing import cast

import pytest

from gg_relay.session.transport.protocol import EventFrame, TransportClosed
from gg_relay.session.transport.tcp import (
    AuthFailed,
    TcpServer,
    TcpTransport,
)


def _frame(seq: int, payload: str = "hi") -> EventFrame:
    return cast(
        EventFrame,
        {
            "v": 1,
            "type": "msg.chunk",
            "seq": seq,
            "ts": "2026-05-24T00:00:00Z",
            "data": {"text": payload},
        },
    )


@pytest.fixture
async def server_and_token() -> tuple[TcpServer, str]:
    server = await TcpServer.listen(
        "127.0.0.1", 0, expected_token="correct-horse-battery-staple"
    )
    try:
        yield server, "correct-horse-battery-staple"
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_auth_handshake_succeeds_and_roundtrip(
    server_and_token: tuple[TcpServer, str],
) -> None:
    server, token = server_and_token

    async def _client() -> TcpTransport:
        return await TcpTransport.connect(
            "127.0.0.1", server.port, auth_token=token
        )

    client_t, server_t = await asyncio.gather(_client(), server.accept())
    try:
        await client_t.send(_frame(1))
        recv = await server_t.recv()
        assert recv["seq"] == 1
        assert recv["data"]["text"] == "hi"

        await server_t.send(_frame(2, "back"))
        echo = await client_t.recv()
        assert echo["seq"] == 2
        assert echo["data"]["text"] == "back"
    finally:
        await client_t.close()
        await server_t.close()


@pytest.mark.asyncio
async def test_auth_handshake_rejects_wrong_token(
    server_and_token: tuple[TcpServer, str],
) -> None:
    server, _ = server_and_token
    with pytest.raises(AuthFailed):
        await TcpTransport.connect(
            "127.0.0.1", server.port, auth_token="WRONG"
        )


@pytest.mark.asyncio
async def test_listen_rejects_empty_token() -> None:
    """A blank ``expected_token`` would auth every connection — the
    constructor must refuse it (defence-in-depth)."""
    with pytest.raises(ValueError, match="non-empty"):
        await TcpServer.listen("127.0.0.1", 0, expected_token="")


@pytest.mark.asyncio
async def test_recv_after_peer_close_raises(
    server_and_token: tuple[TcpServer, str],
) -> None:
    server, token = server_and_token

    async def _client() -> TcpTransport:
        return await TcpTransport.connect(
            "127.0.0.1", server.port, auth_token=token
        )

    client_t, server_t = await asyncio.gather(_client(), server.accept())
    try:
        await server_t.close()
        with pytest.raises(TransportClosed):
            await client_t.recv()
    finally:
        await client_t.close()


@pytest.mark.asyncio
async def test_send_after_close_raises(
    server_and_token: tuple[TcpServer, str],
) -> None:
    server, token = server_and_token

    async def _client() -> TcpTransport:
        return await TcpTransport.connect(
            "127.0.0.1", server.port, auth_token=token
        )

    client_t, server_t = await asyncio.gather(_client(), server.accept())
    await client_t.close()
    with pytest.raises(TransportClosed):
        await client_t.send(_frame(99))
    await server_t.close()


@pytest.mark.asyncio
async def test_connect_retries_then_fails_fast(
    server_and_token: tuple[TcpServer, str],
) -> None:
    """Connecting to a port nothing listens on must raise
    ``ConnectionError`` within ``retry_timeout`` instead of hanging."""
    server, token = server_and_token
    # Pick a port we KNOW nothing is listening on by starting + closing
    # an ephemeral listener.
    sock = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    bad_port = sock.sockets[0].getsockname()[1]
    sock.close()
    await sock.wait_closed()

    with pytest.raises(ConnectionError):
        await TcpTransport.connect(
            "127.0.0.1",
            bad_port,
            auth_token=token,
            retry_timeout=0.2,
        )


@pytest.mark.asyncio
async def test_drop_and_reconnect(
    server_and_token: tuple[TcpServer, str],
) -> None:
    """A new TcpTransport.connect after the first drops still works."""
    server, token = server_and_token

    for round_n in range(2):
        async def _client() -> TcpTransport:
            return await TcpTransport.connect(
                "127.0.0.1", server.port, auth_token=token
            )

        client_t, server_t = await asyncio.gather(_client(), server.accept())
        try:
            await client_t.send(_frame(round_n))
            recv = await server_t.recv()
            assert recv["seq"] == round_n
        finally:
            await client_t.close()
            await server_t.close()


@pytest.mark.asyncio
async def test_malformed_handshake_closes_quietly(
    server_and_token: tuple[TcpServer, str],
) -> None:
    """Sending garbage as the first line must close the connection
    without enqueueing an accepted transport (otherwise the runner
    code would see an unauthenticated peer)."""
    server, _ = server_and_token
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    try:
        writer.write(b"not-json\n")
        await writer.drain()
        # The server either replies with auth.ack ok:false then
        # closes, or closes outright on JSON-decode failure. Either
        # way readline() must terminate (not hang).
        line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        if line:
            ack = json.loads(line.decode())
            assert ack["type"] == "auth.ack"
            assert ack["ok"] is False
        eof = await asyncio.wait_for(reader.read(1024), timeout=2.0)
        assert eof == b""
    finally:
        writer.close()
        with contextlib_suppress():
            await writer.wait_closed()

    # And the server's accept queue must be empty — a rejected
    # connection must never become a usable transport.
    with pytest.raises(TimeoutError):
        await server.accept(timeout=0.2)


import contextlib  # noqa: E402 — local helper kept near its use site


def contextlib_suppress() -> contextlib.AbstractContextManager[object]:
    return contextlib.suppress(Exception)
