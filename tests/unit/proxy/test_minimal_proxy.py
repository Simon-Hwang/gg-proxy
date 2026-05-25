"""Unit tests for the minimal forward proxy + audit log.

We stand up a real upstream server on 127.0.0.1 (so allow-listing works on
``127.0.0.1`` for tests) and exercise CONNECT + plain-HTTP code paths
without going to api.anthropic.com.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import pytest

from gg_relay.proxy.audit import AuditLog
from gg_relay.proxy.server import MinimalProxy

# ── small helpers ───────────────────────────────────────────────────────────


async def _start_dummy_upstream() -> tuple[asyncio.AbstractServer, int, list[bytes]]:
    """A TCP server that records the first chunk it receives and answers
    ``HTTP/1.1 200 OK ...``. Returns (server, port, received_chunks)."""
    received: list[bytes] = []

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        with contextlib.suppress(Exception):
            chunk = await asyncio.wait_for(reader.read(4096), timeout=2.0)
            received.append(chunk)
        body = b"hello"
        resp = (
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        writer.write(resp)
        with contextlib.suppress(Exception):
            await writer.drain()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    server = await asyncio.start_server(handle, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    return server, port, received


async def _proxy_with(
    audit: AuditLog,
    *,
    allowed: list[str] | None = None,
) -> MinimalProxy:
    proxy = MinimalProxy(
        audit=audit,
        allowed_hosts=allowed if allowed is not None else ["127.0.0.1"],
        host="127.0.0.1",
        port=0,
    )
    await proxy.start()
    return proxy


async def _send_connect(
    proxy_port: int,
    target: str,
    *,
    session_id: str | None = "sess-1",
    after_connect_body: bytes = b"",
) -> tuple[bytes, asyncio.StreamWriter]:
    """Open a connection to the proxy, send a CONNECT request, return
    (response_status_line, writer-still-open)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    req = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
    if session_id is not None:
        req += f"X-Relay-Session-Id: {session_id}\r\n"
    req += "\r\n"
    writer.write(req.encode())
    await writer.drain()
    status = await reader.readuntil(b"\r\n")
    # Drain the headers block (don't need them for status assertions).
    while True:
        line = await reader.readuntil(b"\r\n")
        if line in (b"\r\n", b""):
            break
    if after_connect_body:
        writer.write(after_connect_body)
        await writer.drain()
    return status, writer


async def _read_jsonl(p: Path) -> list[dict]:
    return [json.loads(line) for line in p.read_text().splitlines() if line]


# ── tests ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def upstream():
    server, port, received = await _start_dummy_upstream()
    try:
        yield port, received
    finally:
        server.close()
        await server.wait_closed()


@pytest.fixture
def audit_log(tmp_path: Path) -> AuditLog:
    return AuditLog(tmp_path / "audit.jsonl")


class TestAuditLog:
    async def test_allow_writes_event(self, audit_log: AuditLog):
        await audit_log.allow(session_id="s1", host="api.anthropic.com", port=443)
        events = await _read_jsonl(audit_log.path)
        assert len(events) == 1
        assert events[0]["event"] == "allow"
        assert events[0]["host"] == "api.anthropic.com"
        assert events[0]["session_id"] == "s1"
        assert events[0]["port"] == 443
        assert events[0]["ts"].endswith("Z")

    async def test_deny_writes_event_with_reason(self, audit_log: AuditLog):
        await audit_log.deny(
            session_id="s2", host="evil.example.com", reason="host_not_in_whitelist"
        )
        events = await _read_jsonl(audit_log.path)
        assert events[0]["event"] == "deny"
        assert events[0]["reason"] == "host_not_in_whitelist"


class TestConnectAllowedHost:
    async def test_connect_allowed_host_succeeds_and_tunnels_bytes(
        self, audit_log: AuditLog, upstream
    ):
        port, received = upstream
        proxy = await _proxy_with(audit_log)
        try:
            status, writer = await _send_connect(
                proxy.port,
                f"127.0.0.1:{port}",
                after_connect_body=b"GET / HTTP/1.1\r\nHost: x\r\n\r\n",
            )
            assert status.startswith(b"HTTP/1.1 200")
            # Read the tunnelled body back.
            reader = writer.get_extra_info("socket")  # ensures writer alive
            del reader
            # Wait for the upstream to actually observe the tunnelled
            # bytes before we tear anything down. The proxy spawns a
            # background pump task to forward client→upstream and
            # ``asyncio.Server.close()`` does NOT await in-flight
            # connection handlers (only the listening socket), so
            # without this poll the assertion races with the pump on
            # py3.11 — py3.12's slightly different event-loop close
            # ordering happens to win the race more often, which is
            # how this latent test bug stayed hidden.
            loop = asyncio.get_event_loop()
            deadline = loop.time() + 2.0
            while not received and loop.time() < deadline:
                await asyncio.sleep(0.01)
            writer.close()
            await writer.wait_closed()
        finally:
            await proxy.close()
        # The upstream MUST have observed the bytes we sent post-CONNECT.
        assert received and b"GET / HTTP/1.1" in received[0]
        events = await _read_jsonl(audit_log.path)
        assert events[0]["event"] == "allow"
        assert events[0]["host"] == "127.0.0.1"


class TestConnectBlockedHost:
    async def test_returns_403_for_disallowed_host(
        self, audit_log: AuditLog
    ):
        proxy = await _proxy_with(audit_log, allowed=["api.anthropic.com"])
        try:
            status, writer = await _send_connect(
                proxy.port, "evil.example.com:443", session_id="sess-bad"
            )
            assert status.startswith(b"HTTP/1.1 403")
            writer.close()
            await writer.wait_closed()
        finally:
            await proxy.close()
        events = await _read_jsonl(audit_log.path)
        assert events == [
            {
                "event": "deny",
                "session_id": "sess-bad",
                "host": "evil.example.com",
                "port": 443,
                "reason": "host_not_in_whitelist",
                "ts": events[0]["ts"],
            }
        ]


class TestSessionHeaderMissing:
    async def test_missing_session_id_header_logged_as_unknown(
        self, audit_log: AuditLog, upstream
    ):
        port, _received = upstream
        proxy = await _proxy_with(audit_log)
        try:
            status, writer = await _send_connect(
                proxy.port, f"127.0.0.1:{port}", session_id=None
            )
            assert status.startswith(b"HTTP/1.1 200")
            writer.close()
            await writer.wait_closed()
        finally:
            await proxy.close()
        events = await _read_jsonl(audit_log.path)
        assert events[0]["session_id"] == "unknown"


class TestUpstreamUnreachable:
    async def test_returns_502_when_upstream_refuses(self, audit_log: AuditLog):
        # Pick a port nothing is listening on (0 → bind+close to grab + release).
        s = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        dead_port = s.sockets[0].getsockname()[1]
        s.close()
        await s.wait_closed()

        proxy = await _proxy_with(audit_log)
        try:
            status, writer = await _send_connect(
                proxy.port, f"127.0.0.1:{dead_port}"
            )
            assert status.startswith(b"HTTP/1.1 502")
            writer.close()
            await writer.wait_closed()
        finally:
            await proxy.close()
        events = await _read_jsonl(audit_log.path)
        assert events[0]["event"] == "deny"
        assert events[0]["reason"].startswith("upstream_unreachable:")


class TestPlainHttpDispatch:
    async def test_plain_http_blocked_host_returns_403(
        self, audit_log: AuditLog
    ):
        proxy = await _proxy_with(audit_log, allowed=["api.anthropic.com"])
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", proxy.port
            )
            writer.write(
                b"GET http://evil.example.com/ HTTP/1.1\r\n"
                b"Host: evil.example.com\r\n"
                b"X-Relay-Session-Id: s-plain\r\n\r\n"
            )
            await writer.drain()
            status = await reader.readuntil(b"\r\n")
            assert status.startswith(b"HTTP/1.1 403")
            writer.close()
            await writer.wait_closed()
        finally:
            await proxy.close()
        events = await _read_jsonl(audit_log.path)
        assert events[0]["host"] == "evil.example.com"
        assert events[0]["reason"] == "host_not_in_whitelist"


class TestProxyLifecycle:
    async def test_async_context_manager_starts_and_stops(
        self, audit_log: AuditLog
    ):
        async with MinimalProxy(
            audit=audit_log,
            allowed_hosts=["api.anthropic.com"],
            host="127.0.0.1",
            port=0,
        ) as proxy:
            assert proxy.port > 0
        # After the async-with exits, the proxy server is closed.

    async def test_double_close_is_idempotent(self, audit_log: AuditLog):
        proxy = await _proxy_with(audit_log)
        await proxy.close()
        await proxy.close()
