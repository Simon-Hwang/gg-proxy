# Plan v5 — Require per-user credentials (C+B)

**Date**: 2026-05-26
**Author**: gg-relay
**Supersedes**: v1, v2, v3, v4 (all FAIL × 2)
**Status**: For final Santa review.

## Changelog vs. v4 (FAIL → fix)

Two clean mechanical issues identified by Reviewers N + O:

| # | v4 Critical | v5 Fix |
|---|------------|--------|
| 1 | `scripts/load_test.py` argparse approach is wrong — file is Locust-based (`from locust import HttpUser, events, ...`), no module-level argparse exists. Reviewers N.1, O.1. | **Switch to env-var only.** Drop CLI flag complexity. Read `CREDENTIALS_KEY = os.environ.get("ANTHROPIC_API_KEY", "")` at module top; include in body when non-empty. Strict-mode operators set `ANTHROPIC_API_KEY` in the env before running locust (same pattern they'd use for the relay itself). Zero Locust integration risk. |
| 3 | Inlined `batch_sessions` snippet used non-existent `body.session_ids` / `body.action` — actual schema field is `payload.ids` / `payload.action`. Reviewer P. | Renamed param to `payload`, loop iterates `payload.ids`, branch reads `payload.action`. Pre-existing code already uses this shape (sessions.py:742-747). |
| 2 | `from rr import _resolve_role` in `sessions.py` binds at import time → `tests/conftest.py:73` `monkeypatch.setattr(rr, "_resolve_role", patched)` doesn't propagate to `sessions._resolve_role`. Reviewers N.2, O.2. | **Switch to module-import.** Change `sessions.py` imports to `from gg_relay.api.dependencies import require_role as rr` and call `rr._resolve_role(request)` everywhere. The conftest patch then propagates naturally because attribute lookup happens at call time. Affects 3 pre-existing callsites (lines 342, 442, 740) PLUS the 2 new ones (`submit_session`, `batch_sessions`). Each change is a 1-line replacement. |

### Self-containment fix (Reviewer N.3, O.S2)

v4 left §2.4.4 batch retry and §3.1 test table as "Identical to v3". v5 inlines both so reviewers / implementers can work from v5 alone.

### Plan tidy-up

* Removed § "Carried unchanged from v3" sections — v5 is the canonical spec; cross-references replaced with inline content.
* Combined the §2 spec table with §7 execution checklist for single-pass implementation.

---

## 0. Problem statement

Today every authenticated user (admin or not) falls back to operator's `.env` `ANTHROPIC_API_KEY` when they have not configured per-user credentials. Audit trail does not distinguish "alice used her own key" from "alice silently spent operator quota". No enforcement knob exists.

## 1. Goals

* **G1.** Config flag `RELAY_REQUIRE_PER_USER_CREDENTIALS=true|false` (default `false`).
* **G2.** When `true` AND actor is non-admin AND merged credentials do NOT contain `ANTHROPIC_API_KEY` ∨ `ANTHROPIC_AUTH_TOKEN` (non-empty) → reject `400 missing_credentials`.
* **G3.** When `true` AND credentials store lookup itself raises → reject `503 credential_lookup_unavailable` (operator/infra problem). Header `Retry-After: 5`.
* **G4.** Body credential keys validated against `ALLOWED_ENV_NAMES` REGARDLESS of strict mode → unknown key → `400 unsupported_credential_key`.
* **G5.** WARN log on EVERY fallback (admin + non-admin), regardless of flag.
* **G6.** Explicit audit row on every rejection (`session_reject_missing_credentials` / `session_reject_lookup_unavailable`).
* **G7.** Admin escape hatch — admin actors retain fallback under strict mode.
* **G8.** Zero behavioural change when flag is unset. All existing tests, CLI, scripts unaffected.
* **G9.** Dashboard "New Session" form renders structured actionable error UI for both 400 codes.

## 2. Implementation (file-by-file, fully spelled out)

### 2.1 `src/gg_relay/config.py`

Add ONE field next to existing v3 credential fields (~line 410):

```python
require_per_user_credentials: bool = False
"""Strict-mode opt-in. Rejects non-admin sessions lacking
ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN with HTTP 400.
Admin retains fallback for operations.

Bedrock / Vertex deployments: leave this False. The upload
allowlist does not include CLAUDE_CODE_USE_BEDROCK /
CLAUDE_CODE_USE_VERTEX, so a non-admin cannot configure those
providers via /dashboard/me/credentials.

Default False preserves single-tenant behaviour.
"""
```

### 2.2 `src/gg_relay/session/manager.py`

#### 2.2.1 Bundle validator (module level)

```python
def _credential_bundle_is_complete(
    creds: Mapping[str, str],
) -> bool:
    """Returns True iff creds contains a complete Anthropic
    direct/proxy auth bundle (ANTHROPIC_API_KEY OR
    ANTHROPIC_AUTH_TOKEN, non-empty after strip).

    Empty / whitespace-only / non-str values count as absent.
    Defensive isinstance() guard tolerates in-process callers
    bypassing pydantic.

    Bedrock/Vertex deferred — those providers need
    CLAUDE_CODE_USE_BEDROCK / CLAUDE_CODE_USE_VERTEX which are
    not in the upload allowlist as of this plan.
    """
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        value = creds.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False
```

#### 2.2.2 Exception classes (module level)

```python
class MissingCredentialsError(Exception):
    """Strict mode rejected a non-admin session lacking a complete
    auth bundle. API → HTTP 400 missing_credentials."""

    def __init__(
        self,
        *,
        actor_label: str | None,
        actor_role: str | None,
    ) -> None:
        self.actor_label = actor_label
        self.actor_role = actor_role
        super().__init__(
            "non-admin actor requires per-user credentials "
            "(ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN); "
            "operator enabled RELAY_REQUIRE_PER_USER_CREDENTIALS"
        )


class CredentialsLookupUnavailable(Exception):
    """Strict mode hit a transient credentials-store failure.
    API → HTTP 503 credential_lookup_unavailable + Retry-After: 5."""

    def __init__(self, *, actor_label: str | None) -> None:
        self.actor_label = actor_label
        super().__init__(
            "user_credentials store lookup failed; refusing "
            "fallback under strict mode"
        )
```

#### 2.2.3 `SessionManager.__init__` adds one kwarg

```python
def __init__(
    self,
    *,
    # ...existing parameters...
    user_credentials_store: Any = None,
    require_per_user_credentials: bool = False,   # v5 — new
) -> None:
    # ...existing body...
    self._require_per_user_credentials = require_per_user_credentials
```

#### 2.2.4 `submit` — DB-lookup escalation + bundle enforcement

```python
async def submit(
    self,
    spec: SessionSpec,
    *,
    runtime_ctx: SessionRuntimeContext = _DEFAULT_RUNTIME_CTX,
    api_key_id: str | None = None,
    owner: str | None = None,
    actor_label: str | None = None,
    actor_role: str | None = None,           # v5 — new
    description: str | None = None,
    parent_session_id: str | None = None,
) -> str:
    if not self._accepting_new:
        raise RuntimeError(
            "SessionManager is shutting down; refusing new submit"
        )

    # v5 — DB lookup with strict-mode escalation
    if (
        self._user_credentials_store is not None
        and actor_label
    ):
        try:
            db_creds = await self._user_credentials_store.get_for_user(
                actor_label
            )
        except Exception as exc:
            logger.warning(
                "user_credentials lookup failed for actor=%s",
                actor_label, exc_info=True,
            )
            if self._require_per_user_credentials:
                raise CredentialsLookupUnavailable(
                    actor_label=actor_label
                ) from exc
            db_creds = {}
        if db_creds:
            merged = {**db_creds, **runtime_ctx.credentials}
            runtime_ctx = replace(runtime_ctx, credentials=merged)

    # v5 — bundle-based enforcement with provider-agnostic WARN
    if not _credential_bundle_is_complete(runtime_ctx.credentials):
        if actor_role == "admin":
            logger.warning(
                "admin actor=%r submitted session with no complete "
                "Anthropic auth bundle in merged credentials "
                "(ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN). "
                "Session will inherit host env via SDK subprocess.",
                actor_label,
            )
        else:
            logger.warning(
                "non-admin actor=%r role=%r submitted session with "
                "no complete Anthropic auth bundle in merged "
                "credentials (ANTHROPIC_API_KEY / "
                "ANTHROPIC_AUTH_TOKEN). Configure via "
                "/dashboard/me/credentials or set "
                "RELAY_REQUIRE_PER_USER_CREDENTIALS=true to reject. "
                "Bedrock/Vertex deployments: leave strict mode off.",
                actor_label, actor_role,
            )
            if self._require_per_user_credentials:
                raise MissingCredentialsError(
                    actor_label=actor_label,
                    actor_role=actor_role,
                )

    # ...existing sid + persistence + queue, unchanged...
```

#### 2.2.5 `retry` forwards `actor_role`

```python
async def retry(
    self,
    session_id: str,
    *,
    actor: str | None = None,
    actor_role: str | None = None,           # v5 — new
) -> str:
    # ...existing fetch + spec rebuild...
    return await self.submit(
        spec,
        runtime_ctx=runtime_ctx,
        api_key_id=api_key_id,
        owner=owner,
        actor_label=actor,
        actor_role=actor_role,               # v5 — forward
        description=description,
        parent_session_id=session_id,
    )
```

### 2.3 `src/gg_relay/api/dependencies/require_role.py`

**NO CHANGE.** v5 reuses the existing `_resolve_role` private symbol.

### 2.4 `src/gg_relay/api/routers/sessions.py`

#### 2.4.1 Imports (v5 — module-import for `_resolve_role`)

Replace the current import:

```python
# BEFORE (lines 19-24):
from gg_relay.api.dependencies.require_role import (
    ROLE_HIERARCHY,
    _resolve_role,           # ← removed in v5
    require_role,
    require_role_or_own_session,
)

# AFTER (v5):
from gg_relay.api.dependencies import require_role as _rr_mod
from gg_relay.api.dependencies.require_role import (
    ROLE_HIERARCHY,
    require_role,
    require_role_or_own_session,
)
```

Then update the 5 callsites (3 pre-existing + 2 new):

```python
# Pre-existing line 342:  role = _resolve_role(request)
# v5:                     role = _rr_mod._resolve_role(request)

# Pre-existing line 442:  current_role = _resolve_role(request)
# v5:                     current_role = _rr_mod._resolve_role(request)

# Pre-existing line 740:  role = _resolve_role(request)
# v5:                     role = _rr_mod._resolve_role(request)

# New in submit_session (this plan):
actor_role = _rr_mod._resolve_role(request)

# New in batch_sessions (this plan):
actor_role = _rr_mod._resolve_role(request)
```

The conftest patch `monkeypatch.setattr(rr, "_resolve_role", patched)` now propagates to `_rr_mod._resolve_role` because attribute lookup happens at call time, not import time. **Net effect**: existing tests that rely on the autouse admin-grant via empty `role_mapping` continue to work for the 3 pre-existing callsites AND for the 2 new ones. Strict-mode tests still configure `role_mapping` explicitly for predictable behaviour.

#### 2.4.2 Add `ALLOWED_ENV_NAMES` import + validator helper

```python
from gg_relay.api.routers.user_credentials import ALLOWED_ENV_NAMES
from gg_relay.session.manager import (
    # ...existing imports + new...
    CredentialsLookupUnavailable,
    MissingCredentialsError,
)


def _validate_body_credentials(creds: Mapping[str, str]) -> None:
    """Validate every key in body.credentials against the
    /me/credentials upload allowlist. Defence-in-depth: runs
    regardless of strict mode."""
    bad = [k for k in creds.keys() if k not in ALLOWED_ENV_NAMES]
    if bad:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "unsupported_credential_key",
                "error": "credential_key_not_allowed",
                "rejected_keys": sorted(bad),
                "allowed": sorted(ALLOWED_ENV_NAMES),
                "message": (
                    "request-body credentials contained "
                    "unsupported env-var name(s); allowed set "
                    "mirrors /api/v1/me/credentials uploads."
                ),
            },
        )
```

#### 2.4.3 `submit_session` — full revised body

```python
@router.post(
    "",                                       # v5 — NOT "/api/v1/sessions"
    response_model=SessionResponse,
    status_code=202,
    dependencies=[Depends(require_role("submitter"))],
    responses={
        202: {"description": "Session accepted"},
        400: {
            "description": (
                "missing_credentials (strict mode) | "
                "unsupported_credential_key | other SDK errors"
            ),
        },
        503: {
            "description": (
                "credential_lookup_unavailable — credentials "
                "store transient failure under strict mode"
            ),
        },
    },
)
async def submit_session(
    request: Request,
    body: SessionSubmitRequest,
    manager: SessionManager = ManagerDep,
    api_key_id: str | None = ApiKeyIdDep,
) -> JSONResponse:
    # v5 — validate body credentials BEFORE building runtime context
    _validate_body_credentials(body.credentials)

    spec = _build_spec(body)
    ctx = SessionRuntimeContext(
        credentials=dict(body.credentials),
        trace_id=body.trace_id or "",
    )
    owner = (
        body.owner
        or getattr(request.state, "api_key_label", None)
        or "anon"
    )
    description = body.description
    response_headers: dict[str, str] = {}
    if description is not None and len(description) > _DESCRIPTION_MAX_LEN:
        description = description[:_DESCRIPTION_MAX_LEN]
        response_headers["X-Description-Truncated"] = "true"

    # v5 — derive audit + actor metadata OUTSIDE try
    audit = getattr(request.app.state, "audit_service", None)
    actor_label = getattr(request.state, "api_key_label", None)
    actor_role = _rr_mod._resolve_role(request)

    try:
        sid = await manager.submit(
            spec,
            runtime_ctx=ctx,
            api_key_id=api_key_id,
            owner=owner,
            actor_label=actor_label,
            actor_role=actor_role,
            description=description,
        )
    except MissingCredentialsError as exc:
        if audit is not None:
            with contextlib.suppress(Exception):
                await audit.record(
                    actor=actor_label or "anon",
                    action="session_reject_missing_credentials",
                    target_type="session",
                    target_id="-",
                    metadata={
                        "role": actor_role,
                        "reason": "no_per_user_credentials",
                    },
                )
        raise HTTPException(
            status_code=400,
            detail={
                "code": "missing_credentials",
                "error": "per_user_credentials_required",
                "actor_label": exc.actor_label,
                "actor_role": exc.actor_role,
                "message": (
                    "This deployment requires per-user credentials "
                    "(ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN). "
                    "Configure at /dashboard/me/credentials, or "
                    "ask an admin to provision via "
                    "/dashboard/admin/credentials."
                ),
            },
        ) from exc
    except CredentialsLookupUnavailable as exc:
        if audit is not None:
            with contextlib.suppress(Exception):
                await audit.record(
                    actor=actor_label or "anon",
                    action="session_reject_lookup_unavailable",
                    target_type="session",
                    target_id="-",
                    metadata={
                        "reason": "credentials_store_unavailable",
                    },
                )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "credential_lookup_unavailable",
                "error": "credentials_store_transient_failure",
                "actor_label": exc.actor_label,
                "message": (
                    "User-credentials store is temporarily "
                    "unavailable. Retry; if persistent, check the "
                    "relay's database and Fernet-key health."
                ),
            },
            headers={"Retry-After": "5"},
        ) from exc
    except SDKError as exc:
        # ...existing branch — unchanged...
        raise HTTPException(
            status_code=exc.http_status,
            detail={
                "code": f"sdk_{exc.category}",
                "error_category": exc.category,
                "message": str(exc),
            },
        ) from exc
    except RuntimeError as exc:
        # ...existing branch — unchanged...
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    detail = await manager.get(sid)
    payload = SessionResponse(...)   # unchanged
    return JSONResponse(content=..., status_code=202, headers=response_headers)
```

#### 2.4.4 `batch_sessions` retry branch — fully spelled out (was "identical to v3" in v4)

```python
async def batch_sessions(
    request: Request,
    payload: BatchSessionRequest,                       # v5 — match existing param name
    manager: SessionManager = ManagerDep,
) -> BatchSessionResponse:
    audit = getattr(request.app.state, "audit_service", None)
    label = getattr(request.state, "api_key_label", None) or "anon"
    actor_role = _rr_mod._resolve_role(request)        # v5 — once

    items: list[BatchSessionItem] = []
    ok_count = 0
    error_count = 0

    for sid in payload.ids:                             # v5 fix — `ids`, not `session_ids`
        try:
            if payload.action == "cancel":
                # ...existing cancel branch unchanged...
                pass
            else:  # retry
                try:
                    new_sid = await manager.retry(
                        sid,
                        actor=label,
                        actor_role=actor_role,         # v5 — forward
                    )
                except MissingCredentialsError:
                    if audit is not None:
                        with contextlib.suppress(Exception):
                            await audit.record(
                                actor=label,
                                action="session_reject_missing_credentials",
                                target_type="session",
                                target_id=sid,
                                metadata={
                                    "role": actor_role,
                                    "reason": "no_per_user_credentials",
                                    "via": "batch_retry",
                                },
                            )
                    items.append(
                        BatchSessionItem(
                            id=sid,
                            status="error",
                            error_code="missing_credentials",
                            error_message=(
                                "Per-user credentials required for "
                                "retry; configure at "
                                "/dashboard/me/credentials."
                            ),
                        )
                    )
                    error_count += 1
                    continue
                except CredentialsLookupUnavailable:
                    if audit is not None:
                        with contextlib.suppress(Exception):
                            await audit.record(
                                actor=label,
                                action="session_reject_lookup_unavailable",
                                target_type="session",
                                target_id=sid,
                                metadata={
                                    "reason": "credentials_store_unavailable",
                                    "via": "batch_retry",
                                },
                            )
                    items.append(
                        BatchSessionItem(
                            id=sid,
                            status="error",
                            error_code="credential_lookup_unavailable",
                            error_message=(
                                "Credentials store transient "
                                "failure; retry shortly."
                            ),
                        )
                    )
                    error_count += 1
                    continue
                items.append(
                    BatchSessionItem(id=sid, status="ok", new_session_id=new_sid)
                )
                ok_count += 1
        # ...existing SessionNotFound / RetryConfigError / SDKError / Exception branches unchanged...
```

Both new `except` clauses precede the existing broad `except Exception` (sessions.py:843).

### 2.5 `src/gg_relay/api/main.py`

```python
manager = SessionManager(
    # ...existing kwargs...
    user_credentials_store=user_credentials_store,
    require_per_user_credentials=getattr(
        cfg, "require_per_user_credentials", False
    ),
)
```

### 2.6 `.env.example`

Append:

```bash
# ── Plan v5 — multi-tenant credential enforcement ──────────────────
#
# When true, non-admin actors that have NOT configured a complete
# Anthropic auth bundle (ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN)
# are rejected with HTTP 400 missing_credentials. Admin can still
# fall back to the host env for operations.
#
# A separate 503 credential_lookup_unavailable (Retry-After: 5) is
# returned if the encrypted credentials store itself errors —
# operator/infra issue, not user-attributable.
#
# Bedrock / Vertex: leave this false. The upload allowlist does not
# yet include CLAUDE_CODE_USE_BEDROCK / CLAUDE_CODE_USE_VERTEX.
#
# Independent of this flag: WARN log on every fallback;
# body.credentials keys ALWAYS validated against /me/credentials
# allowlist (defence-in-depth).
#
# RELAY_REQUIRE_PER_USER_CREDENTIALS=false
```

### 2.7 `src/gg_relay/dashboard/templates/new.html`

Replace the `hx-on::after-request` failure branch:

```html
hx-on::after-request="
  if (event.detail.successful) {
    try {
      const data = JSON.parse(event.detail.xhr.responseText);
      window.location.href = '/dashboard/sessions/' + encodeURIComponent(data.id);
    } catch (e) { /* fall through */ }
  } else {
    const errBox = document.getElementById('submit-error');
    errBox.style.display = 'block';
    let handled = false;
    try {
      const body = JSON.parse(event.detail.xhr.responseText);
      const code = body && body.detail && body.detail.code;
      if (code === 'missing_credentials') {
        errBox.innerHTML = (
          'Per-user credentials required. ' +
          '<a href=&quot;/dashboard/me/credentials&quot;>Configure now</a>.'
        );
        handled = true;
      } else if (code === 'credential_lookup_unavailable') {
        errBox.textContent = (
          'Credentials store temporarily unavailable. Retry shortly.'
        );
        handled = true;
      }
    } catch (e) { /* fall through */ }
    if (!handled) {
      errBox.textContent = 'Submit failed: ' +
        event.detail.xhr.status + ' ' +
        event.detail.xhr.responseText.slice(0, 400);
    }
  }
"
```

XSS surface: `errBox.innerHTML` ONLY receives our static server-controlled strings (the friendly message + static link). User-controlled data never reaches this branch.

### 2.8 `scripts/load_test.py` (v5 — env-var only, no Locust CLI integration)

Top of file:

```python
import os

# v5 — strict-mode operators set ANTHROPIC_API_KEY in the env BEFORE
# running locust. Single-tenant deployments and load tests against
# strict-OFF relays are unaffected (empty string → empty body creds).
CREDENTIALS_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
```

`_build_session_payload`:

```python
def _build_session_payload(label: str) -> dict[str, Any]:
    creds: dict[str, str] = {}
    if CREDENTIALS_KEY:
        creds["ANTHROPIC_API_KEY"] = CREDENTIALS_KEY
    return {
        "spec": {...},   # unchanged
        "credentials": creds,
    }
```

## 3. Test plan (fully spelled out)

### 3.1 `tests/integration/test_require_per_user_credentials.py` (~350 LOC, 13 cases)

Test fixtures construct `SessionManager(require_per_user_credentials=...)` directly AND set up an ASGI client with explicit `role_mapping` (NOT relying on conftest's empty-mapping admin-grant fallback).

| # | Test name | Setup | Assertion |
|---|-----------|-------|-----------|
| T1 | `test_default_off_preserves_fallback` | flag unset, non-admin actor (role_mapping configured as `submitter`), no DB creds, body `{}` | 202; no exception. |
| T2 | `test_strict_blocks_non_admin_without_any_creds` | flag `True`, non-admin (`submitter`), no DB, body `{}` | 400 `missing_credentials`; one audit row `action=session_reject_missing_credentials`. |
| T3 | `test_strict_allows_non_admin_with_db_api_key` | flag `True`, non-admin, DB row `{ANTHROPIC_API_KEY: sk-test}`, body `{}` | 202. |
| T4 | `test_strict_allows_non_admin_with_body_api_key` | flag `True`, non-admin, no DB, body `{ANTHROPIC_API_KEY: sk-test}` | 202. |
| T5 | `test_strict_allows_non_admin_with_auth_token` | flag `True`, non-admin, body `{ANTHROPIC_AUTH_TOKEN: tok}` | 202 (alt auth path). |
| T6 | `test_strict_blocks_non_admin_with_only_base_url` | flag `True`, non-admin, **DB rows for actor: NONE**, body `{ANTHROPIC_BASE_URL: https://proxy}` | 400 — base URL alone is NOT auth. |
| T7 | `test_strict_blocks_non_admin_with_only_aws_keys` | flag `True`, non-admin, body `{AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION}` only | 400 — Bedrock deferred. |
| T8 | `test_strict_allows_admin_without_creds_with_warn` | flag `True`, **admin** (role_mapping admin), no creds | 202; WARN "admin actor=... no complete Anthropic auth bundle". |
| T9 | `test_warn_emitted_for_non_admin_fallback_when_flag_off` | flag `False`, non-admin, no creds | 202 AND WARN containing "non-admin actor=... no complete". |
| T10 | `test_strict_blocks_empty_string_api_key` | flag `True`, non-admin, body `{ANTHROPIC_API_KEY: ""}` | 400 — empty counts as absent. |
| T11 | `test_batch_retry_inherits_strict_mode` | Pre-step: create retryable session under flag-off mode by admin. Then: flip flag `True` (rebuild manager), retry batch as non-admin without creds | per-item `error_code="missing_credentials"`; audit `via="batch_retry"`. |
| T12 | `test_store_failure_returns_503_under_strict_mode` | flag `True`, mock `UserCredentialsStore.get_for_user` with `AsyncMock(side_effect=Exception)` | 503 `credential_lookup_unavailable`; response header `Retry-After: 5`. |
| T13 | `test_store_failure_silent_under_soft_mode` | flag `False`, store raises | 202 (legacy preserved); WARN. |

### 3.2 `tests/integration/test_body_credentials_allowlist.py` (~80 LOC, 4 cases)

| # | Test name | Setup | Assertion |
|---|-----------|-------|-----------|
| TB1 | `test_unknown_body_key_rejected_400` | body `{LD_PRELOAD: /bad.so}`, mock manager via `app.dependency_overrides[get_manager]` | 400 `unsupported_credential_key`; `rejected_keys=["LD_PRELOAD"]`; `mock_manager.submit.assert_not_called()`. |
| TB2 | `test_unknown_body_key_rejected_even_when_strict_off` | flag `False`, body `{PATH: ...}` | 400 (defence-in-depth always on). |
| TB3 | `test_allowed_body_key_accepted` | body `{ANTHROPIC_API_KEY: sk-test}` | 202. |
| TB4 | `test_mixed_keys_one_bad_rejects_all` | body `{ANTHROPIC_API_KEY: sk-..., LD_PRELOAD: ...}` | 400; `mock_manager.submit.assert_not_called()` (good key not used). |

### 3.3 `tests/integration/test_router_passes_actor_role.py` (~80 LOC, 2 cases)

| # | Test name | Verifies |
|---|-----------|----------|
| TR1 | `test_submit_route_passes_actor_role_kwarg` | `app.dependency_overrides[get_manager] = lambda: mock_manager`; POST `/api/v1/sessions`; `mock_manager.submit.call_args.kwargs["actor_role"]` equals the value from `_rr_mod._resolve_role(request)`. |
| TR2 | `test_batch_retry_passes_actor_role_kwarg` | Same pattern for `manager.retry`. |

### 3.4 `tests/integration/test_user_credentials_dashboard.py` extensions

| # | Test name | Verifies |
|---|-----------|----------|
| TD1a | `test_new_session_template_contains_missing_creds_handler` | Read `src/gg_relay/dashboard/templates/new.html`. Assert the **exact JS branch** `code === 'missing_credentials'` is present. Assert `/dashboard/me/credentials` anchor present. |
| TD1b | `test_api_returns_missing_credentials_code_under_strict` | Strict mode `True`, dashboard cookie session, POST `/api/v1/sessions` with body lacking creds → JSON `detail.code == 'missing_credentials'`. |

### 3.5 OpenAPI snapshot

Regenerate `docs/openapi.snapshot.json` via `uv run python scripts/dump_openapi.py` AFTER all router changes are committed.

### 3.6 Conftest verification

* `tests/conftest.py:73` autouse fixture **untouched** by v5 — confirmed safe by the module-import refactor (§2.4.1).
* Confirm `tests/unit/api/test_require_role_dependency.py` passes unchanged.

## 4. Backward compatibility

| Aspect | v5 behaviour |
|--------|--------------|
| `require_per_user_credentials=False` default | Every existing test, lifespan, CLI, script: zero behavioural change. |
| `actor_role=None` default on `submit`/`retry` | Direct in-process callers unchanged. Enforcement only fires under strict mode + non-admin. |
| `_resolve_role` private name | Unchanged. Conftest patch works for all 5 callsites in sessions.py via module-import refactor. |
| `scripts/load_test.py` | Uses `ANTHROPIC_API_KEY` from env (same env var the relay itself reads). Strict-mode operators set it; single-tenant operators already have it set. |
| Body-credentials allowlist | NEW behaviour. Any caller posting `{"LD_PRELOAD": "..."}` will now 400. Intentional (security hole closure). |

## 5. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| R1 | Bedrock/Vertex strict-mode unsupported. | Documented as non-goal. Future plan extends allowlist. |
| R2 | Body-creds validation breaks legitimate caller. | Allowlist IS the threat model. |
| R3 | Bundle validator central point of policy. | Tests T2-T10 lock down each branch. |
| R4 | Audit-write failure. | `contextlib.suppress(Exception)` — proven pattern. |
| R5 | TD1a brittle to template renames. | Acceptable — both ends move together. |
| R6 | Module-import refactor touches 3 pre-existing callsites. | Each is a 1-line replacement; existing tests (which set explicit `role_mapping`) keep passing. The change ALSO closes a latent test-fixture/import-binding mismatch noted by Reviewers N+O on v4. |
| R7 | `scripts/load_test.py` env-var approach assumes operators run locust with `ANTHROPIC_API_KEY` set. | This is how the relay itself reads the key — same operator habit. |

## 6. Execution checklist

* [ ] **D.1** Add `require_per_user_credentials: bool = False` to `Config`.
* [ ] **D.2** Add `_credential_bundle_is_complete()` (with `isinstance(value, str)` guard) + `MissingCredentialsError` + `CredentialsLookupUnavailable` in `manager.py`.
* [ ] **D.3** `SessionManager.__init__` parameter + state.
* [ ] **D.4** `SessionManager.submit` / `retry` parameter + enforcement (DB-lookup escalation + bundle check + provider-agnostic dual WARN).
* [ ] **D.5** `routers/sessions.py` import refactor: drop `_resolve_role` from the named import, add `from gg_relay.api.dependencies import require_role as _rr_mod`. Update 3 pre-existing callsites (lines 342, 442, 740) to `_rr_mod._resolve_role(request)`.
* [ ] **D.6** Add `ALLOWED_ENV_NAMES` import + `_validate_body_credentials()` helper to `routers/sessions.py`.
* [ ] **D.7** Update `submit_session`: route path `@router.post("", ...)`, validate body creds, derive `audit` + `actor_role` outside try, catch new errors with audit-before-raise, declare `responses={400, 503}`.
* [ ] **D.8** Update `batch_sessions`: resolve `actor_role` once via `_rr_mod._resolve_role`, pass to retry, explicit except branches with audit before raise.
* [ ] **D.9** Wire `require_per_user_credentials` from config into `SessionManager(...)` in `main.py` lifespan.
* [ ] **D.10** `.env.example` update.
* [ ] **D.11** Update `dashboard/templates/new.html` `hx-on::after-request`.
* [ ] **D.12** `scripts/load_test.py`: add `CREDENTIALS_KEY = os.environ.get(...)` at top, wire into `_build_session_payload`.
* [ ] **D.13** Write `tests/integration/test_require_per_user_credentials.py` (T1–T13).
* [ ] **D.14** Write `tests/integration/test_body_credentials_allowlist.py` (TB1–TB4; TB1+TB4 with `assert_not_called`).
* [ ] **D.15** Write `tests/integration/test_router_passes_actor_role.py` (TR1–TR2).
* [ ] **D.16** Extend `tests/integration/test_user_credentials_dashboard.py` with TD1a (exact `code === 'missing_credentials'` grep) + TD1b.
* [ ] **D.17** Regenerate `docs/openapi.snapshot.json` (`uv run python scripts/dump_openapi.py`).
* [ ] **D.18** Confirm `tests/conftest.py:73` autouse fixture works for ALL `_rr_mod._resolve_role` callsites via the module-import refactor.
* [ ] **D.19** Full regression: `uv run pytest tests/ -q --no-cov`, `uv run ruff check src tests`, existing `make actor-label-audit`.
