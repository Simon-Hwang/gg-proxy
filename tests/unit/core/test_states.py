"""SessionState transition table tests (Plan 6 D6.1).

Covers the addition of ``PAUSED`` and the canonical legal-transition
table exposed via :data:`gg_relay.core.LEGAL_TRANSITIONS` /
:func:`is_legal_transition`. Tests intentionally exercise the *shape* of
the table (which edges exist, which don't) so regressions surface as
specific failed transitions instead of opaque "table changed" diffs.
"""
from __future__ import annotations

import pytest

from gg_relay.core import (
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    SessionState,
    is_legal_transition,
)


class TestPausedMember:
    """Plan 6 D6.1 adds the PAUSED enum member; cover its presence + shape."""

    def test_paused_value_is_lowercase_string(self):
        assert SessionState.PAUSED == "paused"

    def test_paused_not_terminal(self):
        assert SessionState.PAUSED not in TERMINAL_STATES

    def test_all_states_covered_in_transition_table(self):
        # Every enum member must appear as a key — guards against forgetting
        # to extend the table when adding a future state.
        assert set(LEGAL_TRANSITIONS.keys()) == set(SessionState)


class TestLegalTransitions:
    @pytest.mark.parametrize(
        "from_state, to_state",
        [
            (SessionState.QUEUED, SessionState.RUNNING),
            (SessionState.QUEUED, SessionState.CANCELLED),
            (SessionState.RUNNING, SessionState.PAUSED),
            (SessionState.RUNNING, SessionState.COMPLETED),
            (SessionState.RUNNING, SessionState.FAILED),
            (SessionState.RUNNING, SessionState.CANCELLED),
            (SessionState.RUNNING, SessionState.INTERRUPTED),
            (SessionState.PAUSED, SessionState.RUNNING),
            (SessionState.PAUSED, SessionState.CANCELLED),
            (SessionState.PAUSED, SessionState.INTERRUPTED),
            (SessionState.QUEUED, SessionState.INTERRUPTED),
        ],
    )
    def test_allowed_edges(self, from_state, to_state):
        assert is_legal_transition(from_state, to_state)

    @pytest.mark.parametrize(
        "from_state, to_state",
        [
            (SessionState.PAUSED, SessionState.COMPLETED),
            (SessionState.PAUSED, SessionState.FAILED),
            (SessionState.PAUSED, SessionState.QUEUED),
            (SessionState.RUNNING, SessionState.QUEUED),
            (SessionState.FAILED, SessionState.PAUSED),
            (SessionState.COMPLETED, SessionState.RUNNING),
            (SessionState.CANCELLED, SessionState.RUNNING),
            (SessionState.INTERRUPTED, SessionState.PAUSED),
            (SessionState.QUEUED, SessionState.PAUSED),
        ],
    )
    def test_rejected_edges(self, from_state, to_state):
        assert not is_legal_transition(from_state, to_state)


class TestTerminalStatesAreSinks:
    """No transitions out of any terminal state (defence-in-depth)."""

    @pytest.mark.parametrize("state", sorted(TERMINAL_STATES))
    def test_terminal_has_no_outgoing(self, state):
        assert LEGAL_TRANSITIONS[state] == frozenset()


class TestBackcompatPersistedRows:
    """The PAUSED addition must not break round-tripping older status
    strings out of the database (Plan 4 0001 baseline rows)."""

    @pytest.mark.parametrize(
        "raw",
        ["queued", "running", "completed", "failed", "cancelled", "interrupted"],
    )
    def test_legacy_status_strings_load(self, raw):
        # SessionState(raw) is how api/dashboard layers hydrate row["status"];
        # PAUSED is the only new value so the legacy 6 must still parse.
        state = SessionState(raw)
        assert state.value == raw

    def test_new_paused_value_loads(self):
        assert SessionState("paused") is SessionState.PAUSED


class TestIsLegalTransitionSelfEdge:
    """Self-edges (X → X) are always rejected — no-op transitions are not
    a valid state-machine input. SessionManager.pause/resume short-circuit
    no-ops *before* invoking the transition guard."""

    @pytest.mark.parametrize("state", list(SessionState))
    def test_self_transition_rejected(self, state):
        assert not is_legal_transition(state, state)
