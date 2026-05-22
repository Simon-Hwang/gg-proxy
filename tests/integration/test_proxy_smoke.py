"""Real-curl smoke test for the minimal forward proxy.

Skipped unless ``curl`` is on ``PATH`` (``@pytest.mark.requires_curl``).
Runs the proxy on a random local port, points ``curl`` at a local dummy
upstream allowlisted under ``127.0.0.1``, and asserts the proxied
response body comes through. Does NOT hit api.anthropic.com — that
requires network egress and is the manual-only path.
"""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from gg_relay.proxy.audit import AuditLog
from gg_relay.proxy.server import MinimalProxy

pytestmark = pytest.mark.requires_curl


@pytest.fixture(autouse=True)
def _skip_if_no_curl() -> None:
    if shutil.which("curl") is None:
        pytest.skip("curl not available on PATH")


async def _spin_upstream() -> tuple[asyncio.AbstractServer, int]:
    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await reader.read(4096)
        body = b"ok"
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, host="127.0.0.1", port=0)
    return server, server.sockets[0].getsockname()[1]


async def test_curl_plain_http_through_proxy_returns_body(tmp_path: Path):
    """curl -x http://127.0.0.1:<proxy> http://127.0.0.1:<upstream>/
    should print 'ok' and the audit log should record one allow event."""
    audit = AuditLog(tmp_path / "audit.jsonl")
    upstream, upstream_port = await _spin_upstream()
    try:
        async with MinimalProxy(
            audit=audit,
            allowed_hosts=["127.0.0.1"],
            host="127.0.0.1",
            port=0,
        ) as proxy:
            proc = await asyncio.create_subprocess_exec(
                "curl",
                "--silent",
                "--show-error",
                "--max-time", "5",
                "-x", f"http://127.0.0.1:{proxy.port}",
                "-H", "X-Relay-Session-Id: curl-test",
                f"http://127.0.0.1:{upstream_port}/",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=10
            )
        assert proc.returncode == 0, stderr.decode("utf-8", errors="replace")
        assert stdout == b"ok", stdout
        events = [
            json.loads(line)
            for line in (tmp_path / "audit.jsonl").read_text().splitlines()
            if line
        ]
        assert any(
            e["event"] == "allow" and e["session_id"] == "curl-test"
            for e in events
        )
    finally:
        upstream.close()
        await upstream.wait_closed()
