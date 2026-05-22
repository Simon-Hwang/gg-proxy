"""AuditLog — append-only JSONL writer for proxy allow/deny events.

One line per decision; rotated externally (logrotate / fluentbit). Keeping
the writer synchronous (single fsync per line) is safe because the proxy
fires at most a few decisions per second per session.
"""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class AuditLog:
    """Append-only JSON-lines audit log.

    Thread-safe (RLock around the file open + write) so multiple proxy
    workers in the same process can share a single AuditLog instance. The
    public ``allow()`` / ``deny()`` methods are ``async`` for caller
    ergonomics; under the hood the write is synchronous (millisecond-scale).
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def _write(self, event: dict[str, Any]) -> None:
        event["ts"] = _now_iso()
        line = json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n"
        with self._lock, self._path.open("a", encoding="utf-8") as f:
            f.write(line)

    async def allow(self, *, session_id: str, host: str, port: int) -> None:
        await asyncio.to_thread(
            self._write,
            {
                "event": "allow",
                "session_id": session_id,
                "host": host,
                "port": port,
            },
        )

    async def deny(
        self,
        *,
        session_id: str,
        host: str,
        reason: str,
        port: int | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "event": "deny",
            "session_id": session_id,
            "host": host,
            "reason": reason,
        }
        if port is not None:
            payload["port"] = port
        await asyncio.to_thread(self._write, payload)
