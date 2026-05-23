"""OpenAPI snapshot drift gate (Plan 7 D7.11).

Regenerates the spec from the live :func:`create_app` factory and
asserts it exactly matches the committed
``docs/openapi.snapshot.json``. On drift the helper walks both trees
and prints the first ten differing paths so reviewers see *what*
changed, not just *that* it changed, alongside the one-line refresh
command (``make update-openapi-snapshot``).

We intentionally use plain dict comparison rather than pulling in
``jsondiff`` / ``deepdiff`` — those would be the only place in the
test suite needing them. The handwritten walker is ~15 lines and
gives us the surgical "first N diff paths" output we want.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = REPO_ROOT / "docs" / "openapi.snapshot.json"


def _walk_diff(
    baseline: Any, live: Any, *, path: str = "", out: list[str] | None = None
) -> list[str]:
    """Collect dotted paths where ``baseline`` differs from ``live``.

    Short-circuits identical subtrees so the cost is proportional to
    the diff, not to the spec size.
    """
    if out is None:
        out = []
    if baseline == live:
        return out
    if isinstance(baseline, dict) and isinstance(live, dict):
        for key in sorted(set(baseline) | set(live)):
            _walk_diff(
                baseline.get(key),
                live.get(key),
                path=f"{path}.{key}" if path else key,
                out=out,
            )
    elif isinstance(baseline, list) and isinstance(live, list):
        if len(baseline) != len(live):
            out.append(f"{path} (list length {len(baseline)}→{len(live)})")
        for idx, (b_item, live_item) in enumerate(zip(baseline, live, strict=False)):
            _walk_diff(b_item, live_item, path=f"{path}[{idx}]", out=out)
    else:
        out.append(path or "<root>")
    return out


def test_openapi_snapshot_matches() -> None:
    """The live OpenAPI spec must equal the committed snapshot."""
    os.environ.setdefault("RELAY_API_KEYS_RAW", "test-key")

    from gg_relay.api.main import create_app
    from gg_relay.config import Config

    cfg = Config()
    app = create_app(cfg)
    spec_live = app.openapi()

    spec_baseline = json.loads(SNAPSHOT.read_text())

    if spec_live != spec_baseline:
        diff_paths = _walk_diff(spec_baseline, spec_live)
        msg = (
            f"OpenAPI drift detected ({len(diff_paths)} diffs). "
            f"Run `make update-openapi-snapshot` to refresh "
            f"docs/openapi.snapshot.json. First diffs: "
            f"{diff_paths[:10]}"
        )
        raise AssertionError(msg)


def test_openapi_snapshot_is_committed_and_nonempty() -> None:
    """The snapshot file ships in-tree so reviewers can read it."""
    assert SNAPSHOT.exists(), (
        f"Missing {SNAPSHOT}; run `make update-openapi-snapshot`."
    )
    spec = json.loads(SNAPSHOT.read_text())
    assert spec.get("openapi", "").startswith("3."), (
        "Snapshot does not look like an OpenAPI 3.x spec"
    )
    assert spec.get("paths"), "Snapshot has no paths"
