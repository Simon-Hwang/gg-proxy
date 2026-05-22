#!/usr/bin/env python3
"""Plan 6 Task 0 — Deep-verify SDK interrupt/resume spike (c)(d).

Plan 5 Task 0 (``spike_sdk_interrupt_resume.py``) only verified (a)+(b):
``interrupt()`` halts the current turn, and a follow-up ``query()`` produces
a new ``ResultMessage``. This script picks up (c) and (d):

* **(c) Long-pause interrupt → sleep → resume.** Verifies that we can
  ``interrupt()`` the SDK, hold the client idle for a wall-clock pause that
  exceeds typical keep-alive intervals (default 120s, override with
  ``--pause-s``; in mock mode 2s for CI speed), then send a follow-up
  ``query()`` and still receive a ``ResultMessage`` on the same client. This
  is the core assumption behind the Plan 6 D6.1 PAUSED state — if the SDK
  drops the conversation after a long idle period we'd have to fall back to
  disconnect/reconnect (Plan 8 Roadmap).
* **(d) Self-interrupt from inside can_use_tool.** Verifies that calling
  ``client.interrupt()`` from inside the ``can_use_tool`` permission
  callback unwinds cleanly (no hung task / deadlock). Plan 6 deliberately
  does NOT use this pattern (HITL goes through the host coordinator, not
  via SDK self-interrupt), but we want a definitive negative-test
  baseline: if it deadlocks, Plan 6's choice to delegate via coordinator
  is the safer path; if it works, future Plan 7+ optimisations may use it.

The script is *report-oriented* (same convention as the Plan 5 spike) and
falls back to a mock SDK when ``ANTHROPIC_API_KEY`` is unset OR running as
root (the local ``claude`` CLI refuses to bypass permissions under root,
which prevents the real-mode SDK from issuing the prompt loop). Both
scenarios are still exercised in mock mode so we have a smoke test of our
own harness regardless of credentials; the report records ``mode`` clearly
so downstream readers know whether (c)(d) were really verified against the
upstream SDK.

Output: appends a "## Plan 6 Task 0 deep-verify" section to
``docs/sdk-interrupt-resume-spike.md``.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORT_PATH = REPO_ROOT / "docs" / "sdk-interrupt-resume-spike.md"


@dataclass
class TrialResult:
    label: str
    mode: str
    ok: bool
    duration_s: float
    notes: str = ""
    error: str | None = None


@dataclass
class DeepVerifyReport:
    sdk_version: str
    mode: str
    started_at: str
    finished_at: str = ""
    pause_s: float = 0.0
    trials: list[TrialResult] = field(default_factory=list)
    summary: str = "DV-INCONCLUSIVE"
    notes: list[str] = field(default_factory=list)


# ── real-mode drivers ───────────────────────────────────────────────────


async def _drive_real_long_pause(pause_s: float) -> TrialResult:
    """(c) interrupt → sleep(pause_s) → query → expect ResultMessage."""
    from claude_code_sdk import (
        AssistantMessage,
        ClaudeCodeOptions,
        ClaudeSDKClient,
        ResultMessage,
    )

    client = ClaudeSDKClient(ClaudeCodeOptions(allowed_tools=[]))
    notes_acc: list[str] = []
    err: str | None = None
    t0 = time.monotonic()
    ok = False
    try:
        await client.connect()
        await client.query(
            "Count slowly from 1 to 50, one number per line, "
            "with a brief one-sentence comment after each."
        )
        chunks_before = 0
        async for msg in client.receive_messages():
            if isinstance(msg, AssistantMessage):
                chunks_before += 1
                if chunks_before >= 2:
                    break
            if isinstance(msg, ResultMessage):
                break
        await client.interrupt()
        notes_acc.append(f"interrupt issued after {chunks_before} chunks")
        await asyncio.sleep(pause_s)
        notes_acc.append(f"slept {pause_s:.1f}s")
        await client.query("Reply with exactly: RESUMED_LONG_PAUSE")
        resume_completed = False
        async for msg in client.receive_messages():
            if isinstance(msg, ResultMessage):
                resume_completed = True
                break
        ok = resume_completed
        if not ok:
            notes_acc.append("no ResultMessage observed after long-pause resume")
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()
    return TrialResult(
        label="(c) long_pause",
        mode="real",
        ok=ok,
        duration_s=time.monotonic() - t0,
        notes="; ".join(notes_acc),
        error=err,
    )


async def _drive_real_self_interrupt() -> TrialResult:
    """(d) ``client.interrupt()`` from inside ``can_use_tool`` callback."""
    from claude_code_sdk import (
        ClaudeCodeOptions,
        ClaudeSDKClient,
        PermissionResultAllow,
        PermissionResultDeny,
        ResultMessage,
        ToolPermissionContext,
    )

    notes_acc: list[str] = []
    err: str | None = None
    t0 = time.monotonic()
    ok = False
    client: Any = None
    try:
        called_interrupt = asyncio.Event()

        async def can_use_tool(
            tool_name: str,
            tool_input: dict[str, Any],
            context: ToolPermissionContext,
        ) -> PermissionResultAllow | PermissionResultDeny:
            del tool_input, context
            notes_acc.append(f"callback fired for {tool_name}")
            if client is not None and not called_interrupt.is_set():
                called_interrupt.set()
                with contextlib.suppress(Exception):
                    await client.interrupt()
            return PermissionResultDeny(message="self-interrupt test deny")

        client = ClaudeSDKClient(
            ClaudeCodeOptions(can_use_tool=can_use_tool)
        )
        await client.connect()
        await client.query(
            "Run the Bash tool with command 'echo hi' so we can test "
            "the permission callback path."
        )
        result_seen = False
        async for msg in client.receive_messages():
            if isinstance(msg, ResultMessage):
                result_seen = True
                break
        ok = called_interrupt.is_set() and result_seen
        if not result_seen:
            notes_acc.append("no ResultMessage observed after self-interrupt")
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()
    return TrialResult(
        label="(d) self_interrupt",
        mode="real",
        ok=ok,
        duration_s=time.monotonic() - t0,
        notes="; ".join(notes_acc),
        error=err,
    )


# ── mock-mode drivers ────────────────────────────────────────────────────


class _MockClient:
    """Mock counterpart of ClaudeSDKClient with the same call surface used
    by (c) and (d). Mirrors the Plan 5 spike's mock contract."""

    def __init__(self) -> None:
        self.connected = False
        self.interrupt_count = 0
        self.queries: list[str] = []
        self._cancel = asyncio.Event()

    async def connect(self) -> None:
        self.connected = True

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)
        self._cancel.clear()

    async def interrupt(self) -> None:
        self.interrupt_count += 1
        self._cancel.set()

    async def disconnect(self) -> None:
        self.connected = False

    async def receive_messages(self) -> Any:
        for i in range(20):
            if self._cancel.is_set():
                yield {"type": "ResultMessage", "stop_reason": "interrupted"}
                return
            yield {"type": "AssistantMessage", "seq": i}
            await asyncio.sleep(0.01)
        yield {"type": "ResultMessage", "stop_reason": "completed"}


async def _drive_mock_long_pause(pause_s: float) -> TrialResult:
    notes_acc: list[str] = []
    err: str | None = None
    t0 = time.monotonic()
    ok = False
    client = _MockClient()
    try:
        await client.connect()
        await client.query("count to ten please")
        chunks = 0
        async for msg in client.receive_messages():
            if isinstance(msg, dict) and msg.get("type") == "AssistantMessage":
                chunks += 1
                if chunks >= 2:
                    break
        await client.interrupt()
        notes_acc.append(f"interrupt issued after {chunks} chunks")
        await asyncio.sleep(pause_s)
        notes_acc.append(f"slept {pause_s:.2f}s")
        await client.query("resume probe")
        resume_completed = False
        async for msg in client.receive_messages():
            if isinstance(msg, dict) and msg.get("type") == "ResultMessage":
                resume_completed = True
                break
        ok = resume_completed
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
    finally:
        await client.disconnect()
    return TrialResult(
        label="(c) long_pause",
        mode="mock",
        ok=ok,
        duration_s=time.monotonic() - t0,
        notes="; ".join(notes_acc),
        error=err,
    )


async def _drive_mock_self_interrupt() -> TrialResult:
    notes_acc: list[str] = []
    err: str | None = None
    t0 = time.monotonic()
    ok = False
    client = _MockClient()
    try:
        called_interrupt = asyncio.Event()

        async def callback() -> None:
            called_interrupt.set()
            await client.interrupt()

        await client.connect()
        await client.query("trigger tool")
        chunks = 0
        async for msg in client.receive_messages():
            if isinstance(msg, dict) and msg.get("type") == "AssistantMessage":
                chunks += 1
                # Simulate can_use_tool firing after chunk 1: schedule the
                # self-interrupt as a sibling task (matches how the SDK runs
                # the callback in its own task).
                if chunks == 1:
                    asyncio.create_task(callback())
            if isinstance(msg, dict) and msg.get("type") == "ResultMessage":
                break
        ok = called_interrupt.is_set() and client.interrupt_count == 1
        notes_acc.append(
            f"callback fired={called_interrupt.is_set()} "
            f"interrupts={client.interrupt_count}"
        )
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
    finally:
        await client.disconnect()
    return TrialResult(
        label="(d) self_interrupt",
        mode="mock",
        ok=ok,
        duration_s=time.monotonic() - t0,
        notes="; ".join(notes_acc),
        error=err,
    )


# ── orchestration ───────────────────────────────────────────────────────


def _detect_mode(forced: str | None) -> str:
    if forced in ("real", "mock"):
        return forced
    if os.environ.get("ANTHROPIC_API_KEY") and os.geteuid() != 0:
        return "real"
    return "mock"


def _summarise(trials: list[TrialResult]) -> str:
    real = [t for t in trials if t.mode == "real"]
    if not real:
        return "DV-INCONCLUSIVE"
    if all(t.ok for t in real):
        return "DV-OK"
    if any(t.ok for t in real):
        return "DV-PARTIAL"
    return "DV-FAIL"


async def _run(mode: str, pause_s: float) -> DeepVerifyReport:
    import claude_code_sdk

    sdk_version = getattr(claude_code_sdk, "__version__", "unknown")
    report = DeepVerifyReport(
        sdk_version=sdk_version,
        mode=mode,
        started_at=datetime.now(UTC).isoformat(),
        pause_s=pause_s,
    )

    if mode == "real":
        trials = [
            await _drive_real_long_pause(pause_s),
            await _drive_real_self_interrupt(),
        ]
    else:
        trials = [
            await _drive_mock_long_pause(min(pause_s, 2.0)),
            await _drive_mock_self_interrupt(),
        ]
    report.trials = trials
    report.summary = _summarise(trials)
    report.finished_at = datetime.now(UTC).isoformat()

    if mode == "mock":
        report.notes.append(
            "Ran in mock mode (no ANTHROPIC_API_KEY or running as root). "
            "Real-API re-run required from a non-root shell with the key "
            "exported to upgrade DV-INCONCLUSIVE → DV-OK/PARTIAL/FAIL."
        )
    return report


_DEEP_VERIFY_MARKER = "## Plan 6 Task 0 deep-verify"


def _render_section(report: DeepVerifyReport) -> str:
    lines = [
        _DEEP_VERIFY_MARKER,
        "",
        f"- Generated: `{report.finished_at}`",
        f"- claude-code-sdk: `{report.sdk_version}`",
        f"- Mode: **{report.mode}**",
        f"- Pause window: **{report.pause_s:.1f}s** (real) / "
        f"**{min(report.pause_s, 2.0):.1f}s** (mock)",
        f"- Summary: **{report.summary}**",
        "",
        "### Outcomes",
        "",
        "| Outcome | Meaning |",
        "|---|---|",
        "| `DV-OK` | (c) and (d) both pass against the real SDK. PAUSED + |"
        " HITL-via-coordinator design is safe as written. |",
        "| `DV-PARTIAL` | Only one of (c)/(d) passes. Note which one and add "
        "fallback to Plan 6 §10 risk table. |",
        "| `DV-FAIL` | Neither (c) nor (d) passes. Plan 6 §10 must add a "
        "long-pause-via-disconnect/reconnect mitigation. |",
        "| `DV-INCONCLUSIVE` | Ran in mock fallback. Re-run with real key "
        "before relying on (c)(d) guarantees. |",
        "",
        "### Trials",
        "",
        "| # | Label | Mode | OK | Duration (s) | Notes / Error |",
        "|---|---|---|---|---|---|",
    ]
    for i, t in enumerate(report.trials, 1):
        cell = t.notes or ""
        if t.error:
            cell = f"ERROR={t.error}; {cell}".rstrip("; ")
        lines.append(
            f"| {i} | {t.label} | {t.mode} | "
            f"{'yes' if t.ok else 'no'} | {t.duration_s:.2f} | {cell} |"
        )
    lines += [
        "",
        "### Notes",
        "",
    ]
    if report.notes:
        for n in report.notes:
            lines.append(f"- {n}")
    else:
        lines.append("- (none)")
    lines += [
        "",
        "### Raw JSON",
        "",
        "```json",
        json.dumps(asdict(report), indent=2),
        "```",
        "",
    ]
    return "\n".join(lines)


def _upsert_section(report: DeepVerifyReport) -> None:
    section = _render_section(report)
    existing = REPORT_PATH.read_text(encoding="utf-8") if REPORT_PATH.exists() else ""
    if _DEEP_VERIFY_MARKER in existing:
        head, _sep, _tail = existing.partition(_DEEP_VERIFY_MARKER)
        new_body = head.rstrip() + "\n\n" + section + "\n"
    else:
        new_body = existing.rstrip() + "\n\n" + section + "\n"
    REPORT_PATH.write_text(new_body, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("auto", "real", "mock"),
        default=os.environ.get("SPIKE_MODE", "auto"),
    )
    parser.add_argument(
        "--pause-s",
        type=float,
        default=float(os.environ.get("SPIKE_PAUSE_S", "120")),
        help="Pause duration for (c) long_pause trial (default: 120s)",
    )
    args = parser.parse_args()
    mode = _detect_mode(None if args.mode == "auto" else args.mode)
    try:
        report = asyncio.run(_run(mode, args.pause_s))
    except Exception:
        traceback.print_exc()
        return 2
    _upsert_section(report)
    print(f"\n=== Deep-verify summary: {report.summary} (mode={report.mode}) ===")
    for t in report.trials:
        print(
            f"  - {t.label} [{t.mode}] ok={'Y' if t.ok else 'n'} "
            f"({t.duration_s:.2f}s) {t.notes or ''}"
        )
    print(f"Report appended: {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
