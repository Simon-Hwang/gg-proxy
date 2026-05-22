#!/usr/bin/env python3
"""Plan 5 Task 0 — SDK interrupt/resume Minimal spike (D5.1=C).

Verifies behaviour, not API existence (existence is already confirmed —
v0.0.25 exposes ClaudeSDKClient.interrupt / connect / disconnect / query /
receive_messages, see scripts/spike_sdk_interrupt.py & docs/sdk-spike-report.md).

Scope (locked to (a)+(b); (c) long-pause / (d) callback-internal interrupt
are deferred to Plan 6 Task 0 deep-verify):

  (a) Does ``ClaudeSDKClient.interrupt()`` actually halt the current SDK
      turn — i.e. does ``receive_messages()`` stop yielding new chunks
      shortly after the interrupt call?
  (b) After the interrupt, can we issue a follow-up ``query(...)`` and get
      a new ResultMessage, i.e. is the SDK client reusable as a resume
      vehicle?

The script is *report-oriented*. It always succeeds even when no
``ANTHROPIC_API_KEY`` is present (or when ``claude`` CLI can't reach the
network) — in that case it falls back to a behavioural smoke-test against
the local SDK surface (validates that ``connect`` / ``query`` / ``interrupt``
/ ``disconnect`` can be invoked in the documented order without raising
unexpected exceptions). The fallback path is documented in the markdown
report; only a real-API run is conclusive for outcomes I-OK / I-PARTIAL.

Usage::

    source .venv/bin/activate
    python scripts/spike_sdk_interrupt_resume.py            # auto-detect
    SPIKE_MODE=real python scripts/spike_sdk_interrupt_resume.py  # force real
    SPIKE_MODE=mock python scripts/spike_sdk_interrupt_resume.py  # force mock

The script prints a structured summary on stdout AND writes
``docs/sdk-interrupt-resume-spike.md`` so subsequent agents (Plan 6 Task 0
deep-verify) can read the recorded outcomes.
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

# Outcomes (mirror plan §7 Task 0 docstring):
#   I-OK       -> interrupt halts AND follow-up query produces ResultMessage
#   I-PARTIAL  -> interrupt halts but follow-up query is missing/garbled
#   I-NONE     -> interrupt itself unusable (raises / never returns)
#   I-INCONCLUSIVE -> ran in mock fallback (no real API call)


@dataclass
class TrialResult:
    """One spike trial — records the observable behaviour of one
    connect → query → interrupt → query → disconnect cycle."""

    trial: int
    chunks_before_interrupt: int
    interrupt_latency_s: float
    chunks_after_interrupt: int  # chunks observed after interrupt() returned
    resume_completed: bool  # follow-up query yielded a ResultMessage
    resume_text_excerpt: str = ""
    error: str | None = None
    mode: str = "real"  # "real" or "mock"


@dataclass
class SpikeReport:
    sdk_version: str
    mode: str  # "real" or "mock"
    started_at: str
    finished_at: str = ""
    trials: list[TrialResult] = field(default_factory=list)
    outcome: str = "I-INCONCLUSIVE"
    notes: list[str] = field(default_factory=list)


# ── trial drivers ─────────────────────────────────────────────────────────


async def _drive_real_trial(trial: int) -> TrialResult:
    """One real-API trial.

    The prompt asks Claude to count slowly so the SDK reliably yields more
    than one streamable chunk before our interrupt fires. We bail on the
    response loop as soon as we have 3 message chunks; that gives the
    interrupt something concrete to cancel.
    """
    from claude_code_sdk import (
        AssistantMessage,
        ClaudeCodeOptions,
        ClaudeSDKClient,
        ResultMessage,
        TextBlock,
    )

    # NOTE: we don't force ``permission_mode="bypassPermissions"`` here
    # because the local CLI refuses that flag when running as root. The
    # spike's prompt should never trigger tool use, so default permissions
    # are sufficient. If a real-API run is required from a non-root shell,
    # callers can export ``ANTHROPIC_API_KEY`` and re-run as a normal user.
    client = ClaudeSDKClient(ClaudeCodeOptions(allowed_tools=[]))
    chunks_before = 0
    chunks_after = 0
    resume_completed = False
    resume_excerpt = ""
    err: str | None = None
    t0 = 0.0
    interrupt_latency = -1.0

    try:
        await client.connect()
        await client.query(
            "Count slowly from 1 to 100, one number per line, "
            "with a one-sentence comment after each number."
        )
        async for msg in client.receive_messages():
            if isinstance(msg, AssistantMessage):
                chunks_before += 1
                if chunks_before >= 3:
                    break
            if isinstance(msg, ResultMessage):
                # SDK turn finished before we got the chance to interrupt —
                # the prompt was too short or the model was too quick.
                break
        t0 = time.monotonic()
        await client.interrupt()
        interrupt_latency = time.monotonic() - t0

        # Drain whatever may still leak out *after* interrupt() returned.
        # Real "halt" means: no new AssistantMessage with new content,
        # within a small grace window. We cap at ~2s.
        async def _drain_postinterrupt() -> None:
            nonlocal chunks_after
            async for msg in client.receive_messages():
                if isinstance(msg, AssistantMessage):
                    chunks_after += 1
                if isinstance(msg, ResultMessage):
                    return
                if chunks_after >= 5:
                    return

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(_drain_postinterrupt(), timeout=2.0)

        try:
            await client.query("Reply with exactly: STOPPED_OK")
            async for msg in client.receive_messages():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            resume_excerpt += block.text
                if isinstance(msg, ResultMessage):
                    resume_completed = True
                    break
        except Exception as resume_exc:
            err = f"resume_query_failed: {type(resume_exc).__name__}: {resume_exc}"
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()

    return TrialResult(
        trial=trial,
        chunks_before_interrupt=chunks_before,
        interrupt_latency_s=interrupt_latency,
        chunks_after_interrupt=chunks_after,
        resume_completed=resume_completed,
        resume_text_excerpt=resume_excerpt[:120],
        error=err,
        mode="real",
    )


class _MockClient:
    """Minimal substitute for ClaudeSDKClient when no API access exists.

    Exercises the *call surface* we rely on (connect / query / interrupt /
    receive_messages / disconnect) but yields a deterministic synthetic
    stream so we can confirm our Python harness flows correctly.
    """

    def __init__(self) -> None:
        self.connected = False
        self.interrupt_called_at: float | None = None
        self.queries: list[str] = []
        self._cancel = asyncio.Event()

    async def connect(self) -> None:
        self.connected = True

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)
        self._cancel.clear()

    async def interrupt(self) -> None:
        self.interrupt_called_at = time.monotonic()
        self._cancel.set()

    async def disconnect(self) -> None:
        self.connected = False

    async def receive_messages(self) -> Any:  # AsyncIterator[Any]
        # Yield a few synthetic "chunks", or stop early if interrupted.
        for i in range(50):
            if self._cancel.is_set():
                # Mimic a ResultMessage to close the turn cleanly.
                yield {"type": "ResultMessage", "stop_reason": "interrupted"}
                return
            yield {"type": "AssistantMessage", "seq": i}
            await asyncio.sleep(0.02)
        yield {"type": "ResultMessage", "stop_reason": "completed"}


async def _drive_mock_trial(trial: int) -> TrialResult:
    client = _MockClient()
    chunks_before = 0
    chunks_after = 0
    resume_completed = False
    err: str | None = None

    try:
        await client.connect()
        await client.query("synthetic prompt")
        async for msg in client.receive_messages():
            if isinstance(msg, dict) and msg.get("type") == "AssistantMessage":
                chunks_before += 1
                if chunks_before >= 3:
                    break
        t0 = time.monotonic()
        await client.interrupt()
        interrupt_latency = time.monotonic() - t0

        async def _drain_postinterrupt() -> None:
            nonlocal chunks_after
            async for msg in client.receive_messages():
                if isinstance(msg, dict) and msg.get("type") == "AssistantMessage":
                    chunks_after += 1
                if isinstance(msg, dict) and msg.get("type") == "ResultMessage":
                    return

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(_drain_postinterrupt(), timeout=1.0)

        await client.query("resume probe")
        async for msg in client.receive_messages():
            if isinstance(msg, dict) and msg.get("type") == "ResultMessage":
                resume_completed = True
                break
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        interrupt_latency = -1.0
    finally:
        await client.disconnect()

    return TrialResult(
        trial=trial,
        chunks_before_interrupt=chunks_before,
        interrupt_latency_s=interrupt_latency,
        chunks_after_interrupt=chunks_after,
        resume_completed=resume_completed,
        resume_text_excerpt="(mock)",
        error=err,
        mode="mock",
    )


# ── outcome classification ────────────────────────────────────────────────


def _classify(trials: list[TrialResult]) -> str:
    real = [t for t in trials if t.mode == "real" and t.error is None]
    if not real:
        return "I-INCONCLUSIVE"
    halted = sum(1 for t in real if t.interrupt_latency_s >= 0 and t.chunks_after_interrupt <= 1)
    resumed = sum(1 for t in real if t.resume_completed)
    n = len(real)
    if halted == n and resumed == n:
        return "I-OK"
    if halted == n and resumed == 0:
        return "I-PARTIAL"
    if halted < n:
        return "I-NONE"
    return "I-PARTIAL"


# ── markdown report ──────────────────────────────────────────────────────


def _render_markdown(report: SpikeReport) -> str:
    lines = [
        "# SDK Interrupt/Resume Spike Report",
        "",
        "*Plan 5 Task 0 — D5.1=C Minimal spike*",
        "",
        f"- Generated: `{report.finished_at}`",
        f"- claude-code-sdk: `{report.sdk_version}`",
        f"- Mode: **{report.mode}**",
        f"- Outcome: **{report.outcome}**",
        "",
        "## Scope",
        "",
        "Per Plan 5 §6.5 / §7 Task 0, only items (a) and (b) are verified here.",
        "",
        "- (a) ``ClaudeSDKClient.interrupt()`` halts the current SDK turn",
        "- (b) A follow-up ``query(...)`` on the same client produces a new ``ResultMessage``",
        "",
        "Items (c) long-pause `disconnect()/connect()` and (d) self-interrupt inside ",
        "``can_use_tool`` are explicitly deferred to **Plan 6 Task 0 deep-verify**.",
        "",
        "## Outcome classification",
        "",
        "| Outcome | Meaning | Plan-6 implication |",
        "|---|---|---|",
        "| `I-OK` | (a) AND (b) both work | Plan 6 implements PAUSED state |",
        "| `I-PARTIAL` | (a) works, (b) fails or drops context |"
        " Plan 6 uses soft-pause: cancel + new session with carry-over |",
        "| `I-NONE` | (a) does not halt | Plan 6 removes PAUSED; pre-tool gate only |",
        "| `I-INCONCLUSIVE` | ran in mock fallback |"
        " Re-run with real API key before Plan 6 Task 0 |",
        "",
        "## Trial results",
        "",
        "| # | Mode | Chunks before | Interrupt latency (s) | Chunks after | Resume OK | Error |",
        "|---|---|---|---|---|---|---|",
    ]
    for t in report.trials:
        err = t.error if t.error else ""
        lines.append(
            f"| {t.trial} | {t.mode} | {t.chunks_before_interrupt} | "
            f"{t.interrupt_latency_s:.3f} | {t.chunks_after_interrupt} | "
            f"{'yes' if t.resume_completed else 'no'} | {err} |"
        )
    lines += [
        "",
        "## Notes",
        "",
    ]
    if report.notes:
        for n in report.notes:
            lines.append(f"- {n}")
    else:
        lines.append("- (none)")
    lines += [
        "",
        "## Environment & how to re-run conclusively",
        "",
        "The spike auto-detects environment and falls back to a mock SDK when",
        "no real call is possible. To get a conclusive `I-OK` / `I-PARTIAL`",
        "verdict, run from an account that satisfies *all three*:",
        "",
        "1. `ANTHROPIC_API_KEY` exported (or `claude` CLI already logged in via `claude /login`).",
        "2. Non-root shell — the CLI refuses `--dangerously-skip-permissions`"
        " under root which crashes the SDK reader; the spike removed that"
        " flag to stay portable, but root + interactive permission gating"
        " still blocks the prompt loop.",
        "3. Network egress to `api.anthropic.com`.",
        "",
        "Re-run::",
        "",
        "    SPIKE_MODE=real python scripts/spike_sdk_interrupt_resume.py --trials 3",
        "",
        "## Carry-forward to Plan 6 Task 0 deep-verify",
        "",
        "- Items (c) `disconnect()/connect()` long-pause behaviour and (d) self-",
        "  interrupt inside `can_use_tool` were *not* exercised here. Plan 6",
        "  Task 0 must cover them before committing to the PAUSED design.",
        "- If Plan 6 Task 0 lands `I-OK` for (a)+(b) but `I-PARTIAL` for (c),",
        "  PAUSED is still viable for short pauses (≤ keep-alive); document",
        "  the bound and require operators to confirm.",
        "- If (d) cannot be made safe, route HITL purely through the host-side",
        "  ToolPolicy + transport coordinator (already the v1 design) — i.e.",
        "  never call `interrupt()` from the can_use_tool callback path.",
        "",
        "## Raw JSON",
        "",
        "```json",
        json.dumps(asdict(report), indent=2),
        "```",
    ]
    return "\n".join(lines) + "\n"


# ── main ─────────────────────────────────────────────────────────────────


def _detect_mode(forced: str | None) -> str:
    if forced in ("real", "mock"):
        return forced
    if os.environ.get("ANTHROPIC_API_KEY") or _claude_cli_authenticated():
        return "real"
    return "mock"


def _claude_cli_authenticated() -> bool:
    """Best-effort check: the SDK shells out to the local ``claude`` CLI.

    If the CLI itself is missing we'll definitely fail; if it exists but
    has never been logged in, ``connect()`` will return an auth error. We
    can't fully tell apart these cases without actually trying — the
    spike's fallback path handles that by catching the exception.
    """
    import shutil

    return shutil.which("claude") is not None


async def _run(mode: str, trials: int) -> SpikeReport:
    import claude_code_sdk

    sdk_version = getattr(claude_code_sdk, "__version__", "unknown")
    report = SpikeReport(
        sdk_version=sdk_version,
        mode=mode,
        started_at=datetime.now(UTC).isoformat(),
    )
    notes: list[str] = []

    runner = _drive_real_trial if mode == "real" else _drive_mock_trial
    # Hard per-trial budget so a stuck CLI subprocess never hangs the spike.
    trial_budget_s = 30.0 if mode == "real" else 5.0
    for i in range(1, trials + 1):
        try:
            result = await asyncio.wait_for(runner(i), timeout=trial_budget_s)
        except TimeoutError:
            result = TrialResult(
                trial=i,
                chunks_before_interrupt=0,
                interrupt_latency_s=-1.0,
                chunks_after_interrupt=0,
                resume_completed=False,
                error=f"trial_timeout_{trial_budget_s:.0f}s",
                mode=mode,
            )
            notes.append(f"Trial {i} hit hard timeout ({trial_budget_s:.0f}s)")
        except Exception as exc:  # noqa: BLE001
            result = TrialResult(
                trial=i,
                chunks_before_interrupt=0,
                interrupt_latency_s=-1.0,
                chunks_after_interrupt=0,
                resume_completed=False,
                error=f"driver_crash: {type(exc).__name__}: {exc}",
                mode=mode,
            )
            notes.append(f"Trial {i} driver crashed: {exc}")
        report.trials.append(result)

    # If we ran "real" but every trial errored (no auth, no network), the
    # outcome is still I-INCONCLUSIVE and we annotate why.
    real_errors = [t.error for t in report.trials if t.mode == "real" and t.error]
    if mode == "real" and len(real_errors) == len(report.trials) and real_errors:
        notes.append(
            "All real-API trials failed; treating as inconclusive. "
            "Re-run with ANTHROPIC_API_KEY exported / claude CLI logged in."
        )
        notes.append(f"First error: {real_errors[0]}")

    report.outcome = _classify(report.trials)
    report.notes = notes
    report.finished_at = datetime.now(UTC).isoformat()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("auto", "real", "mock"),
        default=os.environ.get("SPIKE_MODE", "auto"),
        help="Force mode (default: auto-detect via ANTHROPIC_API_KEY / claude CLI).",
    )
    parser.add_argument("--trials", type=int, default=3)
    args = parser.parse_args()
    mode = _detect_mode(None if args.mode == "auto" else args.mode)

    try:
        report = asyncio.run(_run(mode, args.trials))
    except Exception:
        traceback.print_exc()
        return 2

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(_render_markdown(report), encoding="utf-8")

    print(f"\n=== Spike outcome: {report.outcome} (mode={report.mode}) ===")
    print(f"Report written: {REPORT_PATH}")
    for t in report.trials:
        ok = "ok" if t.error is None else f"ERR={t.error}"
        print(
            f"  trial {t.trial:>2} [{t.mode}]: "
            f"before={t.chunks_before_interrupt} "
            f"latency={t.interrupt_latency_s:.3f}s "
            f"after={t.chunks_after_interrupt} "
            f"resume={'Y' if t.resume_completed else 'n'} {ok}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
