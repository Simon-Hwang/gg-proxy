"""Runtime guards for upstream Claude CLI auth failures.

Two independent safety nets, both lit by the same root cause:
ANTHROPIC_API_KEY rejected by the upstream API.

``Guard A`` (``api_retry`` budget): relay counts
``SystemMessage(subtype="api_retry")`` frames; once the count exceeds
``api_retry_budget`` (when > 0) the runner aborts with
``SDKPermissionError``. This stops the CLI's full 10-attempt internal
retry loop (~3 minutes of dead air) from costing the operator a
worker slot while producing nothing.

``Guard B`` (synthetic completion): if the CLI exhausts its OWN
retries before the relay budget kicks in (e.g. budget=0) it emits a
fabricated ``AssistantMessage(model="<synthetic>")`` carrying the
upstream error text, then a normal ``ResultMessage`` with zero
tokens. Without this guard the runner happily wrote
``status=completed`` for what was actually a credential failure.
The guard re-classifies that final ResultMessage as a permission
error so the manager writes ``status=failed end_reason=permission:401``.

Together the two cover the entire failure window:

  attempt 1 ── budget=N ──→ attempt N+1 raised by Guard A
                                       ↓
                                 SDKPermissionError

  attempt 1 ── budget=0 ──→ attempt 10 → synthetic msg → ResultMessage
                                                          ↓
                                                    Guard B raises
                                                    SDKPermissionError

Both raise the SAME exception class so downstream ``classify_sdk_error``
buckets them identically (``permission:403``).
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_code_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

from gg_relay.core.exceptions import SDKPermissionError
from gg_relay.session.client import (
    SYNTHETIC_MODEL_MARKER,
    make_sdk_runner,
)
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import DEFAULT_POLICY
from gg_relay.session.spec import PluginManifest, SessionSpec
from gg_relay.session.transport.inmemory import make_pair
from gg_relay.session.transport.protocol import TransportClosed

pytestmark = pytest.mark.asyncio


# ── helpers ──────────────────────────────────────────────────────────


def _spec(tmp_path: Path) -> SessionSpec:
    return SessionSpec(
        prompt="x",
        cwd=tmp_path,
        plugins=PluginManifest(profile="minimal"),
        executor="inprocess",
    )


class _StubClient:
    """Minimal duck-typed SDK client; subclasses define ``receive_messages``."""

    def __init__(self, options: Any) -> None:
        self.options = options

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def query(self, prompt: str) -> None:
        return None

    async def interrupt(self) -> None:
        return None

    async def receive_messages(self) -> AsyncIterator[Any]:  # pragma: no cover
        if False:
            yield None


def _make_retry_msg(
    *,
    attempt: int,
    max_retries: int = 10,
    error_status: int = 401,
    error: str = "authentication_failed",
) -> SystemMessage:
    """Build the exact ``SystemMessage`` shape the Claude CLI emits on
    upstream rejection (matches the live-session payload we captured
    in ``frames.payload`` for the user-reported regression)."""
    return SystemMessage(
        subtype="api_retry",
        data={
            "type": "system",
            "subtype": "api_retry",
            "attempt": attempt,
            "max_retries": max_retries,
            "retry_delay_ms": 1000.0,
            "error_status": error_status,
            "error": error,
            "session_id": "upstream-session-id",
            "uuid": f"retry-{attempt}",
        },
    )


def _make_synthetic_assistant(text: str) -> AssistantMessage:
    """Build the fabricated CLI message that follows retry exhaustion.

    ``model="<synthetic>"`` is the marker the CLI uses when it gives
    up and writes a fake "completion" carrying the error text. Matches
    the live frame we captured.
    """
    return AssistantMessage(
        model=SYNTHETIC_MODEL_MARKER,
        parent_tool_use_id=None,
        content=[TextBlock(text=text)],
    )


def _real_result_message() -> ResultMessage:
    """The ResultMessage the CLI emits after the synthetic completion.

    Note: tokens=0, cost=0 — proving the upstream call produced
    nothing. Pre-fix this got the runner to write
    ``session.end(status="completed", ...)``.
    """
    return ResultMessage(
        subtype="success",
        duration_ms=180_000,
        duration_api_ms=180_000,
        is_error=False,
        num_turns=0,
        session_id="upstream-session-id",
        total_cost_usd=0.0,
        usage={"input_tokens": 0, "output_tokens": 0},
    )


async def _drive(
    tmp_path: Path,
    sdk_factory,
    *,
    api_retry_budget: int = 0,
) -> tuple[list[dict[str, Any]], BaseException | None]:
    """Run one session with the given stub SDK + budget.

    Returns ``(frames, raised_exc)``. The runner is invoked DIRECTLY
    (not via ``InProcessExecutor``) because the executor intentionally
    swallows runner exceptions inside its task wrapper — production
    relies on ``SessionManager`` observing the failure via the
    ``except Exception`` path on its own ``_drive_session`` invocation.
    The exception STILL propagates from the runner coroutine, so this
    white-box helper awaits it directly to assert the guard fired.

    Frames produced before the raise are drained from the host side
    of the in-memory transport pair so callers can pin "the timeline
    still includes the triggering retry/synthetic frame" behaviour.
    """
    coord = HITLCoordinator()
    runner = make_sdk_runner(
        policy=DEFAULT_POLICY,
        coordinator=coord,
        sdk_factory=sdk_factory,
        api_retry_budget=api_retry_budget,
    )
    host_side, runner_side = make_pair()
    spec = _spec(tmp_path)

    raised: BaseException | None = None
    runner_task = asyncio.create_task(runner(runner_side, spec))

    async def _drain_host() -> list[dict[str, Any]]:
        seen: list[dict[str, Any]] = []
        while True:
            try:
                f = await host_side.recv()
            except TransportClosed:
                return seen
            seen.append(dict(f))
            if f["type"] == "session.end":
                return seen

    drain_task = asyncio.create_task(_drain_host())
    try:
        await runner_task
    except BaseException as exc:  # noqa: BLE001
        raised = exc
    finally:
        # Always close transports so the drain task exits cleanly.
        await runner_side.close()
        await host_side.close()
        with contextlib.suppress(Exception):
            frames = await drain_task
        if "frames" not in locals():
            frames = []
    return frames, raised


# ── Guard A: api_retry budget ────────────────────────────────────────


async def test_api_retry_budget_zero_disables_guard(tmp_path: Path) -> None:
    """Budget=0 is the documented "disabled" sentinel — even 10
    retry frames must not raise. Pre-existing behaviour must survive
    the new guard."""

    class _C(_StubClient):
        async def receive_messages(self) -> AsyncIterator[Any]:
            for i in range(1, 11):
                yield _make_retry_msg(attempt=i)
            # Land on a clean ResultMessage (no synthetic) so we
            # finish cleanly and prove the run was not aborted.
            yield ResultMessage(
                subtype="success",
                duration_ms=10,
                duration_api_ms=10,
                is_error=False,
                num_turns=1,
                session_id="s",
                total_cost_usd=0.001,
                usage={"input_tokens": 1, "output_tokens": 1},
            )

    frames, raised = await _drive(
        tmp_path, lambda opts: _C(opts), api_retry_budget=0
    )
    assert raised is None
    end = next(f for f in frames if f["type"] == "session.end")
    assert end["status"] == "completed"


async def test_api_retry_budget_raises_when_exceeded(tmp_path: Path) -> None:
    """Budget=3 + 4 retry frames must raise SDKPermissionError on
    frame 4 (count > budget). The frame is forwarded to the transport
    BEFORE the raise so the dashboard timeline still shows the
    triggering retry."""

    class _C(_StubClient):
        async def receive_messages(self) -> AsyncIterator[Any]:
            for i in range(1, 5):
                yield _make_retry_msg(attempt=i, error_status=401)
            # Should never be yielded — the runner must abort first.
            yield _real_result_message()

    frames, raised = await _drive(
        tmp_path, lambda opts: _C(opts), api_retry_budget=3
    )
    assert isinstance(raised, SDKPermissionError), (
        f"expected SDKPermissionError, got {type(raised).__name__}: {raised}"
    )
    msg = str(raised)
    assert "api_retry budget exhausted" in msg
    assert "count=4" in msg
    assert "budget=3" in msg
    # Diagnostic must include the upstream HTTP status so the operator
    # sees "401" in the end_reason column.
    assert "error_status=401" in msg
    # The 4th retry frame was forwarded before the raise so the
    # dashboard timeline shows the cause.
    retry_chunks = [
        f for f in frames
        if f["type"] == "msg.chunk"
        and f.get("data", {}).get("subtype") == "api_retry"
    ]
    assert len(retry_chunks) == 4
    # No session.end was emitted (the raise replaces it).
    assert not any(f["type"] == "session.end" for f in frames)


async def test_api_retry_budget_one_is_strict(tmp_path: Path) -> None:
    """Budget=1 means "at most 1 retry tolerated"; the 2nd raises."""

    class _C(_StubClient):
        async def receive_messages(self) -> AsyncIterator[Any]:
            yield _make_retry_msg(attempt=1)
            yield _make_retry_msg(attempt=2)

    _, raised = await _drive(
        tmp_path, lambda opts: _C(opts), api_retry_budget=1
    )
    assert isinstance(raised, SDKPermissionError)
    assert "count=2" in str(raised)
    assert "budget=1" in str(raised)


async def test_api_retry_count_resets_per_runner(tmp_path: Path) -> None:
    """Two separate runs must not share retry counters — the budget
    is per-runner, not process-global."""

    class _C(_StubClient):
        async def receive_messages(self) -> AsyncIterator[Any]:
            yield _make_retry_msg(attempt=1)
            yield ResultMessage(
                subtype="success",
                duration_ms=10,
                duration_api_ms=10,
                is_error=False,
                num_turns=1,
                session_id="s",
                total_cost_usd=0.001,
                usage={"input_tokens": 1, "output_tokens": 1},
            )

    # Two consecutive runs, each with budget=2 and 1 retry frame.
    # Pre-fix bug would be a process-global counter accumulating
    # across runs and tripping on run #2.
    for _ in range(2):
        _, raised = await _drive(
            tmp_path, lambda opts: _C(opts), api_retry_budget=2
        )
        assert raised is None


# ── Guard B: synthetic completion detection ──────────────────────────


async def test_synthetic_assistant_then_result_raises_permission(
    tmp_path: Path,
) -> None:
    """The exact sequence captured in the user-reported live session.

    With budget=0 (Guard A off) the runner must STILL fail the
    session via Guard B — otherwise the dashboard writes
    status=completed with zero tokens for what was actually a
    credential rejection."""

    class _C(_StubClient):
        async def receive_messages(self) -> AsyncIterator[Any]:
            # 3 retry frames (would survive budget=0)
            for i in range(1, 4):
                yield _make_retry_msg(attempt=i)
            # CLI gives up internally → synthetic AssistantMessage
            yield _make_synthetic_assistant(
                "Failed to authenticate. API Error: 401 unauthorized."
            )
            # …followed by a fake "successful" ResultMessage
            yield _real_result_message()

    frames, raised = await _drive(
        tmp_path, lambda opts: _C(opts), api_retry_budget=0
    )
    assert isinstance(raised, SDKPermissionError), (
        f"expected SDKPermissionError, got {type(raised).__name__}: {raised}"
    )
    msg = str(raised)
    assert "synthetic completion" in msg
    # The error text from the CLI must be threaded through so the
    # dashboard can show "401 unauthorized" instead of a generic
    # "synthetic completion" diagnostic.
    assert "401" in msg
    # No session.end was written — the raise replaces it.
    assert not any(f["type"] == "session.end" for f in frames)


async def test_real_assistant_then_result_completes_normally(
    tmp_path: Path,
) -> None:
    """Negative case: a non-synthetic AssistantMessage must NOT trip
    Guard B. Otherwise every successful session would be re-classified
    as a permission failure. This pins the "real model name resets the
    flag" branch."""

    class _C(_StubClient):
        async def receive_messages(self) -> AsyncIterator[Any]:
            yield AssistantMessage(
                model="claude-sonnet-4-5",
                parent_tool_use_id=None,
                content=[TextBlock(text="here is /tmp listing: …")],
            )
            yield ResultMessage(
                subtype="success",
                duration_ms=500,
                duration_api_ms=400,
                is_error=False,
                num_turns=1,
                session_id="s",
                total_cost_usd=0.01,
                usage={"input_tokens": 50, "output_tokens": 30},
            )

    frames, raised = await _drive(
        tmp_path, lambda opts: _C(opts), api_retry_budget=0
    )
    assert raised is None
    end = next(f for f in frames if f["type"] == "session.end")
    assert end["status"] == "completed"
    assert end["tokens"]["input_tokens"] == 50


async def test_synthetic_then_real_recovers_and_completes(
    tmp_path: Path,
) -> None:
    """Subtle edge case: a synthetic message may appear MID-stream
    (e.g. a transient blip) and the SDK may then continue and emit a
    real AssistantMessage + ResultMessage. The synthetic flag must
    reset on the real message so the run completes cleanly.

    Pins the ``else: last_synthetic_text = None`` branch in the
    runner core — easy to delete by accident and impossible to detect
    without this test."""

    class _C(_StubClient):
        async def receive_messages(self) -> AsyncIterator[Any]:
            yield _make_synthetic_assistant(
                "transient: please retry"
            )
            # Real message clears the flag
            yield AssistantMessage(
                model="claude-sonnet-4-5",
                parent_tool_use_id=None,
                content=[TextBlock(text="actual answer here")],
            )
            yield ResultMessage(
                subtype="success",
                duration_ms=100,
                duration_api_ms=80,
                is_error=False,
                num_turns=1,
                session_id="s",
                total_cost_usd=0.005,
                usage={"input_tokens": 20, "output_tokens": 10},
            )

    frames, raised = await _drive(
        tmp_path, lambda opts: _C(opts), api_retry_budget=0
    )
    assert raised is None, f"recovery path must complete, got {raised!r}"
    end = next(f for f in frames if f["type"] == "session.end")
    assert end["status"] == "completed"


# ── Cross-guard: both lit, A wins (fail-faster) ──────────────────────


async def test_budget_fires_before_synthetic_when_both_apply(
    tmp_path: Path,
) -> None:
    """When budget=2 AND the stream eventually emits a synthetic +
    ResultMessage, the budget guard must fire first (after the 3rd
    retry frame) and the synthetic guard never gets a chance. Both
    raise the SAME exception class but the message must come from
    the budget path."""

    class _C(_StubClient):
        async def receive_messages(self) -> AsyncIterator[Any]:
            for i in range(1, 4):  # 3 retry frames; budget=2 trips on 3
                yield _make_retry_msg(attempt=i)
            yield _make_synthetic_assistant("never reached")
            yield _real_result_message()

    _, raised = await _drive(
        tmp_path, lambda opts: _C(opts), api_retry_budget=2
    )
    assert isinstance(raised, SDKPermissionError)
    assert "api_retry budget exhausted" in str(raised), (
        "budget guard must win over synthetic guard when both apply"
    )
    assert "synthetic completion" not in str(raised)
