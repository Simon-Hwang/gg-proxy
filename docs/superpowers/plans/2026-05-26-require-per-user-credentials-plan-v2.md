# Plan v2 — Require per-user credentials (C+B)

**Date**: 2026-05-26
**Author**: gg-relay
**Supersedes**: Plan v1 (2026-05-26-require-per-user-credentials-plan-v1.md)
**Status**: For Santa re-review.

## Changelog vs. v1 (FAIL → fix)

| # | v1 Critical | v2 Fix | Cite |
|---|------------|--------|------|
| 1 | Phantom `/sessions/{sid}/retry` route (Reviewer H.1) | Retry path is `POST /sessions/batch` → `manager.retry(sid, actor=label)` at `routers/sessions.py:806`. D.5 now targets the batch endpoint. | `routers/sessions.py:702-886` |
| 2 | Batch `except Exception` swallows `MissingCredentialsError` → `internal_error` (Reviewer H.2) | Add explicit `except MissingCredentialsError` branch BEFORE `except Exception` in `batch_sessions`, surfaced as per-item `error_code="missing_credentials"`. | `routers/sessions.py:843` |
| 3 | Recognised-key set too narrow (Reviewer H.3) — only Anthropic auth; Bedrock/Vertex users blocked. | **Switched semantic**: strict mode now requires **any non-empty merged `runtime_ctx.credentials`** (i.e. at least one allowlist entry uploaded), not a hard-coded auth-key set. Drops `_UPSTREAM_AUTH_KEYS`. Documents the trade-off: operator commits to "upload-anything-and-we-pass" rather than the manager second-guessing per-mode auth combinations. | `api/routers/user_credentials.py:_validate_env_name` allowlist |
| 4 | Fragile Make-target audit (Reviewer H.4) | Drop `require-creds-audit` Make target. Replace with a single integration test that constructs a real `TestClient`, mocks `manager.submit` / `manager.retry`, and asserts `actor_role=` appears in the kwargs. Survives refactors that grep misses (multiline calls, decorator-mediated). | new `tests/integration/test_router_passes_actor_role.py` |
| 5 | DB-lookup failure → silently `db_creds={}` → 400 mis-attributes to user (Reviewer I.1) | Introduce a SEPARATE error class `CredentialsLookupUnavailable`. When strict mode is on AND `user_credentials_store.get_for_user()` raises, propagate this distinct error → API returns `503 credential_lookup_unavailable` (NOT 400). Soft-mode behaviour is unchanged (still WARN + empty dict). | `manager.submit` lookup try/except block |
| 6 | No audit row on rejection (Reviewer I.2) | Router's `except MissingCredentialsError` branch writes `audit.record(action="session_reject_missing_credentials", actor=label, target_type="session", target_id="-", metadata={"role": actor_role, "reason": "...", "request_id": rid})` BEFORE raising HTTPException. Same pattern for `CredentialsLookupUnavailable` → `action="session_reject_lookup_unavailable"`. Batch path writes one row per rejected item. | mirrors `routers/sessions.py:524-528` star/unstar audit pattern |
| 7 | Dashboard `new.html` JS dumps raw responseText (Reviewer I.3) | `hx-on::after-request` parses JSON; when `data.detail.code === "missing_credentials"` renders a structured banner with anchor link to `/dashboard/me/credentials`. Same handling for `lookup_unavailable` (different copy: "transient — retry"). Fall-through to existing raw-text path on parse failure. | `dashboard/templates/new.html:70-81` |

### Additional fixes folded from suggestions

* **I.S1**: Test T5 was inconsistent with code (admin WARN claimed but not implemented). v2 implements WARN on **every** fallback (admin + non-admin) but only **rejects** non-admin under strict mode. Two distinct log messages so operators can filter.
* **I.S4**: Declare `responses={400: ..., 503: ...}` on the `POST /sessions` and `POST /sessions/batch` routes; regenerate `docs/openapi.snapshot.json` as part of D.10. Plan v1 omitted this.
* **H.S1**: Promote `_resolve_role` → public `resolve_role` in `api/dependencies/require_role.py`. Keep `_resolve_role = resolve_role` alias so any existing private-import keeps working. One-line addition.
* **I.S3** (`scripts/load_test.py`): Add a `--credentials-key` flag (optional) that defaults to `os.environ.get("ANTHROPIC_API_KEY")`. Pre-existing default behaviour unchanged when strict mode is off. Strict-mode operators run `--credentials-key=$KEY` to keep load tests working.

---

## 0. Problem statement

(Same as v1.) After Plan v3, every authenticated user falls back to the operator's `.env` `ANTHROPIC_API_KEY` when they lack per-user credentials. This plan adds (C) an opt-in strict mode that rejects non-admin fallback and (B) an unconditional WARN observability signal.

## 1. Goals & non-goals

### Goals

* **G1.** Config flag `RELAY_REQUIRE_PER_USER_CREDENTIALS=true|false` (default `false`).
* **G2.** When `true` AND actor is non-admin AND `runtime_ctx.credentials` is empty after merge → reject with `400 missing_credentials` carrying actionable detail.
* **G3.** When `true` AND the credentials store lookup itself FAILS → reject with `503 credential_lookup_unavailable` (distinct error — failure is operator/infra, not user).
* **G4.** Unconditional WARN log on EVERY fallback (admin + non-admin), regardless of the flag, for grep-able audit.
* **G5.** Explicit audit-log row on every rejection (`session_reject_missing_credentials` / `session_reject_lookup_unavailable`) with actor / role / reason / request_id.
* **G6.** Admin actors retain fallback ability under strict mode (operations / incident response).
* **G7.** Default-off: every existing test, every existing deployment that does not set the flag, every CLI / script invocation observes byte-identical behaviour.
* **G8.** Dashboard "New Session" form renders a structured, actionable error UI when rejected — not a raw responseText dump.

### Non-goals

* No new DB schema or migration.
* No change to the upload allowlist.
* No change to the SDK env merge order in `client.py`.
* No deletion of the existing `os.environ` fallback.
* No deduplication / rate-limiting of the WARN log (out of scope; operators tune logger).

## 2. Scope per-file (additive only)

### 2.1 `src/gg_relay/config.py`

Add ONE field next to the existing v3 credential fields (line ~410):

```python
require_per_user_credentials: bool = False
"""Strict-mode opt-in for multi-tenant deployments.

When ``True``, :meth:`SessionManager.submit` rejects sessions from
**non-admin** actors whose merged ``runtime_ctx.credentials`` is
empty (the actor has neither configured ``/dashboard/me/credentials``
nor supplied credentials in the API body). Admin actors retain the
fallback path for operations / incident response.

Default ``False`` preserves single-tenant behaviour: every existing
deployment that does not set ``RELAY_REQUIRE_PER_USER_CREDENTIALS``
sees identical behaviour. When ``True``, operators must ensure each
dashboard user has either configured ``/dashboard/me/credentials``
or that an admin has provisioned via ``/admin/credentials``.

Independent of this flag, a WARN log line fires on EVERY fallback
(admin + non-admin) so operators can detect drift even without
enforcement. Two distinct messages let operators filter:

  * ``"non-admin actor=... fell back to host env"``
  * ``"admin actor=... fell back to host env"``
"""
```

### 2.2 `src/gg_relay/session/manager.py`

#### 2.2.1 New exception classes (module level, near other domain errors)

```python
class MissingCredentialsError(Exception):
    """Strict mode rejected a non-admin session lacking per-user creds.

    API layer translates to HTTP ``400 missing_credentials``.
    Distinct from :class:`CredentialsLookupUnavailable` so the API
    layer can pick the correct status code AND audit action.
    """

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
            "(operator enabled RELAY_REQUIRE_PER_USER_CREDENTIALS)"
        )


class CredentialsLookupUnavailable(Exception):
    """Strict mode hit a transient credentials-store failure.

    API layer translates to HTTP ``503 credential_lookup_unavailable``.
    NOT user-attributable; operator/infra problem. Keeps the WARN
    semantics of soft-mode unchanged (still ``db_creds={}`` +
    WARN log) — only strict mode escalates to this exception.
    """

    def __init__(self, *, actor_label: str | None) -> None:
        self.actor_label = actor_label
        super().__init__(
            "user_credentials store lookup failed; refusing "
            "fallback under strict mode"
        )
```

#### 2.2.2 `__init__` adds ONE parameter

```python
def __init__(
    self,
    *,
    # ...existing parameters...
    user_credentials_store: Any = None,
    require_per_user_credentials: bool = False,
) -> None:
    # ...existing body...
    self._require_per_user_credentials = require_per_user_credentials
```

#### 2.2.3 `submit` accepts `actor_role`, enforces, escalates DB failures

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

    # Plan v3 §B.6 — merge per-user DB credentials BEFORE persistence.
    # v4 §3: under STRICT mode, escalate lookup failures to a distinct
    # error class so the API returns 503 (operator/infra) rather than
    # 400 (mis-attributing to user). Soft mode keeps the legacy
    # silently-empty behaviour.
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

    # Plan v4 §2 — strict-mode enforcement + soft-mode observability.
    # Fires on EVERY fallback (admin + non-admin) for audit; only
    # REJECTS non-admin under strict mode.
    if not runtime_ctx.credentials:
        if actor_role == "admin":
            logger.warning(
                "admin actor=%r fell back to host env credentials",
                actor_label,
            )
            # admin always allowed — operator escape hatch.
        else:
            logger.warning(
                "non-admin actor=%r role=%r fell back to host env "
                "credentials. Configure via /dashboard/me/credentials "
                "or set RELAY_REQUIRE_PER_USER_CREDENTIALS=true to "
                "reject.",
                actor_label,
                actor_role,
            )
            if self._require_per_user_credentials:
                raise MissingCredentialsError(
                    actor_label=actor_label,
                    actor_role=actor_role,
                )

    # ...existing rest of submit (sid, persistence, queue)...
```

#### 2.2.4 `retry` forwards `actor_role` symmetrically

```python
async def retry(
    self,
    session_id: str,
    *,
    actor: str | None = None,
    actor_role: str | None = None,
) -> str:
    # ...existing fetch+rebuild spec...
    return await self.submit(
        spec,
        runtime_ctx=runtime_ctx,
        api_key_id=api_key_id,
        owner=owner,
        actor_label=actor,
        actor_role=actor_role,   # ← v4: forward
        description=description,
        parent_session_id=session_id,
    )
```

### 2.3 `src/gg_relay/api/dependencies/require_role.py`

Promote helper to public name with back-compat alias:

```python
def resolve_role(request: Request) -> str:
    # ...existing body of _resolve_role...

# v4 §H.S1 — keep the underscored alias so existing private-import
# callers keep working until they migrate.
_resolve_role = resolve_role
```

### 2.4 `src/gg_relay/api/routers/sessions.py`

#### 2.4.1 `submit_session` — pass `actor_role`, catch both errors, audit

```python
from gg_relay.api.dependencies.require_role import resolve_role
from gg_relay.session.manager import (
    MissingCredentialsError,
    CredentialsLookupUnavailable,
)

# inside submit_session, after owner resolution:
actor_role = resolve_role(request)
try:
    sid = await manager.submit(
        spec,
        runtime_ctx=ctx,
        api_key_id=api_key_id,
        owner=owner,
        actor_label=getattr(request.state, "api_key_label", None),
        actor_role=actor_role,
        description=description,
    )
except MissingCredentialsError as exc:
    if audit is not None:
        with contextlib.suppress(Exception):
            await audit.record(
                actor=exc.actor_label or "anon",
                action="session_reject_missing_credentials",
                target_type="session",
                target_id="-",
                metadata={
                    "role": exc.actor_role,
                    "reason": "no_per_user_credentials",
                    "request_id": getattr(request.state, "request_id", None),
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
                "This deployment requires per-user credentials. "
                "Configure ANTHROPIC_API_KEY (or another upstream "
                "credential) at /dashboard/me/credentials, or ask "
                "an admin to provision via /dashboard/admin/credentials."
            ),
        },
    ) from exc
except CredentialsLookupUnavailable as exc:
    if audit is not None:
        with contextlib.suppress(Exception):
            await audit.record(
                actor=exc.actor_label or "anon",
                action="session_reject_lookup_unavailable",
                target_type="session",
                target_id="-",
                metadata={
                    "reason": "credentials_store_unavailable",
                    "request_id": getattr(request.state, "request_id", None),
                },
            )
    raise HTTPException(
        status_code=503,
        detail={
            "code": "credential_lookup_unavailable",
            "error": "credentials_store_transient_failure",
            "actor_label": exc.actor_label,
            "message": (
                "User-credentials store is temporarily unavailable. "
                "Retry; if persistent, check the relay's database "
                "and Fernet-key health."
            ),
        },
    ) from exc
# ...existing SDKError + RuntimeError branches...
```

Add `responses={400: {...}, 503: {...}}` to the `@router.post("/api/v1/sessions")` decorator so the OpenAPI snapshot is updated.

#### 2.4.2 `batch_sessions` — explicit retry branch handling

```python
else:  # retry
    try:
        new_sid = await manager.retry(
            sid,
            actor=label,
            actor_role=actor_role,   # resolved once at the top of the loop
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
        # Audit + per-item 503-equivalent error_code.
        # (Mirror MissingCredentialsError branch — see above.)
        ...
    items.append(
        BatchSessionItem(
            id=sid, status="ok", new_session_id=new_sid
        )
    )
    ok_count += 1
```

Add `actor_role = resolve_role(request)` ONCE near the top of `batch_sessions` (after `label = ...`).

Also add `responses={400: ..., 503: ...}` to the batch decorator if it currently lacks them (verify — likely lacks).

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

Document below the existing v3 credential keys:

```bash
# ── Plan v4 — multi-tenant credential enforcement ──────────────────
#
# When true, non-admin actors that have NOT configured per-user
# credentials at /dashboard/me/credentials will be rejected with
# HTTP 400 missing_credentials. Admin actors can still fall back
# to the host env (this var) for operations / incident response.
# Distinct 503 credential_lookup_unavailable is returned if the
# encrypted store itself errors — that's an operator/infra
# condition, not a user-attributable rejection.
#
# Default false — single-tenant deployments are unaffected.
# Independent of this flag, a WARN is emitted on every fallback
# (admin + non-admin) so you can grep for "fell back to host env"
# before flipping it on.
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

### 2.8 `scripts/load_test.py`

(Quick scan: confirm file exists; add `--credentials-key=$ANTHROPIC_API_KEY` defaulting to `os.environ`. If file does not exist or doesn't submit sessions, drop this item — verified during impl.)

## 3. Test plan

New file `tests/integration/test_require_per_user_credentials.py` (~300 LOC, 9 cases):

| # | Test name | Setup | Assertion |
|---|-----------|-------|-----------|
| T1 | `test_default_off_preserves_fallback` | flag unset, non-admin actor, no creds | `submit` returns sid; no `MissingCredentialsError`. |
| T2 | `test_strict_mode_blocks_non_admin_without_creds` | flag `True`, non-admin actor, no creds | HTTP 400 `code=missing_credentials`; one `session_reject_missing_credentials` audit row written. |
| T3 | `test_strict_mode_allows_non_admin_with_db_creds` | flag `True`, non-admin actor, DB-stored `ANTHROPIC_API_KEY` | HTTP 202; no rejection audit row. |
| T4 | `test_strict_mode_allows_non_admin_with_body_creds` | flag `True`, non-admin actor, body `credentials={"ANTHROPIC_API_KEY":"sk-…"}` | HTTP 202. |
| T5 | `test_strict_mode_allows_admin_without_creds_with_warn` | flag `True`, **admin** actor, no creds | HTTP 202; WARN log emitted: `"admin actor=... fell back to host env"`. |
| T6 | `test_warn_emitted_for_non_admin_fallback_even_when_flag_off` | flag `False`, non-admin actor, no creds | HTTP 202 AND WARN: `"non-admin actor=... fell back to host env"`. |
| T7 | `test_any_allowlist_credential_satisfies_strict_mode` | flag `True`, non-admin actor, body `credentials={"ANTHROPIC_BASE_URL":"https://proxy"}` (no auth key) | HTTP 202 — strict mode now accepts any non-empty credentials dict (Reviewer H.3 fix). |
| T8 | `test_batch_retry_inherits_strict_mode` | flag `True`, batch retry initiated by non-admin without creds | per-item `error_code="missing_credentials"` (NOT `internal_error`); audit row `via="batch_retry"`. |
| T9 | `test_unknown_role_treated_as_non_admin` | flag `True`, actor_role=`"viewer"` or `None`, no creds | 400. |
| T10 | `test_store_failure_returns_503_under_strict_mode` | flag `True`, non-admin actor, configured creds but store raises | HTTP 503 `code=credential_lookup_unavailable`; audit row `session_reject_lookup_unavailable`. (Reviewer I.1 fix.) |
| T11 | `test_store_failure_silent_under_soft_mode` | flag `False`, store raises | HTTP 202 (legacy behaviour preserved); WARN logged. |

New file `tests/integration/test_router_passes_actor_role.py` (~70 LOC, 2 cases — Reviewer H.4 fix):

| # | Test name | Verifies |
|---|-----------|----------|
| TR1 | `test_submit_route_passes_actor_role_kwarg` | Mock `manager.submit`; POST `/api/v1/sessions`; assert `actor_role=` appears in the mock's call kwargs and equals the role resolved by `resolve_role`. |
| TR2 | `test_batch_retry_passes_actor_role_kwarg` | Mock `manager.retry`; POST `/api/v1/sessions/batch` with `action=retry`; assert `actor_role=` kwarg present per call. |

Extend `tests/integration/test_user_credentials_dashboard.py`:

| # | Test name | Verifies |
|---|-----------|----------|
| TD1 | `test_new_session_form_renders_structured_400` | strict mode `True`, non-admin session via dashboard form; rendered HTML contains `Per-user credentials required` and the anchor to `/dashboard/me/credentials`. (Reviewer I.3 fix.) |

OpenAPI: regenerate snapshot via `uv run python scripts/dump_openapi.py` as part of D.10 (otherwise `test_openapi_snapshot_matches` will fail).

## 4. Backward compatibility

* Default `require_per_user_credentials=False`. Every existing test fixture, lifespan, CLI, script: zero behavioural change.
* `actor_role=None` default on `submit`/`retry` means direct in-process callers (legacy tests) need no update; the enforcement only fires when both strict mode AND non-admin role are present, and unauthed test-path callers reach the new branch with `actor_role=None` (treated as non-admin) ONLY if strict mode is `True`.
* `_resolve_role` alias preserves any private-name import.

## 5. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| R1: Recognised-key semantic ("any cred satisfies") lets a user upload only `ANTHROPIC_BASE_URL` and pass strict mode, then the SDK fails at the API layer. | Documented trade-off in §1 G2 prose. Strict mode is "operator opt-in evidence-of-intent gate", NOT a per-mode auth validator. Operators wanting auth-key validation must layer on top with their own deploy-time validation. Accept this as v1 scope. |
| R2: Router refactor forgets `actor_role`. | `test_router_passes_actor_role.py` mocks the manager and asserts kwargs. Far stronger than the v1 grep-based Make audit. |
| R3: `CredentialsLookupUnavailable` exception leaks store internals via `__cause__`. | Router converts to HTTPException with sanitized `message`. The `__cause__` traceback is logged server-side (via standard FastAPI handling) but not returned to the client. |
| R4: Audit writes inside `except` could fail and shadow the HTTP error. | All audit writes wrapped in `contextlib.suppress(Exception)` — same pattern as existing star/unstar/batch_cancel paths. |
| R5: WARN log volume in production. | Documented in §1 G4. Single line per submission, standard logger; operators with single-tenant deployments can either flip strict mode on or `logging.getLogger("gg_relay.session.manager").setLevel(logging.ERROR)`. |
| R6: Strict mode disables `scripts/load_test.py`. | §2.8 adds an opt-in `--credentials-key` flag. |
| R7: Dashboard JS `errBox.innerHTML` could XSS via attacker-controlled body. | The error UI ONLY accepts the trusted strings WE generate server-side; the user's prompt is never echoed back into this branch. The XSS surface is just our own copy + the static `/dashboard/me/credentials` anchor. No user data reaches `innerHTML`. |
| R8: Empty `runtime_ctx.credentials` after a successful merge (e.g. DB had a `{}` row) wrongly counted as "no creds". | `user_credentials_store.get_for_user` only returns rows with at least one key; an empty dict can only happen when the user has uploaded nothing. Sanity-checked in T3 (DB has key) vs T2 (DB empty). |
| R9: `actor_role` mis-resolution for legacy single-tenant deployments where `cfg.role_mapping={}`. | `_resolve_role` returns `"viewer"` in that case. Under strict mode, EVERY session including the operator's own would be rejected → operator immediately realizes role mapping isn't set. This is desirable: strict mode shouldn't pretend to work without role mapping. Documented in §6 Q4. |

## 6. Open questions for reviewer

* **Q1.** Should T7's "any cred satisfies" semantic be configurable (e.g. a separate `RELAY_REQUIRE_AUTH_CREDENTIAL=true` sub-flag that DEMANDS `ANTHROPIC_API_KEY` OR `ANTHROPIC_AUTH_TOKEN` specifically)? v2 says NO — too many auth modes; keep the manager simple. Reviewer may push for the sub-flag.
* **Q2.** Should `CredentialsLookupUnavailable` be retryable-with-backoff at the API client level? v2 returns plain 503 with no `Retry-After` header. Reviewer may want `Retry-After: 5`.
* **Q3.** Should the audit row include the FAILED store exception class / message? v2 says NO — error-message leakage to audit log can include connection strings / secrets. Reviewer may push for class-name only.
* **Q4.** Should strict mode REFUSE to boot if `cfg.role_mapping == {}` (so the operator doesn't enable enforcement against an unmapped fleet)? v2 says NO — boot is owned by lifespan; just log a loud WARN. Reviewer may want a hard fail.

## 7. Execution checklist

* [ ] **D.1** Add `require_per_user_credentials: bool = False` to `Config`.
* [ ] **D.2** Add `MissingCredentialsError` + `CredentialsLookupUnavailable` exception classes in `manager.py`.
* [ ] **D.3** Add `require_per_user_credentials` constructor param (default `False`) to `SessionManager`.
* [ ] **D.4** Add `actor_role` param (default `None`) to `submit` AND `retry`; implement merge-failure escalation + fallback enforcement + dual WARN log.
* [ ] **D.5** Promote `_resolve_role` → `resolve_role` (alias kept).
* [ ] **D.6** Update `submit_session` router: pass `actor_role`, catch BOTH exceptions, audit BEFORE raise, declare `responses=`.
* [ ] **D.7** Update `batch_sessions` router: resolve `actor_role` once, pass to `retry`, explicit `except` branches with audit BEFORE generic `Exception`.
* [ ] **D.8** Wire `require_per_user_credentials` from config into `SessionManager(...)` in `api/main.py` lifespan.
* [ ] **D.9** Update `.env.example`.
* [ ] **D.10** Update `dashboard/templates/new.html` `hx-on::after-request` handler.
* [ ] **D.11** `scripts/load_test.py` — verify presence; add `--credentials-key` flag if it submits sessions (skip if no-op).
* [ ] **D.12** Write `tests/integration/test_require_per_user_credentials.py` (T1–T11).
* [ ] **D.13** Write `tests/integration/test_router_passes_actor_role.py` (TR1–TR2).
* [ ] **D.14** Extend `tests/integration/test_user_credentials_dashboard.py` with TD1.
* [ ] **D.15** Regenerate `docs/openapi.snapshot.json` (`uv run python scripts/dump_openapi.py`).
* [ ] **D.16** Run full regression: `uv run pytest tests/ -q --no-cov`, `uv run ruff check src tests`, `make actor-label-audit` (existing, untouched), no `make require-creds-audit` (dropped).
