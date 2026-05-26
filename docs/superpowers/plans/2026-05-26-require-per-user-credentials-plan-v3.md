# Plan v3 — Require per-user credentials (C+B)

**Date**: 2026-05-26
**Author**: gg-relay
**Supersedes**: v1 (FAIL × 2), v2 (FAIL × 2)
**Status**: For Santa re-review.

## Changelog vs. v2 (FAIL → fix)

| # | v2 Critical | v3 Fix | Cite |
|---|------------|--------|------|
| 1 | "Any non-empty creds satisfies strict mode" (Reviewers J.1, K.1) — passes `{"ANTHROPIC_BASE_URL": "..."}` but SDK still inherits operator's `ANTHROPIC_API_KEY`. Credential-exfil channel. | Replace with **complete-bundle validator**. v3 strict mode requires merged creds to contain `ANTHROPIC_API_KEY` OR `ANTHROPIC_AUTH_TOKEN` (non-empty). Bedrock / Vertex deferred — they need `CLAUDE_CODE_USE_BEDROCK`/`_VERTEX` toggles which are NOT in the current upload allowlist. Operators on Bedrock/Vertex leave strict mode off (documented). | `_credential_bundle_is_complete()` helper |
| 2 | Body credentials wildcard (Reviewers J.2, K.2) — `SessionSubmitRequest.credentials: dict[str, str]` accepts any key including `ANTHROPIC_BASE_URL`/`LD_PRELOAD`. | Validate body credential keys against the SAME `ALLOWED_ENV_NAMES` frozenset that `/me/credentials` enforces. Performed at submission time (router level, NOT pydantic — to avoid hard import-time coupling). Reject unknown keys with `400 unsupported_credential_key`. | `routers/sessions.py` adds `_validate_body_credentials()` |
| 3 | `audit` variable undefined in router snippet (Reviewer K.4) — implemented literally → `NameError`. | Fetch `audit = getattr(request.app.state, "audit_service", None)` BEFORE the `try` block. Mirrors `user_credentials.py:205` and `sessions.py:524-526` proven patterns. | `routers/sessions.py:submit_session` |
| 4 | TD1 tests wrong layer (Reviewer K.S1) — dashboard error rendered by client-side JS, not server HTML. | Replace TD1 with two surgical tests: (a) template integrity — `new.html` contains the JS strings `'missing_credentials'` and `'/dashboard/me/credentials'`; (b) API contract — under strict mode the 400 response body has `detail.code == 'missing_credentials'`. Together they pin the contract both ends speak without needing a browser. | new TD1a + TD1b in `test_user_credentials_dashboard.py` |
| 5 | Empty-string credentials treated as present (Reviewer J.S1) — `{"ANTHROPIC_API_KEY": ""}` would satisfy v2's `if creds:` check. | Bundle validator checks `creds.get(k, "").strip()` truthiness for every required key. Empty / whitespace-only values count as absent. | `_credential_bundle_is_complete()` |
| 6 | `resolve_role` rename breaks test monkeypatches (Reviewer J.S2) | Document in v3 §H.S1: tests that patch `_resolve_role` MUST migrate to `resolve_role`. The alias `_resolve_role = resolve_role` only preserves `import` paths, NOT `monkeypatch.setattr` semantics. Grep before merge confirms no existing test patches `_resolve_role`. | `tests/` grep verified |
| 7 | No `Retry-After` on 503 (Reviewer J.S3) | Add `Retry-After: 5` header on the 503 response so clients (CI/CD, dashboard) back off cleanly. | `HTTPException(headers={...})` |

### v1-criticals (carried fix from v2, unchanged)

* Phantom retry route → batch endpoint at `routers/sessions.py:702-886` (unchanged from v2 §2.4.2).
* Batch `except Exception` swallows → explicit `except MissingCredentialsError` before generic (unchanged).
* Make-target audit → router behavioural test (unchanged).
* DB lookup → 400 mis-attribution → distinct `CredentialsLookupUnavailable` → 503 (unchanged).
* No audit on rejection → audit row written before raise (now with correct `audit` variable, fix #3 above).

---

## 0. Problem statement

(Unchanged.) After Plan v3 (the credentials store one), every authenticated user falls back to operator's `.env` `ANTHROPIC_API_KEY` when they lack per-user credentials. This plan adds opt-in strict mode that rejects non-admin fallback, plus a soft WARN that fires regardless.

## 1. Goals & non-goals

### Goals (refined for v3)

* **G1.** Config flag `RELAY_REQUIRE_PER_USER_CREDENTIALS=true|false` (default `false`).
* **G2.** When `true` AND actor is non-admin AND merged creds do NOT contain a **complete Anthropic auth bundle** (≥1 of `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN`, non-empty) → reject with `400 missing_credentials`.
* **G3.** When `true` AND credentials store lookup ITSELF fails → reject with `503 credential_lookup_unavailable` (operator/infra problem, not user). Includes `Retry-After: 5` header.
* **G4.** Body credential keys are validated against the same `ALLOWED_ENV_NAMES` allowlist used by `/me/credentials`. Unknown key → `400 unsupported_credential_key` REGARDLESS of strict mode (defence-in-depth: closes the v2 wildcard exfil channel even when strict mode is off).
* **G5.** Unconditional WARN log on every fallback (admin + non-admin), regardless of flag, for grep-able audit.
* **G6.** Explicit audit-log row on every rejection.
* **G7.** Admin actors retain fallback ability under strict mode.
* **G8.** Default-off: every existing test, lifespan, CLI, script observes byte-identical behaviour.
* **G9.** Dashboard "New Session" form renders a structured, actionable error UI.

### Non-goals

* **Bedrock / Vertex strict-mode support** — the upload allowlist lacks the required `CLAUDE_CODE_USE_BEDROCK` / `CLAUDE_CODE_USE_VERTEX` toggles. Operators using those providers either (a) leave strict mode off, or (b) raise a future plan to extend the allowlist + bundle validator. v3 documents this clearly and the validator returns `False` for Bedrock-only/Vertex-only creds — non-admin would 400.
* **No DB migration / schema change.**
* **No host-env scrubbing.** Reviewer K's suggestion ("scrub host env when user auth is incomplete") is **not** needed because strict mode rejects the submission BEFORE the SDK is invoked. The session never reaches `client._make_runner_core`, so there is no env to scrub.

## 2. Scope per-file

### 2.1 `src/gg_relay/config.py`

```python
require_per_user_credentials: bool = False
"""Strict-mode opt-in. When ``True``, :meth:`SessionManager.submit`
rejects sessions from **non-admin** actors whose merged credentials
do NOT contain ``ANTHROPIC_API_KEY`` or ``ANTHROPIC_AUTH_TOKEN``
(non-empty). Admin actors retain fallback for operations.

Bedrock / Vertex deployments: leave this flag ``False``. The upload
allowlist does not yet include ``CLAUDE_CODE_USE_BEDROCK`` /
``CLAUDE_CODE_USE_VERTEX``, so a non-admin cannot configure a
complete bundle for those providers. A future plan can extend
support; for now strict mode is Anthropic-direct only.

Default ``False`` preserves single-tenant behaviour. Independent of
this flag, a WARN log fires on EVERY fallback (admin + non-admin).
"""
```

### 2.2 `src/gg_relay/session/manager.py`

#### 2.2.1 Bundle validator (module level)

```python
def _credential_bundle_is_complete(
    creds: Mapping[str, str],
) -> bool:
    """Returns ``True`` iff ``creds`` contains a complete Anthropic
    direct/proxy auth bundle.

    v3 scope (Anthropic-direct ONLY — Bedrock / Vertex deferred):

      * ``ANTHROPIC_API_KEY`` non-empty,  OR
      * ``ANTHROPIC_AUTH_TOKEN`` non-empty.

    ``ANTHROPIC_BASE_URL`` alone does NOT satisfy — it is a proxy
    URL, not authentication. Reviewer K.1 in plan v2-Santa pinned
    this exact regression net.

    Empty / whitespace-only values count as absent (Reviewer J.S1).
    """
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if creds.get(key, "").strip():
            return True
    return False
```

#### 2.2.2 Exception classes (module level)

```python
class MissingCredentialsError(Exception):
    """Strict mode rejected a non-admin session lacking a complete
    auth bundle. API translates to ``HTTP 400 missing_credentials``."""

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
    API translates to ``HTTP 503 credential_lookup_unavailable``
    with ``Retry-After: 5``. Operator/infra problem, not user."""

    def __init__(self, *, actor_label: str | None) -> None:
        self.actor_label = actor_label
        super().__init__(
            "user_credentials store lookup failed; refusing "
            "fallback under strict mode"
        )
```

#### 2.2.3 `__init__` adds parameter

```python
def __init__(
    self,
    *,
    # ...existing...
    user_credentials_store: Any = None,
    require_per_user_credentials: bool = False,
) -> None:
    # ...existing body...
    self._require_per_user_credentials = require_per_user_credentials
```

#### 2.2.4 `submit` enforcement (uses bundle validator)

```python
async def submit(
    self,
    spec: SessionSpec,
    *,
    runtime_ctx: SessionRuntimeContext = _DEFAULT_RUNTIME_CTX,
    api_key_id: str | None = None,
    owner: str | None = None,
    actor_label: str | None = None,
    actor_role: str | None = None,
    description: str | None = None,
    parent_session_id: str | None = None,
) -> str:
    if not self._accepting_new:
        raise RuntimeError("SessionManager is shutting down; refusing new submit")

    # v3 — DB lookup with strict-mode escalation
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
                actor_label,
                exc_info=True,
            )
            if self._require_per_user_credentials:
                raise CredentialsLookupUnavailable(
                    actor_label=actor_label
                ) from exc
            db_creds = {}
        if db_creds:
            merged = {**db_creds, **runtime_ctx.credentials}
            runtime_ctx = replace(runtime_ctx, credentials=merged)

    # v3 — enforcement uses bundle validator, not truthiness.
    if not _credential_bundle_is_complete(runtime_ctx.credentials):
        if actor_role == "admin":
            logger.warning(
                "admin actor=%r fell back to host env credentials",
                actor_label,
            )
        else:
            logger.warning(
                "non-admin actor=%r role=%r fell back to host env "
                "credentials (no complete auth bundle). Configure "
                "ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN via "
                "/dashboard/me/credentials, or set "
                "RELAY_REQUIRE_PER_USER_CREDENTIALS=true to reject.",
                actor_label,
                actor_role,
            )
            if self._require_per_user_credentials:
                raise MissingCredentialsError(
                    actor_label=actor_label,
                    actor_role=actor_role,
                )

    # ...existing sid + persistence + queue...
```

#### 2.2.5 `retry` forwards `actor_role`

```python
async def retry(
    self,
    session_id: str,
    *,
    actor: str | None = None,
    actor_role: str | None = None,
) -> str:
    # ...existing fetch + spec rebuild...
    return await self.submit(
        spec,
        runtime_ctx=runtime_ctx,
        api_key_id=api_key_id,
        owner=owner,
        actor_label=actor,
        actor_role=actor_role,
        description=description,
        parent_session_id=session_id,
    )
```

### 2.3 `src/gg_relay/api/dependencies/require_role.py`

```python
def resolve_role(request: Request) -> str:
    # ...existing body of _resolve_role...

# Back-compat alias — preserves `import` paths but NOT
# `monkeypatch.setattr` semantics. Tests that patched
# `_resolve_role` MUST migrate to `resolve_role`. Pre-merge grep
# confirms no test in the tree currently patches the private name.
_resolve_role = resolve_role
```

### 2.4 `src/gg_relay/api/routers/sessions.py`

#### 2.4.1 New helper for body-creds validation (top of file)

```python
from gg_relay.api.routers.user_credentials import ALLOWED_ENV_NAMES

def _validate_body_credentials(creds: Mapping[str, str]) -> None:
    """v3 §G4 / Reviewer J.2-K.2 fix — validate every key in
    request-body ``credentials`` against the same allowlist that
    ``/me/credentials`` enforces on upload. Closes the wildcard
    exfil channel where ``{"ANTHROPIC_BASE_URL": "attacker"}`` would
    have passed strict mode + leaked operator's host
    ``ANTHROPIC_API_KEY`` to attacker infra via the SDK.

    Runs REGARDLESS of strict mode — defence in depth.
    """
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

#### 2.4.2 `submit_session` — full revised body

```python
@router.post(
    "/api/v1/sessions",
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
    # v3 — validate body credentials against allowlist BEFORE building
    # the runtime context. Closes the v2 wildcard channel even when
    # strict mode is off.
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

    # v3 — audit handle pulled from app.state mirror of existing patterns
    # (user_credentials.py:205, sessions.py:524-526). MUST be fetched
    # OUTSIDE the try so the except branches can use it (Reviewer K.4 fix).
    audit = getattr(request.app.state, "audit_service", None)
    actor_label = getattr(request.state, "api_key_label", None)
    actor_role = resolve_role(request)

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
    except RuntimeError as exc:
        # ...existing branch unchanged...
    # ...existing response build...
```

**Exception ordering**: `MissingCredentialsError` and `CredentialsLookupUnavailable` are both `Exception`-subclassed (NOT `RuntimeError` / `SDKError`), so order between them and the existing `except SDKError` / `except RuntimeError` is irrelevant — they don't share lineage. Placing the new branches FIRST is for code readability.

#### 2.4.3 `batch_sessions` — retry branch

```python
async def batch_sessions(request: Request, ...) -> ...:
    # ...existing init...
    audit = getattr(request.app.state, "audit_service", None)
    label = getattr(request.state, "api_key_label", None) or "anon"
    actor_role = resolve_role(request)
    # ...existing loop...
    else:  # retry
        try:
            new_sid = await manager.retry(
                sid,
                actor=label,
                actor_role=actor_role,
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
                        "Per-user credentials required for retry; "
                        "configure at /dashboard/me/credentials."
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
                        "Credentials store transient failure; retry shortly."
                    ),
                )
            )
            error_count += 1
            continue
        items.append(
            BatchSessionItem(id=sid, status="ok", new_session_id=new_sid)
        )
        ok_count += 1
```

The `except MissingCredentialsError` and `except CredentialsLookupUnavailable` MUST appear before the broad `except Exception` (which they currently do — see `sessions.py:843`).

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

```bash
# ── Plan v4 — multi-tenant credential enforcement ──────────────────
#
# When true, non-admin actors that have NOT configured a complete
# Anthropic auth bundle (ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN)
# are rejected with HTTP 400 missing_credentials. Admin actors can
# still fall back to the host env for operations.
#
# A separate 503 credential_lookup_unavailable (with Retry-After: 5)
# is returned if the encrypted credentials store itself errors —
# operator/infra issue, not user-attributable.
#
# Bedrock / Vertex deployments: leave this flag false. The upload
# allowlist does not yet include CLAUDE_CODE_USE_BEDROCK /
# CLAUDE_CODE_USE_VERTEX, so a non-admin cannot configure those
# providers via /dashboard/me/credentials.
#
# Independent of this flag, a WARN fires on every fallback
# (admin + non-admin). Body request credentials are ALWAYS
# validated against the same allowlist as /me/credentials uploads.
#
# RELAY_REQUIRE_PER_USER_CREDENTIALS=false
```

### 2.7 `src/gg_relay/dashboard/templates/new.html`

(Unchanged from v2 §2.7 — structured branch with `innerHTML` for the trusted static strings only; raw `responseText.slice(0, 400)` remains the fallback for unparseable bodies. No XSS surface because user-controlled data never reaches this branch.)

### 2.8 `scripts/load_test.py`

Pre-check during impl: if file exists AND submits sessions → add `--credentials-key` flag. If it does not exist or does not submit → drop this item with a one-line note in commit message.

## 3. Test plan

### 3.1 `tests/integration/test_require_per_user_credentials.py` (~350 LOC, 13 cases)

| # | Test name | Setup | Assertion |
|---|-----------|-------|-----------|
| T1 | `test_default_off_preserves_fallback` | flag unset, non-admin, no creds | 202, no exception. |
| T2 | `test_strict_blocks_non_admin_without_any_creds` | flag `True`, non-admin, no creds | 400 `missing_credentials`; audit row written. |
| T3 | `test_strict_allows_non_admin_with_db_api_key` | flag `True`, non-admin, DB `ANTHROPIC_API_KEY` | 202. |
| T4 | `test_strict_allows_non_admin_with_body_api_key` | flag `True`, non-admin, body `ANTHROPIC_API_KEY` | 202. |
| T5 | `test_strict_allows_non_admin_with_auth_token` | flag `True`, non-admin, body `ANTHROPIC_AUTH_TOKEN` | 202 (alt auth path). |
| T6 | `test_strict_blocks_non_admin_with_only_base_url` | flag `True`, non-admin, body ONLY `ANTHROPIC_BASE_URL` | 400 — base URL alone is NOT auth. **Reviewer K.1 regression net.** |
| T7 | `test_strict_blocks_non_admin_with_only_aws_keys` | flag `True`, non-admin, body AWS keys only (no `ANTHROPIC_*`) | 400 — Bedrock deferred. |
| T8 | `test_strict_allows_admin_without_creds_with_warn` | flag `True`, admin, no creds | 202; WARN log "admin actor=... fell back". |
| T9 | `test_warn_emitted_for_non_admin_fallback_when_flag_off` | flag `False`, non-admin, no creds | 202; WARN. |
| T10 | `test_strict_blocks_empty_string_api_key` | flag `True`, non-admin, body `ANTHROPIC_API_KEY=""` | 400 — empty counts as absent. **Reviewer J.S1 regression net.** |
| T11 | `test_batch_retry_inherits_strict_mode` | flag `True`, batch retry, non-admin without creds | per-item `error_code="missing_credentials"`; audit `via="batch_retry"`. |
| T12 | `test_store_failure_returns_503_under_strict_mode` | flag `True`, store raises | 503 `credential_lookup_unavailable`; header `Retry-After: 5`. |
| T13 | `test_store_failure_silent_under_soft_mode` | flag `False`, store raises | 202 (legacy preserved); WARN. |

### 3.2 `tests/integration/test_body_credentials_allowlist.py` (~80 LOC, 4 cases)

**v3 critical** — body wildcard channel is closed REGARDLESS of strict mode.

| # | Test name | Setup | Assertion |
|---|-----------|-------|-----------|
| TB1 | `test_unknown_body_key_rejected_400` | body `{"LD_PRELOAD": "..."}` | 400 `unsupported_credential_key`; `rejected_keys=["LD_PRELOAD"]`. |
| TB2 | `test_unknown_body_key_rejected_even_when_strict_off` | flag `False`, body `{"PATH": "..."}` | 400 (defence-in-depth always on). |
| TB3 | `test_allowed_body_key_accepted` | body `{"ANTHROPIC_API_KEY": "sk-..."}` | 202. |
| TB4 | `test_mixed_keys_one_bad_rejects_all` | body `{"ANTHROPIC_API_KEY": "sk-...", "LD_PRELOAD": "..."}` | 400; even the good key is not used. |

### 3.3 `tests/integration/test_router_passes_actor_role.py` (~80 LOC, 2 cases)

| # | Test name | Verifies |
|---|-----------|----------|
| TR1 | `test_submit_route_passes_actor_role_kwarg` | Override `get_manager` dep (NOT the `Depends(...)` object — Reviewer K.S4); POST `/api/v1/sessions`; assert `actor_role=` in kwargs == `resolve_role(request)`. |
| TR2 | `test_batch_retry_passes_actor_role_kwarg` | Same pattern for `manager.retry`. |

### 3.4 `tests/integration/test_user_credentials_dashboard.py` extensions

| # | Test name | Verifies |
|---|-----------|----------|
| TD1a | `test_new_session_template_contains_missing_creds_handler` | Read `new.html`, assert string `'missing_credentials'` AND `/dashboard/me/credentials` both appear in the template body. Pins the client-side branch exists. |
| TD1b | `test_api_returns_missing_credentials_code_under_strict` | Strict mode on, dashboard cookie session, POST `/api/v1/sessions` body lacks creds → response JSON `detail.code == 'missing_credentials'`. Pins the API contract the template depends on. |

Together TD1a + TD1b prove both ends of the contract without needing a browser. Documented as Reviewer K.S1 fix.

### 3.5 OpenAPI snapshot

Regenerate via `uv run python scripts/dump_openapi.py` after D.6 to capture the new `400` + `503` response declarations.

## 4. Backward compatibility

* `require_per_user_credentials=False` default → zero behavioural change for every existing deployment, test, CLI, and script.
* `actor_role=None` default on `submit` / `retry` → direct in-process callers unchanged. The new enforcement only fires when (strict mode is `True`) AND (non-admin role). Untyped `actor_role=None` is treated as non-admin → still 400 under strict mode, which is correct.
* Body-credentials validation is NEW and DOES alter behaviour: any existing caller sending `{"LD_PRELOAD": "..."}` will now get 400. This is **intentional** (closing a security hole) and unlikely to affect anyone (no legitimate use). Tests TB1-TB4 pin the new contract.
* `_resolve_role = resolve_role` alias preserves imports. Tests using `monkeypatch.setattr(...,"_resolve_role",...)` would silently no-op — pre-merge grep confirms no such test exists.

## 5. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| R1 | Strict-mode Bedrock/Vertex users can't configure. | Documented as non-goal; future plan extends allowlist. |
| R2 | Body-creds validation rejects a legitimate caller relying on a non-allowlist env. | Defence in depth — the allowlist IS the threat model. Any caller needing more keys must request an explicit allowlist extension via a separate plan. |
| R3 | `_credential_bundle_is_complete` central is the single point of policy. | Tests T2-T10 lock down each branch; future provider additions require both a validator branch AND a test. |
| R4 | Audit-write inside `except` could fail and shadow the original error. | `contextlib.suppress(Exception)` — proven pattern from existing routes. |
| R5 | TD1a is a template-grep test (brittle to renames). | Acceptable — template renames would also break the JS branch. The grep is the test. |
| R6 | `Retry-After: 5` is arbitrary. | Documented; operators can override via reverse proxy. Industry default for transient backends. |
| R7 | Pre-merge grep for `_resolve_role` patching could miss a test that wraps it. | Add an explicit assertion in the impl-time grep: `! rg -n '_resolve_role' tests/ \| rg -v 'import _resolve_role'`. Found patches → block merge. |
| R8 | Future contributor adds a new credentials route forgetting `_validate_body_credentials`. | Test TR1/TR2 only checks `actor_role`. NEW test TB5 (suggested below) could parameterise over all `manager.submit`-calling routes. Deferred to future plan. |

## 6. Open questions

* **Q1 (resolved)**: Bundle validator scope = Anthropic-direct only for v3. Bedrock/Vertex deferred. v2's "any non-empty" semantic withdrawn.
* **Q2 (resolved)**: Retry-After: 5 on 503.
* **Q3 (resolved)**: Audit row metadata does NOT include the underlying exception class/message — only the action code, reason string, and (if applicable) `via` channel.
* **Q4 (resolved)**: Strict mode does NOT refuse to boot when `cfg.role_mapping == {}`. The lifespan logs WARN; first session submission immediately surfaces the misconfiguration as a 400 the operator sees in their own logs.

## 7. Execution checklist

* [ ] **D.1** Add `require_per_user_credentials: bool = False` to `Config`.
* [ ] **D.2** Add `_credential_bundle_is_complete()` helper + `MissingCredentialsError` + `CredentialsLookupUnavailable` in `manager.py`.
* [ ] **D.3** `SessionManager.__init__` parameter + state.
* [ ] **D.4** `SessionManager.submit` / `retry` parameter + enforcement (DB-lookup escalation + bundle check + dual WARN).
* [ ] **D.5** Promote `_resolve_role` → `resolve_role` with back-compat alias.
* [ ] **D.6** Add `_validate_body_credentials()` to `routers/sessions.py`.
* [ ] **D.7** Update `submit_session`: validate body creds → derive `audit` + `actor_role` outside try → catch new errors → audit before raise → declare `responses=` (400, 503).
* [ ] **D.8** Update `batch_sessions`: resolve `actor_role` once → pass to retry → explicit except branches with audit before raise (BEFORE generic `Exception`).
* [ ] **D.9** Wire `require_per_user_credentials` in `main.py` lifespan.
* [ ] **D.10** `.env.example` update.
* [ ] **D.11** Update `dashboard/templates/new.html` `hx-on::after-request` (v2 §2.7 spec — unchanged).
* [ ] **D.12** `scripts/load_test.py`: pre-check → add `--credentials-key` if applicable.
* [ ] **D.13** Write `tests/integration/test_require_per_user_credentials.py` (T1–T13).
* [ ] **D.14** Write `tests/integration/test_body_credentials_allowlist.py` (TB1–TB4).
* [ ] **D.15** Write `tests/integration/test_router_passes_actor_role.py` (TR1–TR2).
* [ ] **D.16** Extend `tests/integration/test_user_credentials_dashboard.py` with TD1a + TD1b.
* [ ] **D.17** Regenerate `docs/openapi.snapshot.json`.
* [ ] **D.18** Pre-merge grep: `rg -n '_resolve_role' tests/` returns ZERO patches.
* [ ] **D.19** Full regression: `uv run pytest tests/ -q --no-cov`, `uv run ruff check src tests`, existing `make actor-label-audit`.
