#!/usr/bin/env python3
"""Plan 10 D10.4 — SDK multi-query behaviour spike.

This is deliberately a spike, not a production feature. The current relay
runner emits ``session.end`` and disconnects after the first ResultMessage,
so multi-query continuation needs a separate Plan 11 design even if the SDK
itself can accept repeated ``query(...)`` calls on one connected client.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORT_PATH = REPO_ROOT / "docs" / "sdk-multi-query-spike.md"


@dataclass(slots=True)
class SpikeResult:
    mode: str
    ok: bool
    first_result: bool
    second_result: bool
    hook_events: list[str]
    duration_s: float
    error: str | None = None


class _MockClient:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

    async def receive_messages(self) -> Any:
        yield {"type": "ResultMessage", "query_count": len(self.queries)}


async def _drain_until_result(client: Any, result_type: type[Any] | None) -> bool:
    async with asyncio.timeout(60):
        async for msg in client.receive_messages():
            if result_type is None:
                if isinstance(msg, dict) and msg.get("type") == "ResultMessage":
                    return True
            elif isinstance(msg, result_type):
                return True
    return False


async def _run_mock() -> SpikeResult:
    t0 = time.monotonic()
    client = _MockClient()
    await client.connect()
    await client.query("first")
    first = await _drain_until_result(client, None)
    await client.query("second")
    second = await _drain_until_result(client, None)
    await client.disconnect()
    return SpikeResult(
        mode="mock",
        ok=first and second,
        first_result=first,
        second_result=second,
        hook_events=[],
        duration_s=time.monotonic() - t0,
    )


async def _run_real() -> SpikeResult:
    from claude_code_sdk import ClaudeCodeOptions, ClaudeSDKClient, ResultMessage
    from claude_code_sdk.types import HookContext, HookJSONOutput, HookMatcher

    hook_events: list[str] = []

    async def _hook(
        hook_input: dict[str, Any],
        tool_name: str | None,
        context: HookContext,
    ) -> HookJSONOutput:
        del hook_input, tool_name, context
        hook_events.append("UserPromptSubmit")
        return {}

    client = ClaudeSDKClient(
        ClaudeCodeOptions(
            hooks={
                "UserPromptSubmit": [
                    HookMatcher(matcher=None, hooks=[_hook]),
                ]
            }
        )
    )
    t0 = time.monotonic()
    err: str | None = None
    first = False
    second = False
    try:
        await client.connect()
        await client.query("Reply with exactly: MQ_ONE")
        first = await _drain_until_result(client, ResultMessage)
        await client.query("Reply with exactly: MQ_TWO")
        second = await _drain_until_result(client, ResultMessage)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()
    return SpikeResult(
        mode="real",
        ok=first and second and len(hook_events) >= 2,
        first_result=first,
        second_result=second,
        hook_events=hook_events,
        duration_s=time.monotonic() - t0,
        error=err,
    )


def _write_report(result: SpikeResult) -> None:
    status = "PASS" if result.ok else "INCONCLUSIVE"
    REPORT_PATH.write_text(
        "\n".join(
            [
                "# SDK Multi-Query Spike",
                "",
                f"Generated: {datetime.now(UTC).isoformat()}",
                f"Mode: `{result.mode}`",
                f"Status: `{status}`",
                "",
                "## Observations",
                "",
                f"- First query ResultMessage observed: `{result.first_result}`",
                f"- Second query ResultMessage observed: `{result.second_result}`",
                f"- Hook events observed: `{len(result.hook_events)}`",
                f"- Duration: `{result.duration_s:.2f}s`",
                f"- Error: `{result.error or ''}`",
                "",
                "## Plan 10 Decision",
                "",
                "Do not expose `/continue` or a continuation schema in Plan 10. "
                "The production runner still terminates at the first ResultMessage; "
                "multi-turn relay semantics need a dedicated Plan 11 transport and "
                "lifecycle design.",
                "",
                "## Reproduce",
                "",
                "```bash",
                "uv run python scripts/spike_sdk_multi_query.py",
                "SPIKE_MODE=real uv run python scripts/spike_sdk_multi_query.py",
                "```",
                "",
            ]
        ),
        encoding="utf-8",
    )


async def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["auto", "mock", "real"], default="auto")
    args = parser.parse_args()
    mode = os.environ.get("SPIKE_MODE", args.mode)
    if mode == "auto":
        mode = "real" if os.environ.get("ANTHROPIC_API_KEY") else "mock"
    result = await (_run_real() if mode == "real" else _run_mock())
    _write_report(result)
    print(f"[spike] mode={result.mode} ok={result.ok} report={REPORT_PATH}")
    return 0 if result.ok or result.mode == "mock" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
