# Plan v1 — Require per-user credentials (C+B)

**Date**: 2026-05-26
**Author**: gg-relay
**Scope**: Close the multi-tenant credential-fallback hole identified in the post-A+B follow-up. Today every authenticated user (admin or not) silently falls back to the operator's shared `.env` `ANTHROPIC_API_KEY` when they have not configured per-user credentials. This plan adds (C) an **operator opt-in strict mode** that rejects such sessions for **non-admin** actors, and (B) a **soft observability WARN** that fires regardless of strict mode whenever a non-admin actor would have fallen back. The default remains today's behavior so single-tenant deployments are unaffected.

**Companion to** the Plan v3 work (A+B) merged 2026-05-25. This plan does NOT add any DB schema, migration, or new IAM concept — it composes the existing `actor_label`, `_resolve_role`, and `runtime_ctx.credentials` machinery into a single, configurable enforcement point.

---

## 0. Problem statement

After Plan v3:

* `manager.submit(spec, runtime_ctx=…, actor_label=…)` merges DB-stored credentials keyed off the unforgeable `actor_label`.
* When no DB row exists and the API body sends no `credentials`, `runtime_ctx.credentials == {}`.
* `client._make_runner_core` builds `options.env` from that empty dict, so `ANTHROPIC_API_KEY` is absent from the SDK's explicit env.
* `claude_code_sdk._internal.transport.subprocess_cli` does `env = {**os.environ, **options.env}` — the subprocess **inherits `ANTHROPIC_API_KEY` from the operator's `.env`**.

Operationally this means:

1. Any user able to log into the dashboard or call `/api/v1/sessions` with a valid API key gets a **free silent fallback** to the operator's wallet.
2. There is **no audit trail** distinguishing "alice used her own key" from "alice silently spent operator quota".
3. There is **no enforcement knob** for operators wanting strict per-user attribution.

This is acceptable for single-tenant deployments (the original happy path) but **unsafe for multi-tenant** ones.

## 1. Goals & non-goals

### Goals

* **G1.** Add a config flag `RELAY_REQUIRE_PER_USER_CREDENTIALS=true|false` (default `false`) that enforces per-user credential presence for **non-admin** actors at submission time.
* **G2.** When the flag is on and a non-admin actor would have fallen back to `os.environ`, reject the submission with `400 missing_credentials` carrying an actionable detail pointing at `/dashboard/me/credentials`.
* **G3.** Independent of G1 — regardless of the flag — emit a single WARN log line whenever a non-admin actor falls back, so operators always have a grep-able signal.
* **G4.** Admin actors retain fallback ability under strict mode (operations / incident response).
* **G5.** Default-off: every existing test and every existing deployment that does not set the new flag observes byte-identical behavior.

### Non-goals

* No new DB schema or migration.
* No change to the upload allowlist (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, etc.) — that's owned by `/api/v1/me/credentials`.
* No change to the SDK env merge order in `client.py` (still `runtime_ctx.credentials > spec.plugins.extra_env`).
* No CLI command for managing other users' credentials (already covered by `/admin/credentials`).
* No deletion of the existing `os.environ` fallback (operator-controlled — flipping the flag is the off-switch).
* No "credential-required for `gg-relay` internal tools" change — the enforcement is purely on session submission.

## 2. Scope per-file (additive only)

### 2.1 `src/gg_relay/config.py`

Add ONE field next to the existing v3 credential fields (line ~410):

```python
require_per_user_credentials: bool = False
"""Strict-mode opt-in for multi-tenant deployments.

When ``True``, :meth:`SessionManager.submit` rejects sessions from
**non-admin** actors whose merged ``runtime_ctx.credentials`` lack a
recognised upstream auth key (``ANTHROPIC_API_KEY`` OR
``ANTHROPIC_AUTH_TOKEN``). Admin actors retain the fallback path
for operations / incident response.

Default ``False`` preserves single-tenant behaviour: every existing
deployment that does not set ``RELAY_REQUIRE_PER_USER_CREDENTIALS``
sees identical behaviour. When ``True``, operators must ensure each
dashboard user has either configured ``/dashboard/me/credentials``
or that an admin has provisioned them via ``/admin/credentials``.

Independent of this flag, a one-line WARN ("non-admin actor=… fell
back to host env") is emitted on every fallback so operators can
detect drift even without enforcement.
"""
```

Pydantic `BaseSettings` picks up `RELAY_REQUIRE_PER_USER_CREDENTIALS` automatically via the `RELAY_` prefix the rest of the config uses.

### 2.2 `src/gg_relay/session/manager.py`

#### 2.2.1 New constants (module level)

```python
# Plan v4 §2 — recognised upstream auth keys. ONE of these MUST be
# present in the merged credentials for a session to qualify as
# "has per-user credentials". The deliberately small set covers the
# two Anthropic auth modes; Bedrock / Vertex / proxy modes are out
# of scope (operators using those should leave strict mode off or
# extend this set with an explicit code change).
_UPSTREAM_AUTH_KEYS: frozenset[str] = frozenset({
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
})
```

#### 2.2.2 `__init__` adds ONE parameter (default preserves behaviour)

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

Stored on `self._require_per_user_credentials`. No interaction with any other state.

#### 2.2.3 `submit` accepts `actor_role` and enforces

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
    # ...existing accepting-check + DB-credentials merge...
    # (post-merge runtime_ctx.credentials is the authoritative view)

    # Plan v4 §2 — strict-mode enforcement + soft-mode observability.
    has_auth = any(
        k in runtime_ctx.credentials for k in _UPSTREAM_AUTH_KEYS
    )
    role_is_admin = (actor_role == "admin")
    if not has_auth and not role_is_admin:
        # Soft mode (B): always log — operators get a grep-able
        # signal even without strict mode.
        logger.warning(
            "non-admin actor=%r role=%r submitted session without "
            "per-user credentials; falling back to host env. "
            "Configure via /dashboard/me/credentials or enable "
            "RELAY_REQUIRE_PER_USER_CREDENTIALS=true to reject.",
            actor_label,
            actor_role,
        )
        # Strict mode (C): refuse.
        if self._require_per_user_credentials:
            raise MissingCredentialsError(
                actor_label=actor_label,
                actor_role=actor_role,
            )

    # ...existing rest of submit (sid generation, persistence, queue)...
```

#### 2.2.4 New exception class (module level, near other domain errors)

```python
class MissingCredentialsError(Exception):
    """Raised by :meth:`SessionManager.submit` when strict mode is on,
    the actor is non-admin, and no recognised upstream auth key is
    present in the merged credentials.

    The API router translates this to ``HTTP 400 missing_credentials``;
    in-process callers can catch it for tests / programmatic clients.
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
```

#### 2.2.5 `retry()` forwards `actor_role` symmetrically

The existing `retry()` already forwards `actor` as `actor_label`. Same pattern: add an `actor_role` parameter, default `None`, forward to `self.submit(...)`. The router will resolve and pass it. No new bypass path.

### 2.3 `src/gg_relay/api/routers/sessions.py`

Two-line change in `submit_session` and the analogous `retry` endpoint:

```python
from gg_relay.api.dependencies.require_role import _resolve_role
# ...
sid = await manager.submit(
    spec,
    runtime_ctx=ctx,
    api_key_id=api_key_id,
    owner=owner,
    actor_label=getattr(request.state, "api_key_label", None),
    actor_role=_resolve_role(request),
    description=description,
)
```

And translate `MissingCredentialsError` to HTTP:

```python
except MissingCredentialsError as exc:
    raise HTTPException(
        status_code=400,
        detail={
            "code": "missing_credentials",
            "error": "per_user_credentials_required",
            "actor_label": exc.actor_label,
            "actor_role": exc.actor_role,
            "message": (
                "This deployment requires per-user credentials. "
                "Configure ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) "
                "at /dashboard/me/credentials, or ask an admin to "
                "provision via /dashboard/admin/credentials."
            ),
        },
    ) from exc
```

Same change applies to the retry endpoint (which already passes `actor_label`; we add `actor_role`).

### 2.4 `src/gg_relay/api/main.py`

Single-line wire-up in lifespan where `SessionManager(...)` is constructed:

```python
manager = SessionManager(
    # ...existing kwargs...
    user_credentials_store=user_credentials_store,
    require_per_user_credentials=getattr(
        cfg, "require_per_user_credentials", False
    ),
)
```

`getattr` with default preserves compatibility with any test that constructs a `Config` via `Config.__init__` without the new field (the default applies anyway, but defensive).

### 2.5 `.env.example`

Add commented-out documentation immediately below the existing v3 credential keys:

```bash
# ── Plan v4 — multi-tenant credential enforcement ──────────────────
#
# When true, non-admin actors that have NOT configured per-user
# credentials at /dashboard/me/credentials will be rejected with
# HTTP 400 missing_credentials. Admin actors can still fall back
# to the host env (this var) for operations / incident response.
#
# Default false — single-tenant deployments are unaffected.
# Independent of this flag, a WARN is emitted on every non-admin
# fallback so you can grep for "fell back to host env" before
# flipping it on.
#
# RELAY_REQUIRE_PER_USER_CREDENTIALS=false
```

### 2.6 `Makefile`

Extend the existing `actor-label-audit` target with a sibling `require-creds-audit` that greps for `actor_role=` next to every `manager.submit(` and `manager.retry(` call:

```makefile
.PHONY: require-creds-audit
require-creds-audit:
	@echo "==> Verifying every manager.submit/retry callsite passes actor_role"
	@! rg -n 'manager\.(submit|retry)\(' --type py \
		--glob '!tests/**' \
		--glob '!src/gg_relay/session/manager.py' \
		-A 8 \
		| rg -B 1 -A 8 'manager\.(submit|retry)\(' \
		| rg -v 'actor_role=' \
		| rg 'manager\.(submit|retry)\('
```

(Same pattern as `actor-label-audit`; both guard against a router being added later that forgets to forward the actor metadata.)

## 3. Test plan

New file `tests/integration/test_require_per_user_credentials.py` (~250 LOC):

| # | Test name | Setup | Assertion |
|---|-----------|-------|-----------|
| T1 | `test_default_off_preserves_fallback` | flag unset, non-admin actor, no creds | `submit` succeeds, no `MissingCredentialsError`. Single biggest backward-compat guarantee. |
| T2 | `test_strict_mode_blocks_non_admin_without_creds` | flag `True`, non-admin actor, no creds | `submit` raises `MissingCredentialsError`; HTTP path returns `400` with `code=missing_credentials` and the actionable `message`. |
| T3 | `test_strict_mode_allows_non_admin_with_db_creds` | flag `True`, non-admin actor, DB-stored `ANTHROPIC_API_KEY` | `submit` succeeds; merged creds carry the DB value. |
| T4 | `test_strict_mode_allows_non_admin_with_body_creds` | flag `True`, non-admin actor, body `credentials={"ANTHROPIC_API_KEY": "sk-…"}` | `submit` succeeds without consulting DB. |
| T5 | `test_strict_mode_allows_admin_without_creds` | flag `True`, **admin** actor (`actor_role="admin"`), no creds | `submit` succeeds (operator escape hatch); WARN log emitted. |
| T6 | `test_warn_emitted_for_non_admin_fallback_even_when_flag_off` | flag `False` (default), non-admin actor, no creds | `submit` succeeds AND WARN log emitted with `"fell back to host env"`. |
| T7 | `test_anthropic_auth_token_also_satisfies` | flag `True`, non-admin actor, body has `ANTHROPIC_AUTH_TOKEN` (not `_API_KEY`) | `submit` succeeds — the recognised-key set includes both auth modes. |
| T8 | `test_retry_path_inherits_strict_mode` | flag `True`, retry initiated by non-admin without creds | retry surfaces `MissingCredentialsError` (no actor_role bypass via the retry route). |
| T9 | `test_unknown_role_treated_as_non_admin` | flag `True`, actor_role=`"viewer"` or `None`, no creds | rejected — viewer/None must NOT bypass enforcement. |

Each test uses the existing `manager_factory` / async fixture pattern from `tests/integration/test_manager_credentials_merge.py`. Tests 2 and 8 exercise the HTTP path; the others can hit `manager.submit(...)` directly with a small ASGI app fixture for T2/T8.

Also add ONE assertion to `tests/integration/test_user_credentials_dashboard.py`: with `require_per_user_credentials=True` the `/dashboard/me/credentials` page banner should mention strict mode (purely informational; no behavior change beyond text — but tests pin the text).

## 4. Backward compatibility & migration

* **Default value is `False`** — every test fixture that constructs `Config()` without overriding the field automatically observes the legacy behaviour.
* **`SessionManager` constructor** picks up `require_per_user_credentials=False` by default, so direct test callers (e.g. unit tests that build a `SessionManager` outside the FastAPI lifespan) don't need to change.
* **No schema change** — zero Alembic work, zero operator action required on existing DBs.
* **`.env.example`** documents the new var as optional and commented out.

## 5. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| R1: A router added later forgets to pass `actor_role` → enforcement silently disabled. | `require-creds-audit` Make target greps every `manager.submit/retry` callsite for `actor_role=`. |
| R2: Strict mode breaks programmatic clients (CI) that previously relied on `.env` fallback. | (a) Default off. (b) Strict-mode failure message names the exact remediation. (c) `ANTHROPIC_API_KEY` may still be sent in the API body — CI just adds one line. |
| R3: An attacker spoofs `actor_role="admin"`. | `actor_role` is resolved from `_resolve_role(request)` which is sourced from `request.state.api_key_role` (DB) or `cfg.role_mapping[label]` (config). The router NEVER reads role from the request body. The manager treats `actor_role` as a string but only as a comparison key — no privilege is granted from the value itself. |
| R4: WARN noise (every non-admin fallback in single-tenant deployments). | One log line per submission, INFO-rate logger at WARNING level. Operators with single-tenant `.env` setups can either (a) tune log level, (b) flip strict mode on once they've migrated users, or (c) ignore — the WARN is informational. |
| R5: `_resolve_role` is currently a "private" helper (underscore prefix). | Add an explicit `__all__` entry / rename to `resolve_role` if necessary. Concretely, the helper is already imported by `dashboard/router.py` indirectly; promoting it is a one-line rename plus an alias for back-compat. (Alternative: re-export under a public name to avoid the rename.) |
| R6: Recognised key set is too narrow (no Bedrock / Vertex). | Documented as an explicit non-goal. Operators using those modes leave strict mode off; later plan can extend the set with `AWS_*` / `GOOGLE_APPLICATION_CREDENTIALS` once we add allowlist entries for them. |
| R7: An admin role-mapping entry is misconfigured (`admin` user actually has `submitter` role). | This is a pre-existing config error, not introduced by this plan. The plan's behaviour is correct given the resolved role; the existing `_resolve_role` test coverage and the admin bootstrap path remain authoritative. |

## 6. Execution checklist

* [ ] **D.1** Add `require_per_user_credentials: bool = False` to `Config`.
* [ ] **D.2** Add `_UPSTREAM_AUTH_KEYS` constant + `MissingCredentialsError` exception in `manager.py`.
* [ ] **D.3** Add `require_per_user_credentials` constructor parameter (default `False`) to `SessionManager`.
* [ ] **D.4** Add `actor_role` parameter (default `None`) to `SessionManager.submit` and `SessionManager.retry`; implement enforcement + WARN in `submit`.
* [ ] **D.5** Update `routers/sessions.py` `submit_session` and `retry` to pass `actor_role=_resolve_role(request)` and translate `MissingCredentialsError` to 400.
* [ ] **D.6** Wire `require_per_user_credentials` from config into `SessionManager(...)` in `api/main.py` lifespan.
* [ ] **D.7** Update `.env.example` with the new var (commented out).
* [ ] **D.8** Add `require-creds-audit` Make target.
* [ ] **D.9** Write `tests/integration/test_require_per_user_credentials.py` (T1–T9).
* [ ] **D.10** Run full regression: `pytest tests/ -q --no-cov` (full), `ruff check src tests`, `make actor-label-audit`, `make require-creds-audit`.
* [ ] **D.11** Verify the schema-drift WARN added in the previous turn is unaffected (no Alembic change in this plan).

## 7. Open questions for reviewer

* **Q1.** Should the recognised-key set additionally include `CLAUDE_CODE_USE_BEDROCK=true` + `AWS_*` keys behind a sub-flag, or is "two Anthropic auth modes" the right v1 scope? *(Plan v1 takes the conservative narrow set; an operator-extensible mechanism is deferred.)*
* **Q2.** Should `_resolve_role` be promoted to a public helper (rename / re-export) given two modules now depend on it? *(Plan v1 keeps the underscore prefix and adds a short comment in `routers/sessions.py` pinning the import path; reviewer may request a rename.)*
* **Q3.** Should strict-mode WARN deduplicate per actor (e.g. once per 60s) to control log volume? *(Plan v1 says NO — every submission is a discrete authz decision and audit-worthy. Reviewer may push back on log volume in high-throughput deployments.)*
