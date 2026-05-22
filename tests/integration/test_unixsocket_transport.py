"""Integration tests for ``UnixSocketTransport`` + ``UnixSocketServer``.

Lives under ``tests/integration/`` because the transport binds a real AF_UNIX
socket on disk; no docker/network is required so these tests run in the
default selection set.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest

from gg_relay.session.transport.protocol import (
    EventFrame,
    ToolDecisionFrame,
    ToolRequestFrame,
    TransportClosed,
)
from gg_relay.session.transport.unixsocket import (
    UnixSocketServer,
    UnixSocketTransport,
)


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _tool_req(seq: int, req_id: str = "r-1") -> ToolRequestFrame:
    return cast(
        ToolRequestFrame,
        {
            "v": 1,
            "type": "tool.request",
            "seq": seq,
            "ts": _now_iso(),
            "req_id": req_id,
            "tool": "Bash",
            "args": {"command": "echo hi"},
        },
    )


def _tool_decision(seq: int, req_id: str = "r-1") -> ToolDecisionFrame:
    return cast(
        ToolDecisionFrame,
        {
            "v": 1,
            "type": "tool.decision",
            "seq": seq,
            "ts": _now_iso(),
            "req_id": req_id,
            "decision": "accept",
        },
    )


@pytest.fixture
async def server_and_paths(tmp_path: Path) -> AsyncIterator[tuple[UnixSocketServer, Path]]:
    sock_path = tmp_path / "test.sock"
    server = await UnixSocketServer.listen(sock_path)
    try:
        yield server, sock_path
    finally:
        await server.close()


async def test_listen_creates_socket_file_with_0o666(server_and_paths):
    _server, sock_path = server_and_paths
    assert sock_path.exists()
    mode = stat.S_IMODE(sock_path.stat().st_mode)
    # We chmod 0o666 explicitly so non-root container UIDs can connect (D3.5).
    assert mode == 0o666, f"expected 0o666, got 0o{mode:o}"
    assert stat.S_ISSOCK(sock_path.stat().st_mode)


async def test_connect_after_listen_single_frame_round_trip(server_and_paths):
    server, sock_path = server_and_paths

    async def client():
        return await UnixSocketTransport.connect(sock_path, retry_timeout=2.0)

    client_task = asyncio.create_task(client())
    server_side = await server.accept(timeout=5.0)
    client_side = await client_task

    frame = _tool_req(1)
    await client_side.send(frame)
    received = await server_side.recv()
    assert received["type"] == "tool.request"
    assert received["seq"] == 1
    assert received["req_id"] == "r-1"

    await client_side.close()
    await server_side.close()


async def test_round_trip_event_and_control_frames(server_and_paths):
    server, sock_path = server_and_paths

    client_task = asyncio.create_task(
        UnixSocketTransport.connect(sock_path, retry_timeout=2.0)
    )
    server_side = await server.accept(timeout=5.0)
    client_side = await client_task

    # Runner → Host event frame
    await client_side.send(_tool_req(1, "r-A"))
    got_event = await server_side.recv()
    assert got_event["req_id"] == "r-A"

    # Host → Runner control frame
    await server_side.send(_tool_decision(2, "r-A"))
    got_control = await client_side.recv()
    assert got_control["type"] == "tool.decision"
    assert got_control["decision"] == "accept"

    await client_side.close()
    await server_side.close()


async def test_drain_after_peer_close(server_and_paths):
    """After writer sends N frames + closes, reader must still drain all N
    before raising TransportClosed (standard socket EOF semantics)."""
    server, sock_path = server_and_paths

    client_task = asyncio.create_task(
        UnixSocketTransport.connect(sock_path, retry_timeout=2.0)
    )
    server_side = await server.accept(timeout=5.0)
    client_side = await client_task

    N = 5
    for i in range(1, N + 1):
        await client_side.send(_tool_req(i, f"r-{i}"))
    await client_side.close()

    drained: list[EventFrame] = []
    for _ in range(N):
        frame = await server_side.recv()
        drained.append(frame)
    assert [f["seq"] for f in drained] == list(range(1, N + 1))

    # Now reader must see EOF.
    with pytest.raises(TransportClosed):
        await server_side.recv()

    await server_side.close()


async def test_recv_blocks_until_send(server_and_paths):
    server, sock_path = server_and_paths
    client_task = asyncio.create_task(
        UnixSocketTransport.connect(sock_path, retry_timeout=2.0)
    )
    server_side = await server.accept(timeout=5.0)
    client_side = await client_task

    # recv() with no pending frame must time out.
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(server_side.recv(), timeout=0.2)

    # After a real send, the next recv returns immediately.
    await client_side.send(_tool_req(1))
    frame = await asyncio.wait_for(server_side.recv(), timeout=1.0)
    assert frame["seq"] == 1

    await client_side.close()
    await server_side.close()


async def test_malformed_json_raises_transport_closed(tmp_path: Path):
    """If the peer sends a non-JSON line, we must NOT crash silently; raise
    TransportClosed so the calling bridge can tear down deterministically."""
    sock_path = tmp_path / "test.sock"
    server = await UnixSocketServer.listen(sock_path)
    try:
        # Hand-craft a raw client that writes garbage.
        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        server_side = await server.accept(timeout=5.0)
        writer.write(b"this is not json\n")
        await writer.drain()
        with pytest.raises(TransportClosed):
            await server_side.recv()
        writer.close()
        await writer.wait_closed()
        del reader
        await server_side.close()
    finally:
        await server.close()


async def test_large_frame_round_trip(server_and_paths):
    """1 MiB payload must survive the round trip without truncation."""
    server, sock_path = server_and_paths

    client_task = asyncio.create_task(
        UnixSocketTransport.connect(sock_path, retry_timeout=2.0)
    )
    server_side = await server.accept(timeout=5.0)
    client_side = await client_task

    big = "x" * (1024 * 1024)
    frame: ToolRequestFrame = cast(
        ToolRequestFrame,
        {
            "v": 1,
            "type": "tool.request",
            "seq": 1,
            "ts": _now_iso(),
            "req_id": "r-big",
            "tool": "Bash",
            "args": {"payload": big},
        },
    )
    await client_side.send(frame)
    got = await asyncio.wait_for(server_side.recv(), timeout=5.0)
    assert got["args"]["payload"] == big  # type: ignore[typeddict-item]
    assert len(json.dumps(got)) > 1024 * 1024

    await client_side.close()
    await server_side.close()


async def test_connect_retries_until_server_listens(tmp_path: Path):
    """connect() must retry while the socket file doesn't exist yet."""
    sock_path = tmp_path / "late.sock"
    assert not sock_path.exists()

    async def listen_late() -> UnixSocketServer:
        await asyncio.sleep(0.4)
        return await UnixSocketServer.listen(sock_path)

    listen_task = asyncio.create_task(listen_late())
    # If retry_timeout is honoured, this should succeed because listen_late
    # creates the socket well within 2 s.
    client_side = await UnixSocketTransport.connect(sock_path, retry_timeout=2.0)
    server = await listen_task
    server_side = await server.accept(timeout=2.0)

    await client_side.send(_tool_req(1))
    got = await server_side.recv()
    assert got["seq"] == 1

    await client_side.close()
    await server_side.close()
    await server.close()


async def test_connect_raises_when_retry_timeout_elapses(tmp_path: Path):
    """If the server never listens, connect() must raise ConnectionError after
    retry_timeout."""
    sock_path = tmp_path / "never.sock"
    with pytest.raises(ConnectionError):
        await UnixSocketTransport.connect(sock_path, retry_timeout=0.3)


async def test_send_after_close_raises_transport_closed(server_and_paths):
    server, sock_path = server_and_paths
    client_task = asyncio.create_task(
        UnixSocketTransport.connect(sock_path, retry_timeout=2.0)
    )
    server_side = await server.accept(timeout=5.0)
    client_side = await client_task

    await client_side.close()
    assert client_side.is_alive is False
    with pytest.raises(TransportClosed):
        await client_side.send(_tool_req(1))
    await server_side.close()


async def test_server_close_unlinks_socket_file(tmp_path: Path):
    sock_path = tmp_path / "unlinked.sock"
    server = await UnixSocketServer.listen(sock_path)
    assert sock_path.exists()
    await server.close()
    assert not sock_path.exists()
    # Double-close is a no-op (idempotent).
    await server.close()


async def test_listen_replaces_stale_socket_file(tmp_path: Path):
    """If the path already exists as a stale socket (previous crashed run),
    listen() should unlink and re-bind."""
    sock_path = tmp_path / "stale.sock"
    sock_path.touch()
    assert sock_path.exists()
    server = await UnixSocketServer.listen(sock_path)
    try:
        assert sock_path.exists()
        # The file should now be a socket, not a regular file.
        assert stat.S_ISSOCK(os.stat(sock_path).st_mode)
    finally:
        await server.close()
