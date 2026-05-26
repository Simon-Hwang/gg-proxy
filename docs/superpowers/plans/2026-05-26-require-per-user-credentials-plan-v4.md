# Plan v4 — Require per-user credentials (C+B)

**Date**: 2026-05-26
**Author**: gg-relay
**Supersedes**: v1 (FAIL × 2), v2 (FAIL × 2), v3 (FAIL × 2)
**Status**: For Santa re-review.

## Changelog vs. v3 (FAIL → fix)

| # | v3 Critical | v4 Fix | Cite |
|---|------------|--------|------|
| 1 | Route path `@router.post("/api/v1/sessions")` would double-prefix to `/api/v1/sessions/api/v1/sessions` (router declares `prefix="/sessions"`, main mounts `prefix="/api/v1"`). Reviewers L.1, M.1. | Use `@router.post("", ...)` — the existing convention at `routers/sessions.py:126`. Same applies to the batch endpoint. | `routers/sessions.py:126` |
| 2 | `_resolve_role → resolve_role` rename silently breaks `tests/conftest.py:73` autouse `monkeypatch.setattr(rr, "_resolve_role", patched)`. Reviewers L.2, M.2. | **Drop the rename entirely.** The "private" prefix is convention, not enforcement — `from gg_relay.api.dependencies.require_role import _resolve_role` works fine. The new `sessions.py` callsite imports the existing private name. Conftest patch keeps working unchanged. Zero churn. | unchanged |
| 3 | `scripts/load_test.py` posts `"credentials": {}` — under strict mode would 400. Reviewer M.3. | Resolved IN-PLAN: Add optional `--credentials-key=<value>` argparse flag (default `os.environ.get("ANTHROPIC_API_KEY")`). When set, `_build_session_payload` includes `{"ANTHROPIC_API_KEY": value}`. Empty default = legacy behaviour. Documented: strict-mode operators must either run as admin OR pass `--credentials-key`. | `scripts/load_test.py:52-63` |
| 4 | WARN copy "fell back to host env credentials" is misleading when alice has AWS keys (the strict mode skips Bedrock — see §1 non-goals). Reviewer L.S1. | WARN message v4: `"actor=%r role=%r submitted session with no complete Anthropic auth bundle in merged credentials"`. Provider-agnostic. Bedrock/Vertex operators reading their logs see precisely what's missing. | `manager.submit` WARN strings |
| 5 | `_credential_bundle_is_complete` could `AttributeError` on `None.strip()` if a programmatic caller bypasses pydantic. Reviewer L.S2, M.S4. | Defensive `isinstance(value, str)` guard inside the helper. Pydantic still rejects `None` at the API boundary; the guard protects in-process tests / future programmatic clients. | helper body |
| 6 | T6 setup ambiguous — could share state with T3 if both share a DB. Reviewer M.S1. | T6 setup explicitly states "DB rows: NONE; body: only `ANTHROPIC_BASE_URL`". Plan §3.1 spelled out. | T6 row |
| 7 | TB1 should assert manager is NOT called. Reviewer M.S2. | TB1 verifies via dependency override: `mock_manager.submit.assert_not_called()` after the 400. | TB1 row |
| 8 | TD1a grep too loose — both strings could appear independently. Reviewer M.S3. | TD1a greps for the EXACT JS branch substring: `code === 'missing_credentials'`. Pins the structural integration, not just the words. | TD1a row |

### Carried unchanged from v3

* Bundle validator scope = Anthropic-direct only (`ANTHROPIC_API_KEY` ∨ `ANTHROPIC_AUTH_TOKEN`). Bedrock/Vertex deferred.
* `_validate_body_credentials()` enforcing `ALLOWED_ENV_NAMES` on body credentials, defence-in-depth (always on).
* Distinct `MissingCredentialsError` (400) vs `CredentialsLookupUnavailable` (503 + `Retry-After: 5`).
* `audit` fetched from `request.app.state.audit_service` BEFORE the try block.
* Two-line WARN (admin + non-admin) on every fallback.
* TD1 split: TD1a (template grep) + TD1b (API contract).

---

## 0. Problem statement

(Unchanged from v3.) Every authenticated user falls back to operator's `.env` `ANTHROPIC_API_KEY` when they lack per-user credentials. This plan adds opt-in strict mode rejecting non-admin fallback, plus an unconditional soft WARN.

## 1. Goals & non-goals

(Identical to v3 §1 — bundle scope reaffirmed Anthropic-only.)

## 2. Scope per-file

### 2.1 `src/gg_relay/config.py`

(Identical to v3 §2.1 — `require_per_user_credentials: bool = False` with Bedrock/Vertex caveat.)

### 2.2 `src/gg_relay/session/manager.py`

#### 2.2.1 Bundle validator (v4 — defensive guard)

```python
def _credential_bundle_is_complete(
    creds: Mapping[str, str],
) -> bool:
    """Returns ``True`` iff ``creds`` contains a complete Anthropic
    direct/proxy auth bundle.

    v4 scope (Anthropic-direct ONLY — Bedrock/Vertex deferred):

      * ``ANTHROPIC_API_KEY`` non-empty,  OR
      * ``ANTHROPIC_AUTH_TOKEN`` non-empty.

    ``ANTHROPIC_BASE_URL`` alone does NOT satisfy — it is a proxy
    URL, not authentication.

    Empty / whitespace-only values count as absent. Defensive
    ``isinstance(value, str)`` guard tolerates in-process callers
    that bypass pydantic with a non-str value (e.g. ``None`` from
    a mis-typed test fixture).
    """
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        value = creds.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False
```

#### 2.2.2 Exception classes

(Identical to v3 §2.2.2.)

#### 2.2.3 `__init__` parameter

(Identical to v3 §2.2.3.)

#### 2.2.4 `submit` — refined WARN copy

```python
async def submit(self, spec, *, runtime_ctx=..., ..., actor_label=None, actor_role=None, ...):
    if not self._accepting_new:
        raise RuntimeError("SessionManager is shutting down; refusing new submit")

    # v4 — DB lookup with strict-mode escalation (unchanged from v3)
    if self._user_credentials_store is not None and actor_label:
        try:
            db_creds = await self._user_credentials_store.get_for_user(actor_label)
        except Exception as exc:
            logger.warning(
                "user_credentials lookup failed for actor=%s",
                actor_label, exc_info=True,
            )
            if self._require_per_user_credentials:
                raise CredentialsLookupUnavailable(actor_label=actor_label) from exc
            db_creds = {}
        if db_creds:
            merged = {**db_creds, **runtime_ctx.credentials}
            runtime_ctx = replace(runtime_ctx, credentials=merged)

    # v4 — bundle-based enforcement with provider-agnostic WARN copy
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
                "RELAY_REQUIRE_PER_USER_CREDENTIALS=true to reject.",
                actor_label, actor_role,
            )
            if self._require_per_user_credentials:
                raise MissingCredentialsError(
                    actor_label=actor_label,
                    actor_role=actor_role,
                )

    # ...existing sid + persistence + queue...
```

#### 2.2.5 `retry` forwards `actor_role`

(Identical to v3 §2.2.5.)

### 2.3 `src/gg_relay/api/dependencies/require_role.py`

**NO CHANGE.** v4 drops the rename. `_resolve_role` stays private-named; new callers import it directly. The existing `tests/conftest.py:73` autouse fixture keeps working unchanged.

### 2.4 `src/gg_relay/api/routers/sessions.py`

#### 2.4.1 Imports

```python
# Existing v3 import — keep as-is.
from gg_relay.api.dependencies.require_role import (
    ROLE_HIERARCHY,
    _resolve_role,         # v4 — reuse the existing private name
    require_role,
    require_role_or_own_session,
)
from gg_relay.api.routers.user_credentials import ALLOWED_ENV_NAMES
from gg_relay.session.manager import (
    # ...existing...
    CredentialsLookupUnavailable,
    MissingCredentialsError,
)
```

#### 2.4.2 `_validate_body_credentials` helper (identical to v3 §2.4.1)

```python
def _validate_body_credentials(creds: Mapping[str, str]) -> None:
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

#### 2.4.3 `submit_session` (v4 — route path fixed)

```python
@router.post(
    "",                              # v4 fix — NOT "/api/v1/sessions"
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
    # v4 — validate body credentials BEFORE building runtime context
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

    # v4 — audit + actor_role resolved OUTSIDE try (proven pattern)
    audit = getattr(request.app.state, "audit_service", None)
    actor_label = getattr(request.state, "api_key_label", None)
    actor_role = _resolve_role(request)

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
        # ...existing branch unchanged...
        raise HTTPException(...) from exc
    except RuntimeError as exc:
        # ...existing branch unchanged...
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # ...existing response build...
```

#### 2.4.4 `batch_sessions` retry branch

(Identical to v3 §2.4.3 — already uses `else: # retry` and the existing `await manager.retry(sid, actor=label)` call shape. v4 adds `actor_role=actor_role` and the explicit `except MissingCredentialsError` / `except CredentialsLookupUnavailable` branches BEFORE the broad `except Exception`.)

### 2.5 `src/gg_relay/api/main.py`

(Identical to v3 §2.5 — wire `require_per_user_credentials` from config into `SessionManager(...)` ctor.)

### 2.6 `.env.example`

(Identical to v3 §2.6.)

### 2.7 `src/gg_relay/dashboard/templates/new.html`

(Identical to v3 §2.7 — `hx-on::after-request` parses JSON, switches on `detail.code`, renders structured banner with anchor.)

### 2.8 `scripts/load_test.py` (v4 — resolved in-plan)

```python
# Top of file additions
import os
import argparse

# In the argparse setup (or via env-var read at module top)
parser.add_argument(
    "--credentials-key",
    default=os.environ.get("ANTHROPIC_API_KEY", ""),
    help=(
        "ANTHROPIC_API_KEY value forwarded in body.credentials. "
        "Defaults to the process env var. Empty value uses an "
        "empty credentials dict (legacy behaviour). Required when "
        "the relay is in RELAY_REQUIRE_PER_USER_CREDENTIALS=true "
        "mode and the load-test API key is not admin-roled."
    ),
)

# In _build_session_payload — replace the static "credentials": {}
def _build_session_payload(label: str) -> dict[str, Any]:
    creds: dict[str, str] = {}
    if CREDENTIALS_KEY:   # module-level config from argparse
        creds["ANTHROPIC_API_KEY"] = CREDENTIALS_KEY
    return {
        "spec": {...},   # unchanged
        "credentials": creds,
    }
```

**Migration note for operators**: existing load-test invocations work unchanged when `ANTHROPIC_API_KEY` is in their shell env (the common case). Strict-mode operators either invoke with `--credentials-key=$KEY` or run load tests under an admin API key.

## 3. Test plan

### 3.1 `tests/integration/test_require_per_user_credentials.py` (~350 LOC)

(Identical to v3 §3.1 — 13 cases. **T6 spelled out explicitly**: `setup: DB rows for actor=NONE; body.credentials = {"ANTHROPIC_BASE_URL": "https://proxy"}`. Assertion: 400 + audit row.)

### 3.2 `tests/integration/test_body_credentials_allowlist.py` (~80 LOC)

**TB1 v4 update** — explicit "manager NOT called" assertion:

```python
async def test_unknown_body_key_rejected_400(...):
    mock_manager = AsyncMock()
    app.dependency_overrides[get_manager] = lambda: mock_manager
    response = await client.post(
        "/api/v1/sessions",
        json={"spec": {...}, "credentials": {"LD_PRELOAD": "/bad.so"}},
        headers={"X-API-Key": "k1"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "unsupported_credential_key"
    assert response.json()["detail"]["rejected_keys"] == ["LD_PRELOAD"]
    mock_manager.submit.assert_not_called()  # v4 — Reviewer M.S2 fix
```

TB2, TB3, TB4 unchanged from v3.

### 3.3 `tests/integration/test_router_passes_actor_role.py`

(Identical to v3 §3.3 — TR1 + TR2 via `app.dependency_overrides[get_manager]`.)

### 3.4 Dashboard credentials test extensions

**TD1a v4 update** — exact JS branch grep:

```python
def test_new_session_template_contains_missing_creds_handler():
    template_path = Path(__file__).parent.parent.parent / (
        "src/gg_relay/dashboard/templates/new.html"
    )
    body = template_path.read_text()
    # v4 — Reviewer M.S3 — pin the EXACT JS branch, not just two
    # words that could co-occur incidentally elsewhere.
    assert "code === 'missing_credentials'" in body, (
        "new.html must contain the JS branch that handles the "
        "400 missing_credentials response with an actionable link"
    )
    assert "/dashboard/me/credentials" in body, (
        "new.html must contain the anchor target for the "
        "structured error banner"
    )
```

TD1b unchanged from v3 (`detail.code == "missing_credentials"` assertion).

### 3.5 OpenAPI snapshot

(Identical to v3 §3.5 — regenerate via `uv run python scripts/dump_openapi.py`.)

### 3.6 Conftest verification

NEW v4 — explicit step to verify the `tests/conftest.py:73` autouse fixture still works:

* No change to conftest required.
* Confirm by running `tests/unit/api/test_require_role_dependency.py` (which depends on the fixture) — must pass unchanged.

## 4. Backward compatibility

(Identical to v3 §4. The `_resolve_role` private name is preserved; conftest patch unaffected.)

## 5. Risks & mitigations

(Identical to v3 §5 minus R7 — the false-grep risk is removed by NOT renaming.)

| Risk | Mitigation |
|------|------------|
| R1 | Bedrock/Vertex users can't use strict mode. | Documented. Future plan extends allowlist + bundle validator. |
| R2 | Body-creds validation rejects legitimate caller. | Allowlist IS the threat model; extensions via separate plan. |
| R3 | Bundle validator central point. | Tests T2-T10 lock down each branch. |
| R4 | Audit-write fails. | `contextlib.suppress(Exception)`. |
| R5 | TD1a brittle to template renames. | Acceptable — both ends of the contract move together. |
| R6 | `Retry-After: 5` arbitrary. | Documented; operators override via reverse proxy. |
| R7 | (removed — no rename) | — |
| R8 | Future contributor adds new credentials route forgetting validator. | TR1/TR2 only check `actor_role`; future plan adds parameterised "all submit-calling routes" test. Deferred. |
| R9 | `scripts/load_test.py` migration disturbs CI. | Default reads env var → CI unchanged. Strict-mode operators see the documented flag. |

## 6. Open questions

(All resolved in v3.)

## 7. Execution checklist

* [ ] **D.1** Add `require_per_user_credentials: bool = False` to `Config`.
* [ ] **D.2** Add `_credential_bundle_is_complete()` (with `isinstance(value, str)` guard) + `MissingCredentialsError` + `CredentialsLookupUnavailable` in `manager.py`.
* [ ] **D.3** `SessionManager.__init__` parameter + state.
* [ ] **D.4** `SessionManager.submit` / `retry` parameter + enforcement (DB-lookup escalation + bundle check + provider-agnostic dual WARN).
* [ ] **D.5** ~~`_resolve_role` rename~~ **DROPPED.**
* [ ] **D.6** Add `_validate_body_credentials()` to `routers/sessions.py`. Import `ALLOWED_ENV_NAMES`.
* [ ] **D.7** Update `submit_session`: validate body creds → derive `audit` + `actor_role` outside try → catch new errors → audit before raise → declare `responses=`. **Route path: `@router.post("", ...)`** — NOT `/api/v1/sessions`.
* [ ] **D.8** Update `batch_sessions`: resolve `actor_role` once → pass to retry → explicit except branches with audit BEFORE generic `Exception`.
* [ ] **D.9** Wire `require_per_user_credentials` in `main.py` lifespan.
* [ ] **D.10** `.env.example` update.
* [ ] **D.11** Update `dashboard/templates/new.html` `hx-on::after-request`.
* [ ] **D.12** `scripts/load_test.py`: add `--credentials-key` argparse flag, default to `os.environ.get("ANTHROPIC_API_KEY")`, wire into `_build_session_payload`.
* [ ] **D.13** Write `tests/integration/test_require_per_user_credentials.py` (T1–T13).
* [ ] **D.14** Write `tests/integration/test_body_credentials_allowlist.py` (TB1–TB4; **TB1 with `assert_not_called`**).
* [ ] **D.15** Write `tests/integration/test_router_passes_actor_role.py` (TR1–TR2).
* [ ] **D.16** Extend `tests/integration/test_user_credentials_dashboard.py` with TD1a (**exact `code === 'missing_credentials'` grep**) + TD1b.
* [ ] **D.17** Regenerate `docs/openapi.snapshot.json`.
* [ ] **D.18** Confirm `tests/conftest.py:73` autouse fixture untouched + `tests/unit/api/test_require_role_dependency.py` passes.
* [ ] **D.19** Full regression: `uv run pytest tests/ -q --no-cov`, `uv run ruff check src tests`, existing `make actor-label-audit`.
