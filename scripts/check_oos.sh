#!/usr/bin/env bash
# Plan 7 D7.24 OOS allowlist gate (AC #28), extended by Plan 8 Task 21.
#
# Scans the working copy for forbidden tokens that escaped Plan 5 / 6 / 7
# /8 scope cleanups. Source code, tests, deploy manifests, examples, and
# repo-root configs must remain clean of these substrings; historical
# design documents are explicitly out of scope (see EXCLUDE_DIRS /
# EXCLUDE_FILES below).
#
# This script is portable POSIX grep (no ripgrep dependency) so it can
# run in minimal CI containers and on operator laptops without extra
# tooling. Run via ``bash scripts/check_oos.sh`` from the repo root.
#
# Exit codes
# ----------
#   0  no forbidden tokens found
#   1  at least one forbidden token found (offending file + line printed)
#   2  invocation error (run from wrong directory, missing grep, etc.)

set -euo pipefail

# -----------------------------------------------------------------------------
# Forbidden fixed-string tokens (AC #28 allowlist closure).
#
# Each entry below is a Plan-7-scoped *deprecated* or *out-of-scope* symbol
# that must not reappear in living source. The CHANGELOG.md Deprecated
# section is allowed to mention removed items, hence the explicit file
# exclusion below.
# -----------------------------------------------------------------------------
FORBIDDEN_FIXED=(
  "dingtalk"
  "slack_backend"
  "SessionRecord"
  "SessionState.PENDING"
  "SessionState.CRASHED"
  'importlib.metadata.entry_points("gg_relay.im_backends")'
  "/ui/events"
  "pytest.mark.e2e"
  "scripts/dev.sh"
  # ---- Plan 8 §12 OOS extensions (Task 21) ----
  # Items kicked off Plan 8 scope; revisit in Plan 9+ if they
  # return. Like the Plan 7 tokens above, design docs that mention
  # these names by way of explaining the deferral are excluded via
  # EXCLUDE_DIRS=docs / EXCLUDE_FILES=PLAN.md|CHANGELOG.md.
  "session_replay"     # Plan 9+ scope — full event replay UI.
  "span_tree_svg"      # Plan 9+ scope — inline span tree SVG.
  "hitl_mute"          # Plan 11+ scope — security review pending.
  "runtime_keys.json"  # v1 D8.12 file-lock-backed key store.
  # ``kubernetes_asyncio`` was OOS for Plan 8 (deferred to Plan 9
  # D9.8); the K8sJobExecutor + [k8s] extra landed in 0.9.0 so the
  # token is no longer forbidden.
  "OIDC"               # Plan 11+ scope; not Plan 8.
  "tenant_id"          # multi-tenant RBAC explicitly OOS.
  "release-please"     # using manual release.yml from Plan 7.
)

# Regex tokens (used with ``grep -E``):
#   * ``/api/v1/hitl/.../approve`` — legacy router shape; the current API
#     resolves HITL via ``POST /api/v1/hitl/{request_id}/resolve``.
#   * ``/health`` — bare liveness path; the current router exposes
#     ``/healthz``. The regex below only flags ``/health`` when it appears
#     as a *quoted URL string* in code, so paths like ``{tmp_path}/health.db``
#     (a SQLite filename) and ``/healthz`` are correctly excluded.
FORBIDDEN_REGEX=(
  '/api/v1/hitl/[^[:space:]]*/approve'
  "[\"'\`]/health[^a-zA-Z0-9_/]"
  # Plan 8 Task 21 — v1 D8.12 file-lock impl pattern. The legitimate
  # DB-backed self-service path is ``api_keys`` (Alembic 0011) +
  # ``auth/`` package; fcntl.flock against runtime_keys.json was the
  # rejected alternative and must not regress into the source tree.
  'fcntl\.flock.*runtime_keys'
)

# -----------------------------------------------------------------------------
# Excludes.
#
# Directory excludes cover generated caches, the venv, and the historical
# design-doc tree (Plan 5 / 6 / 7 plans, spec §17, PLAN-style audit history).
# File excludes cover the changelog (Deprecated section legitimately names
# removed items), the in-tree historical PLAN.md (Santa-Method-verified
# v1 audit trail kept verbatim per spec §17.6), the uv.lock pinning file,
# and this script itself (which by definition mentions every token).
# -----------------------------------------------------------------------------
EXCLUDE_DIRS=(
  --exclude-dir=docs
  --exclude-dir=.git
  --exclude-dir=htmlcov
  --exclude-dir=.pytest_cache
  --exclude-dir=.ruff_cache
  --exclude-dir=.mypy_cache
  --exclude-dir=node_modules
  --exclude-dir=__pycache__
  --exclude-dir=.cursor
  --exclude-dir=.claude
)

# GNU grep's --exclude-dir is fixed-string, not glob. Multi-Python
# developers commonly run ``uv venv --python 3.11 .venv-py311`` next
# to ``.venv``, so we expand every dot-venv-prefixed directory at the
# repo root into its own --exclude-dir flag. Without this, the OOS
# scan walks third-party site-packages (mcp/testcontainers) and trips
# on legitimate ``OIDC`` / ``"/health"`` strings in their source.
for _venv_dir in .venv .venv-* venv venv-*; do
  if [[ -d "${_venv_dir}" ]]; then
    EXCLUDE_DIRS+=("--exclude-dir=${_venv_dir}")
  fi
done

EXCLUDE_FILES=(
  --exclude=CHANGELOG.md
  --exclude=PLAN.md
  --exclude=check_oos.sh
  --exclude=uv.lock
  --exclude=.coverage
  --exclude='*.pyc'
  # Plan 8 Task 21 — the Plan 8 OOS pattern *test* legitimately
  # enumerates every forbidden token (it's asserting they are listed
  # in this very script). Exclude it for the same reason the script
  # excludes itself.
  --exclude=test_oos_gate.py
)

if ! command -v grep >/dev/null 2>&1; then
  echo "check_oos: grep not found on PATH" >&2
  exit 2
fi

failed=0

scan_fixed() {
  local token="$1"
  if grep -rnF "${EXCLUDE_DIRS[@]}" "${EXCLUDE_FILES[@]}" -- "${token}" . 2>/dev/null; then
    echo "OOS: forbidden token '${token}' found" >&2
    failed=1
  fi
}

scan_regex() {
  local pattern="$1"
  if grep -rnE "${EXCLUDE_DIRS[@]}" "${EXCLUDE_FILES[@]}" -- "${pattern}" . 2>/dev/null; then
    echo "OOS: forbidden pattern '${pattern}' matched" >&2
    failed=1
  fi
}

for tok in "${FORBIDDEN_FIXED[@]}"; do
  scan_fixed "${tok}"
done

for pat in "${FORBIDDEN_REGEX[@]}"; do
  scan_regex "${pat}"
done

if [[ ${failed} -ne 0 ]]; then
  echo "OOS allowlist gate FAILED" >&2
  exit 1
fi

echo "OOS allowlist gate PASSED"
