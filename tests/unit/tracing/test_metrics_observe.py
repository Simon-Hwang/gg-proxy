"""Plan 7 Task 15 (D7.21) — MetricsSubscriber observe tests.

Covers the three new metric paths introduced by Task 15:

  * ``SESSION_DURATION`` histogram observation on RUNNING → terminal
  * canonical token field-name priority (``input_tokens`` > ``input``
    > ``in_`` > legacy ``tokens["in"]``)
  * ``COST_USD`` increment from ``cost_usd`` attribute
"""
from __future__ import annotations

from types import SimpleNamespace

from gg_relay.core import SessionStateChanged
from gg_relay.tracing.metrics import (
    COST_USD,
    REGISTRY,
    SESSION_DURATION,
    TOKENS_INPUT,
    TOKENS_OUTPUT,
)
from gg_relay.tracing.metrics_subscriber import MetricsSubscriber


def _read(name: str, labels: dict[str, str] | None = None) -> float:
    v = REGISTRY.get_sample_value(name, labels or {})
    return 0.0 if v is None else v


def _read_sum(histogram_name: str) -> float:
    return _read(f"{histogram_name}_sum")


class TestSessionDuration:
    def test_session_duration_observed_on_terminal(self) -> None:
        sub = MetricsSubscriber()
        before = _read_sum("gg_relay_session_duration_seconds")
        sub._on_state(
            SessionStateChanged(
                session_id="dur-1",
                from_state="queued",
                to_state="running",
            )
        )
        # Force a tiny but non-zero duration by mutating the start time.
        sub._start_times["dur-1"] -= 0.25
        sub._on_state(
            SessionStateChanged(
                session_id="dur-1",
                from_state="running",
                to_state="completed",
            )
        )
        after = _read_sum("gg_relay_session_duration_seconds")
        assert after > before
        assert "dur-1" not in sub._start_times


class TestTokenCanonicalPriority:
    def test_tokens_input_canonical_priority(self) -> None:
        sub = MetricsSubscriber()
        before = _read("gg_relay_tokens_input_total")
        # Both attrs present; canonical ``input_tokens`` must win.
        event = SimpleNamespace(
            input_tokens=100,
            input=50,
            output_tokens=0,
            cost_usd=0.0,
        )
        sub._on_aggregates(event)
        after = _read("gg_relay_tokens_input_total")
        assert after - before == 100.0

    def test_tokens_output_canonical_priority(self) -> None:
        sub = MetricsSubscriber()
        before = _read("gg_relay_tokens_output_total")
        event = SimpleNamespace(
            input_tokens=0,
            output_tokens=77,
            output=11,  # legacy alt — must be ignored when canonical present
            cost_usd=0.0,
        )
        sub._on_aggregates(event)
        after = _read("gg_relay_tokens_output_total")
        assert after - before == 77.0


class TestCostUsd:
    def test_cost_usd_inc(self) -> None:
        sub = MetricsSubscriber()
        before = _read("gg_relay_cost_usd_total")
        sub._on_aggregates(
            SimpleNamespace(
                input_tokens=0,
                output_tokens=0,
                cost_usd=0.5,
            )
        )
        after = _read("gg_relay_cost_usd_total")
        # Use round to avoid float fuzz across runs.
        assert round(after - before, 6) == 0.5


# Touch the cross-module symbols so unused-import lints stay quiet — the
# real assertion is via REGISTRY.get_sample_value above.
_ = SESSION_DURATION
_ = TOKENS_INPUT
_ = TOKENS_OUTPUT
_ = COST_USD
