"""Plan 8 Task 21 — OOS gate static patterns.

Lightweight assertion that the Plan 8 §12 OOS additions to
``scripts/check_oos.sh`` are in place; the *executable* gate is run
end-to-end from CI (``bash scripts/check_oos.sh``) and on operator
laptops. This unit-level companion just guards against accidental
re-deletion of the new patterns when somebody re-touches the script
later.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_OOS_SCRIPT = _REPO_ROOT / "scripts" / "check_oos.sh"


def test_oos_gate_has_plan8_patterns() -> None:
    """``scripts/check_oos.sh`` carries the Plan 8 §12 OOS pattern list."""

    text = _OOS_SCRIPT.read_text(encoding="utf-8")
    # Plan 7 D7.24 patterns must still be intact — Plan 8 only
    # *extends* the list, never removes it.
    for legacy in ["dingtalk", "slack_backend", "SessionRecord"]:
        assert legacy in text, f"Plan 7 D7.24 OOS token {legacy!r} unexpectedly removed"
    # Plan 8 Task 21 additions still on the OOS list (Plan 9 D9.8
    # promoted ``kubernetes_asyncio`` *off* the forbidden list since
    # the K8sJobExecutor uses it as an optional ``[k8s]`` extra).
    for new in [
        "session_replay",
        "span_tree_svg",
        "hitl_mute",
        "runtime_keys.json",
        "OIDC",
        "tenant_id",
        "release-please",
        "fcntl",  # the flock+runtime_keys regex
    ]:
        assert new in text, f"Plan 8 Task 21 OOS token {new!r} missing from check_oos.sh"


def test_oos_gate_passes_on_clean_tree() -> None:
    """End-to-end sanity: the gate exits 0 on the current working tree.

    If a future commit accidentally smuggles a forbidden token into
    ``src/`` or ``tests/``, this test surfaces it via the exact same
    error path CI prints. The subprocess call is bounded to a couple
    of seconds because the gate is portable POSIX-grep over excluded
    caches only.
    """

    result = subprocess.run(
        ["bash", str(_OOS_SCRIPT)],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        check=False,
    )
    assert result.returncode == 0, (
        f"OOS gate failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "PASSED" in result.stdout, f"OOS gate missing PASSED marker: {result.stdout!r}"
