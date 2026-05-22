"""Tests for ToolPolicy."""
from __future__ import annotations

from pathlib import Path

import pytest

from gg_relay.session.hitl.policy import DEFAULT_POLICY, ToolPolicy
from gg_relay.session.spec import Decision


class TestNeutralTools:
    @pytest.mark.parametrize("tool", ["Read", "Glob", "Grep", "LS"])
    def test_neutral_always_accept(self, tool: str):
        assert DEFAULT_POLICY.decide(tool, {}, Path("/work")) == Decision.ACCEPT


class TestAutoAcceptFileTools:
    @pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "NotebookEdit"])
    def test_inside_cwd_accept(self, tool: str):
        d = DEFAULT_POLICY.decide(
            tool, {"file_path": "/work/src/main.py"}, Path("/work")
        )
        assert d == Decision.ACCEPT

    def test_outside_cwd_needs_hitl(self):
        d = DEFAULT_POLICY.decide(
            "Write", {"file_path": "/etc/passwd"}, Path("/work")
        )
        assert d == Decision.NEEDS_HITL

    @pytest.mark.parametrize("path", [
        "/work/.env",
        "/work/secrets/db.json",
        "/work/.git/config",
        "/work/id_rsa",
        "/work/cert.pem",
    ])
    def test_dangerous_pattern_needs_hitl(self, path: str):
        d = DEFAULT_POLICY.decide("Write", {"file_path": path}, Path("/work"))
        assert d == Decision.NEEDS_HITL

    @pytest.mark.parametrize("args", [
        {},
        {"file_path": ""},
        {"file_path": None},
        {"notebook_path": ""},
        {"path": ""},
        {"file_path": "", "notebook_path": "", "path": ""},
    ])
    def test_missing_or_empty_path_needs_hitl(self, args):
        """Path-required tools without a usable path string trigger HITL.

        Empty strings and None must be treated identically to a missing key —
        otherwise an SDK that passes `file_path=""` would bypass the path
        scoping check entirely.
        """
        d = DEFAULT_POLICY.decide("Write", args, Path("/work"))
        assert d == Decision.NEEDS_HITL

    def test_dangerous_pattern_case_insensitive(self, tmp_path: Path):
        """Uppercase variants of dangerous filenames must still trigger HITL.

        Fixes case-sensitivity bypass: .ENV / ID_RSA / .PEM are equivalent on
        macOS/Windows filesystems and must not slip through.
        """
        d = DEFAULT_POLICY.decide(
            "Write", {"file_path": str(tmp_path / "config.ENV")}, tmp_path
        )
        assert d == Decision.NEEDS_HITL

    def test_dangerous_pattern_via_symlink(self, tmp_path: Path):
        """A cwd-internal symlink pointing at a dangerous target must trigger HITL.

        Fixes symlink-bypass: an innocuous-looking name like 'innocent.txt'
        that resolves to '/work/.env' must be matched on the resolved path.
        """
        dangerous = tmp_path / ".env"
        dangerous.write_text("SECRET=x")
        link = tmp_path / "innocent.txt"
        link.symlink_to(dangerous)
        d = DEFAULT_POLICY.decide(
            "Write", {"file_path": str(link)}, tmp_path
        )
        assert d == Decision.NEEDS_HITL


class TestHITLTools:
    @pytest.mark.parametrize("tool", ["Bash", "WebFetch", "Task"])
    def test_always_hitl(self, tool: str):
        assert DEFAULT_POLICY.decide(tool, {}, Path("/work")) == Decision.NEEDS_HITL


class TestUnknownTools:
    def test_unknown_tool_conservative(self):
        assert DEFAULT_POLICY.decide("FrobTheBaz", {}, Path("/work")) == Decision.NEEDS_HITL


class TestPolicyOverride:
    def test_custom_policy_can_widen_auto_accept(self):
        custom = ToolPolicy(
            auto_accept_tools=frozenset({"Bash"}),
            hitl_tools=frozenset(),
            neutral_tools=frozenset(),
            # Bash isn't a file tool, so don't require a path. Must be a subset
            # of auto_accept_tools per ToolPolicy.__post_init__ invariant.
            path_required_tools=frozenset(),
            dangerous_patterns=(),
        )
        # Bash + no path-check (since no file_path key) → ACCEPT
        assert custom.decide("Bash", {"command": "ls"}, Path("/work")) == Decision.ACCEPT

    def test_invariant_rejects_leaked_path_required(self):
        """path_required_tools must be a subset of auto_accept_tools."""
        with pytest.raises(ValueError, match="path_required_tools"):
            ToolPolicy(
                auto_accept_tools=frozenset({"Edit"}),
                path_required_tools=frozenset({"Edit", "Bash"}),
            )
