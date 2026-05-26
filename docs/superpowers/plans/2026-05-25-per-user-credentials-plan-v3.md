# Per-User Upstream Credentials — Plan v3

**Status:** DRAFT (pending Santa dual-review, round 3 — final budgeted round)
**Date:** 2026-05-25
**Supersedes:** plan-v2 (Santa round 2 FAIL — see Appendix Z for v1→v2→v3 deltas)

---

## 0. Goals & Non-Goals

### Goals

1. **A: contract repair (inprocess)** — `runtime_ctx.credentials`
   delivered to `_make_runner_core` must reach the SDK subprocess
   env. Today the inprocess path silently discards them, so the
   API schema is a lie. Plan A *brings inprocess into line with
   the documented API contract*; it does NOT promise byte-for-byte
   parity with the docker executor (the two backends have a known,
   pre-existing divergence on `RELAY_TRACE_ID` — see A.3 / A.6).
2. **B: per-user upstream key self-service** — dashboard users
   ("submitter" + "admin" roles) can configure their own
   `ANTHROPIC_*` credentials via a `/dashboard/me/credentials` page.
   `SessionManager` auto-injects the configured values into every
   submission so the operator never has to think about it.
3. **Defence in depth** — secrets at rest are encrypted (fernet),
   never echoed back via the API, redacted in logs, scoped by RBAC
   on the **authenticated identity (not the spoofable `owner`
   field)**, and gated by an `env_name` allowlist.

### Independence

A and B are decoupled. A can ship alone (it just fixes a contract
bug). B cannot ship without A (B's whole point is to populate
`runtime_ctx.credentials`, which today is a no-op in inprocess).

### Non-Goals

- Per-tenant upstream rate-limit accounting (v2 plan).
- Multi-tenant org / workspace model.
- Encryption-key **rotation tooling** (v1 stores a fingerprint
  per-row to enable v2 rotation, but ships no rotation script).
- **Per-user credentials cache** (v1 hits DB every submit; see B.6
  for the latency analysis).
- Cluster-aware cache invalidation (no cache → no invalidation
  needed → no new event class needed).

### Constraints carried forward

- `SessionRuntimeContext` is **never persisted**
  (`SessionSpec.to_json_safe` intentionally drops credentials).
- `SessionManager` is **framework-agnostic** — must not reach into
  `Starlette.request.state`. New collaborators come in via the
  constructor; new identity context flows in as an explicit kwarg
  the router passes (see B.6.2).
- Single-identity contract (Plan 8 D8.25) — cookie auth + API key
  auth converge on `api_key_label`. v1 keys per-user creds on that
  same label.

---

## A. Inprocess Credentials Pass-Through Repair

### A.1 Symptom

`src/gg_relay/session/client.py:_make_runner_core` builds the SDK
env from `spec.plugins.extra_env` + `RELAY_TRACE_ID` + `CLAUDE_ROOT`
and never touches `runtime_ctx.credentials`. Result:

- `POST /api/v1/sessions` accepts `credentials: {ANTHROPIC_API_KEY: ...}`
  in `api/schemas.py:SessionSubmitRequest`, the router forwards them
  into `SessionRuntimeContext` (`api/routers/sessions.py:154`), but
  the inprocess runner silently discards them.
- Docker executor's `_build_env` honours the field correctly
  (`executor/docker.py`), so the bug is inprocess-only.

### A.2 SDK env-passing semantics (spike-confirmed)

`claude_code_sdk._internal.transport.subprocess_cli.py` (l. 183-187):

```python
process_env = {
    **os.environ,
    **self._options.env,                  # USER env wins over host env
    "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
}
```

`ClaudeCodeOptions.env` is **merged on top of `os.environ`**, not a
replacement. Implications:

1. Empty `options.env` is safe — SDK still inherits host's
   `ANTHROPIC_API_KEY`. Preserves existing behaviour for callers
   that don't supply credentials.
2. Any key we put in `options.env` overrides the host's value
   for that subprocess (intended).
3. We can safely add `runtime_ctx.credentials` without breaking
   the "single-tenant deploy that relies on shell env" path.

### A.3 Override order — explicit, with known divergence

**Docker (`_build_env`, unchanged):** baseline → proxy →
`runtime_ctx.credentials` → `spec.plugins.extra_env`. `RELAY_TRACE_ID`
lives in the baseline, so BOTH credentials and extra_env can
override it.

**Inprocess (proposed):** `runtime_ctx.credentials` →
`spec.plugins.extra_env` → `RELAY_TRACE_ID` (explicit set, not
setdefault) → `CLAUDE_ROOT` (setdefault).

**Pre-existing inprocess-vs-docker divergence (NOT introduced by this
plan):**

| key | docker behaviour | inprocess behaviour | rationale |
|---|---|---|---|
| `RELAY_TRACE_ID` | overridable by extra_env | UN-overridable by extra_env (system marker, always wins) | pinned by `tests/unit/session/test_real_sdk_trace_id_inject.py::test_trace_id_does_not_clobber_existing_env` since v0.8; out of scope for this plan |
| `CLAUDE_ROOT` | not set | `setdefault` (extra_env wins) | existing inprocess convention in `_make_runner_core` (kept as-is to avoid surprising any deployment whose `extra_env` already pins `CLAUDE_ROOT`); no current test covers this — A.5 adds `test_extra_env_overrides_claude_root_setdefault` to pin going forward |

The plan does NOT unify these. It simply adds `runtime_ctx.credentials`
as a new layer that is overridable by `extra_env` (matching docker)
and overridable by `RELAY_TRACE_ID` (consistent with the existing
inprocess "system marker wins" convention).

### A.4 Code sketch

```python
# src/gg_relay/session/client.py — _make_runner_core, before
# the `options = ClaudeCodeOptions(...)` line.
env: dict[str, str] = {}
if runtime_ctx is not None:
    for k, v in runtime_ctx.credentials.items():
        env[k] = v
for k, v in spec.plugins.extra_env:
    env[k] = v
if runtime_ctx is not None and runtime_ctx.trace_id:
    env["RELAY_TRACE_ID"] = runtime_ctx.trace_id
if install_report is not None and install_report.install_root is not None:
    env.setdefault("CLAUDE_ROOT", str(install_report.install_root))
```

### A.5 Tests (`tests/unit/session/test_client_credentials_passthrough.py`)

1. `test_runtime_ctx_credentials_reach_sdk_env` — stub SDK factory
   captures `options.env`; assert `ANTHROPIC_API_KEY` from
   `runtime_ctx.credentials` is present.
2. `test_extra_env_overrides_credentials` — both set same key,
   `extra_env` wins (matches docker contract).
3. `test_no_credentials_keeps_env_empty` — empty `runtime_ctx`,
   empty `extra_env` → `options.env == {}`.
4. `test_trace_id_overrides_credentials_attempt_to_set_it` —
   `runtime_ctx.credentials = {"RELAY_TRACE_ID": "evil"}` plus a
   real `trace_id` → the real trace_id wins; pins the
   "inprocess system marker" convention.
5. `test_extra_env_overrides_claude_root_setdefault` — when
   `install_report.install_root` is set AND
   `spec.plugins.extra_env` contains `("CLAUDE_ROOT", "/from/extra")`,
   the `extra_env` value wins (because the system injection uses
   `env.setdefault`, not `env[k] = v`). Backfills the missing
   regression net for the A.3 divergence table entry.

### A.6 Backwards compatibility

- Deployments relying on shell-env `ANTHROPIC_API_KEY` with empty
  `credentials` → unchanged.
- Deployments that already pass `credentials` via API → start
  actually getting honoured. This is a **fix**, not a regression.
- Docker backend → untouched.

### A.7 Documentation updates

- `docs/api.md` — note `credentials` now reaches inprocess.
- `CHANGELOG.md` — `Fixed: inprocess executor now honours
  runtime_ctx.credentials (was silently discarded)`.

---

## B. Per-User Credentials Self-Service

### B.1 Data model

**Alembic `0013` (next free slot; verified `ls versions/` shows
`0012_plan9_events_seq_and_dashboard_keys.py` as the latest).**
`down_revision='0012'`. Plan 9's `dashboard_internal_keys` table
already lives inside 0012 — there is no 0013 yet.

```python
user_credentials = Table(
    "user_credentials", metadata,
    Column("id", Integer, primary_key=True),
    Column("user_label", String(64), nullable=False),
    Column("env_name", String(64), nullable=False),
    Column("value_encrypted", LargeBinary, nullable=False),
    Column("key_fingerprint", String(16), nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    Column("created_by_label", String(64), nullable=False),
    Column("notes", String(512), nullable=True),
    UniqueConstraint("user_label", "env_name",
                     name="uq_user_credentials_label_env"),
    Index("ix_user_credentials_user_label", "user_label"),
)
```

- `user_label` is the same identity used by `api_keys.label`
  (`dashboard-alice`, `ci-bot`, etc.). The credential lookup at
  submit time uses the **authenticated identity** (B.6.2), so
  `user_label` must match the actor's `api_key_label`.
- `env_name` is constrained at the API layer to a hard-coded
  allowlist (B.5).
- `value_encrypted` — fernet-encrypted bytes.
- `key_fingerprint` — first 16 hex chars of SHA-256 of the
  encryption key; v2 rotation tooling can identify rows encrypted
  with a now-stale key without decrypt-and-retry.
- `created_by_label` — the actor who wrote the row. For
  self-service writes equals `user_label`; for admin overrides it
  equals the admin's label. The UI surfaces this so the user can
  tell which rows an admin touched.

### B.2 Encryption + feature flag

**New config fields (`src/gg_relay/config.py`):**

- `credentials_encryption_key: SecretStr | None = None` (env:
  `RELAY_CREDENTIALS_ENCRYPTION_KEY`). Format: 32-byte
  url-safe-base64 fernet key.
- `disable_user_credentials: bool = False` (env:
  `RELAY_DISABLE_USER_CREDENTIALS`). Hard kill switch.

**Lifespan behaviour (`api/main.py`):**

- `disable_user_credentials=True` → store constructed with
  `fernet=None`; routes register but return 503; manager skips
  merge; one INFO log at startup; feature OFF, no warning.
- `disable_user_credentials=False` AND key missing → same wiring
  as above (store with `fernet=None`); one WARNING log at startup
  (`RELAY_CREDENTIALS_ENCRYPTION_KEY missing; user-credentials
  feature disabled — set the key or
  RELAY_DISABLE_USER_CREDENTIALS=true to silence`); routes return
  503.
- `disable_user_credentials=False` AND key present → fernet
  built, store wired, feature ON. One INFO log at startup
  showing the key fingerprint (NOT the key itself).

**Rationale for warn-not-fail:** existing deployments upgrading to
this version must keep working with their current shell-env
`ANTHROPIC_API_KEY`. They opt in by setting the new env var.

**CLI helpers (added to `D` execution order):**

- `gg-relay generate-encryption-key` — prints a fresh fernet
  key. Implementation: `print(Fernet.generate_key().decode())`.
  Added to `cli/__init__.py` as a sibling of `check-secrets`.
- `gg-relay list-bricked-credentials` — lists `(user_label,
  env_name, key_fingerprint, updated_at)` for rows whose
  `key_fingerprint` does NOT match the current key's fingerprint.
  Lets an operator identify what needs re-entry after a key
  rotation or a key loss.

### B.3 Store (`src/gg_relay/store/user_credentials.py`)

```python
class UserCredentialsStore:
    def __init__(
        self, engine, *, fernet: Fernet | None,
        key_fingerprint: str | None,
    ) -> None: ...

    async def get_for_user(self, label: str) -> dict[str, str]:
        """Returns {env_name: decrypted_value} for the user.
        Returns {} when:
          - fernet is None (feature disabled)
          - no rows for that label
          - any row's key_fingerprint doesn't match the current
            key (logs WARNING + emits metric, treats as missing)
          - Fernet.decrypt raises InvalidToken (same — log + skip
            the row, don't poison the whole submit)."""

    async def upsert(
        self, *, user_label: str, env_name: str, value: str,
        actor_label: str, notes: str | None = None,
    ) -> dict[str, Any]:
        """Encrypt + UPSERT; returns row metadata (no plaintext).

        ON CONFLICT behaviour: when a row already exists for
        (user_label, env_name), the SQL ``DO UPDATE SET`` clause
        replaces ``value_encrypted``, ``key_fingerprint``,
        ``updated_at``, ``created_by_label``, and ``notes`` with
        the new values. ``created_by_label`` is INTENTIONALLY
        overwritten — it always reflects the most recent writer,
        which is exactly what the admin-override audit story
        requires (see B.8.3.g)."""

    async def delete(self, *, user_label: str, env_name: str) -> bool:
        """True if a row was removed, False if no-op."""

    async def list_for_user(self, label: str) -> list[dict[str, Any]]:
        """Metadata only. Never decrypts."""

    async def list_all(self) -> list[dict[str, Any]]:
        """Admin view. Metadata only."""

    async def list_bricked(self) -> list[dict[str, Any]]:
        """Rows whose key_fingerprint != current key's fingerprint.
        Used by the CLI helper. Metadata only."""
```

- All methods short-circuit safely when `fernet is None`.
- `get_for_user` is on the submit hot path. **v1 hits the DB
  every call** — no caching. Latency budget below.

### B.4 API routes (new router `api/routers/user_credentials.py`)

| Method | Path | Role | Behaviour |
|---|---|---|---|
| `GET` | `/api/v1/me/credentials` | submitter | Lists own metadata. No plaintext. |
| `PUT` | `/api/v1/me/credentials/{env_name}` | submitter | Body `UserCredentialUpsert` model (B.4.1). 400 if env_name not in allowlist. Returns metadata only. |
| `DELETE` | `/api/v1/me/credentials/{env_name}` | submitter | 200 idempotent (or 204 if no row existed). |
| `GET` | `/api/v1/admin/credentials` | admin | All users' metadata. |
| `GET` | `/api/v1/admin/credentials/{user_label}` | admin | One user's metadata. |
| `PUT` | `/api/v1/admin/credentials/{user_label}/{env_name}` | admin | Admin writes on user's behalf. **Same `env_name` allowlist enforced (B.5) — admins cannot bypass the LD_PRELOAD/PATH gate.** Audit tagged `admin_override=true`. |
| `DELETE` | `/api/v1/admin/credentials/{user_label}/{env_name}` | admin | Admin revokes. |

#### B.4.1 Request schema (`api/schemas.py`)

```python
class UserCredentialUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    value: str = Field(min_length=1, max_length=4096)
    notes: str | None = Field(default=None, max_length=512)
```

- `extra="forbid"` mirrors the rest of the API surface.
- `min_length=1` rejects empty strings before they can shadow a
  healthy DB credential (closes the empty-string footgun from v1
  review).

### B.5 Env-name allowlist

```python
ALLOWED_ENV_NAMES = frozenset({
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "ANTHROPIC_BEDROCK_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_REGION",
    "GOOGLE_APPLICATION_CREDENTIALS",
})
```

- Prevents `PATH`, `LD_PRELOAD`, `PYTHONPATH` weaponisation.
- Module-level constant. The drift snapshot test (B.8.1.e) pins
  the exact set; any future expansion forces a test-file edit
  + code review.
- **Enforced on BOTH `/me/credentials/{env_name}` AND
  `/admin/credentials/{user_label}/{env_name}` PUT routes.**
  Implementation: a shared `_validate_env_name(env_name)` helper
  in `api/routers/user_credentials.py` raises `HTTPException(400,
  {"detail": "env_name_not_allowed", "allowed": sorted(ALLOWED_ENV_NAMES)})`.
  Both route handlers MUST call this helper before reaching the
  store. Admin-route enforcement is pinned by
  B.8.3.j (`test_admin_put_rejects_env_name_outside_allowlist`).

### B.6 Manager auto-injection — keyed by AUTHENTICATED identity

This is the v2 critical fix. v1 wrongly keyed the lookup off the
spoofable `owner` field.

#### B.6.1 Threat model

- Bob authenticates as `dashboard-bob`. Alice has stored
  `ANTHROPIC_API_KEY=sk-alice-...` via her dashboard.
- Bob submits `POST /api/v1/sessions` with body
  `{credentials: {}, owner: "dashboard-alice", ...}`.
- **Required outcome:** Bob's session runs with whatever
  credentials Bob has stored (or the host's shell env if none),
  NOT with Alice's stored key. Bob never gains a way to consume
  Alice's Anthropic quota or have charges billed against her.

#### B.6.2 Implementation

`SessionManager.__init__` gains one optional collaborator:

```python
def __init__(
    self,
    *,
    ...,
    user_credentials_store: UserCredentialsStore | None = None,
):
    self._user_credentials_store = user_credentials_store
```

`SessionManager.submit` gains a new kwarg `actor_label`:

```python
async def submit(
    self,
    spec: SessionSpec,
    *,
    runtime_ctx: SessionRuntimeContext = _DEFAULT_RUNTIME_CTX,
    api_key_id: int | None = None,
    owner: str | None = None,
    actor_label: str | None = None,   # NEW — the AUTHENTICATED identity
    description: str | None = None,
) -> str:
    # ...existing intro...

    # NEW: merge per-user DB-stored credentials. Keyed by ACTOR,
    # NOT by owner. owner is a Plan 7 D7.26 attribution override
    # that any submitter can set; using it for credential lookup
    # would let Bob borrow Alice's keys by setting owner='alice'.
    # actor_label comes from request.state.api_key_label (router)
    # and is unforgeable.
    if (
        self._user_credentials_store is not None
        and actor_label
    ):
        try:
            db_creds = await self._user_credentials_store.get_for_user(actor_label)
        except Exception:  # never block a submit on a DB hiccup
            logger.warning(
                "user_credentials lookup failed for actor=%s",
                actor_label, exc_info=True,
            )
            db_creds = {}
        if db_creds:
            # API body credentials win — programmatic clients
            # (CI) and incident-response operators may need to
            # override a stale DB row from outside the dashboard.
            # Empty body credentials ({}) are normal (dashboard
            # form path); the spread leaves db_creds intact.
            merged = {**db_creds, **runtime_ctx.credentials}
            runtime_ctx = replace(runtime_ctx, credentials=merged)

    # ...continue with existing submit logic...
```

Router change (`api/routers/sessions.py`, `submit_session` —
verified current `sid = await manager.submit(...)` line is `173`):

```python
sid = await manager.submit(
    spec,
    runtime_ctx=ctx,
    api_key_id=api_key_id,
    owner=owner,                                              # attribution (overridable)
    actor_label=getattr(request.state, "api_key_label", None),  # auth (unforgeable)
    description=description,
)
```

#### B.6.2.bis Retry-path actor scoping (closing the v2 reviewer gap)

The repo has NO `POST /api/v1/sessions/{sid}/retry` route. The
ONLY retry entry point is `POST /api/v1/sessions/batch` with
`action="retry"`, which calls `manager.retry(sid, actor=label)`
(`api/routers/sessions.py:798`). `manager.retry` itself
(`session/manager.py:431-536`) accepts `actor: str | None`
documented as "the API key label of the user requesting the
retry" — i.e. semantically identical to the new `actor_label`
concept. **We REUSE the existing `actor` parameter as the
credentials-lookup key; no new parameter is introduced on
`retry()`.**

`manager.retry` body change (around current line 519):

```python
new_sid = await self.submit(
    retry_spec,
    owner=owner,
    description=new_description,
    parent_session_id=sid,
    actor_label=actor,         # NEW — forward the retrier's auth
                               # identity to the credentials merge
                               # path. ``actor`` here is the
                               # ``api_key_label`` of whoever hit
                               # the batch retry endpoint, NOT the
                               # original submitter — so Bob
                               # retrying Alice's session uses
                               # Bob's credentials, never Alice's.
)
```

Audit grep run BEFORE this PR lands, BEFORE every implementation
step, AND in CI (as a one-line `make actor-label-audit` target):

```bash
# Every manager.submit / manager.retry call site that builds a
# real session must either explicitly pass actor_label (router /
# manager-internal call sites) OR be a test fixture (clearly
# unauthenticated path).
rg -n 'manager\.(submit|retry)\(' src/gg_relay/api/routers/ src/gg_relay/session/manager.py
```

Today that audit yields three production call sites:

1. `api/routers/sessions.py:173` — `manager.submit(...)` in
   `submit_session`. Plan B.6.2 adds `actor_label=`.
2. `api/routers/sessions.py:798` — `manager.retry(sid, actor=label)`
   in the batch endpoint. **Unchanged at the router** — `label`
   already IS the authenticated identity. The fix is inside
   `manager.retry`, which now forwards `actor_label=actor` down
   into `self.submit(...)`.
3. `session/manager.py:519` — `self.submit(...)` inside
   `manager.retry`. **THIS is the v2 gap** — patched as shown
   above.

Any future PR that adds another call site is caught by the
`make actor-label-audit` CI check, which greps the audit list
and fails if a call site doesn't pass `actor_label=` (router) or
isn't inside `manager.retry` (the internal hop).

#### B.6.3 Why not also restrict `body.owner` to admin?

Plan 7 D7.26 explicitly allows any submitter to set `body.owner`
for the attribution use case (e.g. a CI bot submitting on behalf
of a developer). Locking that down would be a separate behaviour
change. The v2 fix keeps `body.owner` open for attribution but
removes it from the credential-lookup path. Attribution and
authentication are now properly decoupled.

#### B.6.4 Performance note (no cache)

- Submit rate ceiling: dashboard form + API combined, realistic
  upper bound ~10/s per relay process (manager's existing
  `_max_concurrent_sessions` is the dominant throttle).
- `get_for_user` is a single PK index seek + N decryptions where
  N = stored env names per user (realistic: 1–4). Fernet decrypt
  on a 64-byte ciphertext is <50µs. Total path <2ms.
- Submit end-to-end latency dominated by SDK subprocess spawn
  (~300–800ms). Adding 2ms is invisible.
- v2 explicitly DROPS the v1 plan's "TTL=60s + KeyInvalidated"
  cache machinery. The existing `KeyInvalidated` event (`cluster/
  key_invalidate.py`) is hard-coded to refresh
  `app.state.dashboard_internal_keys` — it cannot be reused
  without rewriting its subscriber. No-cache means no
  invalidation machinery needed, no new event class needed, no
  multi-worker correctness story to maintain. Re-introduce a
  cache only if measured load demands it.

### B.7 Dashboard pages

**`/dashboard/me/credentials`** (any logged-in user):

- Table columns: `env_name | updated_at | created_by_label | notes | actions`
  — **no plaintext, no preview, no masked preview**. Once
  encrypted the value is opaque to the UI. (Removes the v1
  "sk-…ant-****" preview that had no defined storage
  semantics.) If the user wants to verify a key works, they
  submit a session.
- Form: `select env_name` (allowlist) + `textarea value` +
  `notes` + `Save` → PUT via HTMX, swap the row.
- `Delete` button → DELETE + row removal.
- Warning banner when feature disabled
  (`warn_user_credentials_disabled` flag from lifespan):
  "Operator has not configured encryption; your credentials
  cannot be saved. Ask an admin to set
  RELAY_CREDENTIALS_ENCRYPTION_KEY."

**`/dashboard/admin/credentials`** (admin only — gated by
`_dashboard_role(request) == "admin"`):

- Table grouped by `user_label`.
- "Manage as user" inline form, yellow banner: "Setting another
  user's credential bypasses their consent. Action is logged."
- Optional "Show bricked credentials" tab calling
  `list_bricked()` (uses the same surface as the CLI helper).

**Sidebar entries**: under "Settings" cluster, next to "API keys".

### B.8 Tests

#### B.8.1 Store unit (`tests/unit/store/test_user_credentials_store.py`)

a. `test_upsert_then_get_round_trip` — value decrypts identical.
b. `test_upsert_idempotent_on_label_env_pair` — second write overwrites.
c. `test_get_unknown_returns_empty_dict`.
d. `test_list_for_user_no_plaintext_in_metadata`.
e. `test_allowed_env_names_snapshot` — assert
   `ALLOWED_ENV_NAMES == frozenset({...exact set...})`. Forces a
   conscious PR change to add `PATH`/`LD_PRELOAD`-style keys.
f. `test_delete_is_idempotent`.
g. `test_no_fernet_short_circuits_get_returns_empty`.
h. `test_key_fingerprint_recorded_on_upsert`.
i. `test_get_skips_row_with_mismatched_fingerprint` — manually
   insert a row whose `key_fingerprint` doesn't match the store's
   current fernet; `get_for_user` returns `{}` (or partial — see
   sub-case j) AND logs a warning.
j. `test_get_returns_partial_when_one_row_is_bricked` — alice has
   two rows, only one with mismatched fingerprint. `get_for_user`
   returns only the good row, logs warning for the bricked one.
k. `test_list_bricked_returns_only_mismatched_rows`.
l. `test_get_skips_row_when_decrypt_raises_invalid_token` —
   simulate a Fernet `InvalidToken` (tampered ciphertext) and
   confirm the row is skipped, logged, not raised.

#### B.8.2 Manager integration (`tests/integration/test_manager_credentials_merge.py`)

a. `test_db_creds_injected_for_actor_with_no_runtime_ctx` —
   alice has `ANTHROPIC_API_KEY=sk-db`; submit with empty body
   creds and `actor_label='dashboard-alice'` → SDK env stub sees
   `sk-db`.
b. `test_api_body_credentials_override_db_creds`.
c. **`test_actor_owner_decoupling_prevents_credential_borrowing`**
   — Bob has NO DB credentials; Alice has `sk-alice`. Submit with
   `actor_label='dashboard-bob'`, `owner='dashboard-alice'`.
   Assert SDK env has NO `ANTHROPIC_API_KEY` (or whatever Bob's
   host env had — pin "alice's sk-alice does NOT appear"). This
   is the v2 critical-fix regression test.
d. `test_no_actor_skips_db_lookup` — `actor_label=None` (e.g.
   unauthenticated test path); no lookup, no crash.
e. `test_feature_disabled_falls_through` —
   `user_credentials_store=None`; submit unchanged.
f. `test_lookup_failure_does_not_block_submit` — store raises;
   log warning, fall through; session still created.
g. `test_retry_uses_retrier_actor_for_creds_not_original_submitter`
   — alice submitted, bob retries with admin permission; bob's
   creds (not alice's) are merged. Pins the retry path uses
   `actor_label` of the retrier, parallel to submit.

#### B.8.3 API integration (`tests/integration/test_user_credentials_api.py`)

a. `test_me_creds_anonymous_returns_401`.
b. `test_me_creds_submitter_can_only_see_own`.
c. `test_me_put_round_trips_metadata_only` — response has no plaintext, no preview.
d. `test_me_put_rejects_env_name_outside_allowlist` — 400
   `{"detail": "env_name_not_allowed", "allowed": [...]}`.
e. `test_me_put_rejects_empty_value` — 422 from pydantic
   `min_length=1` (closes empty-string override footgun).
f. `test_admin_creds_lists_all_users`.
g. `test_admin_put_creates_audit_row_with_admin_override_flag` —
   AND assert `user_credentials.created_by_label` equals the
   admin's label, not the target user's label.
h. `test_feature_disabled_returns_503`.
i. `test_value_never_in_response_or_log` — capture caplog +
   response.body; assert the raw value string doesn't appear in
   either (defense-in-depth against accidental f-string log
   leakage).
j. `test_admin_put_rejects_env_name_outside_allowlist` — admin
   POSTs `/api/v1/admin/credentials/dashboard-bob/LD_PRELOAD`
   with a valid body; expect 400 `{"detail":
   "env_name_not_allowed", "allowed": [...]}`. Pins that admin
   role does NOT bypass the allowlist (closes the v2 reviewer
   gap where the allowlist was only tested on `/me/`).
k. `test_admin_put_rejects_path_env_name` — same with `PATH`,
   for explicit defense against the most obvious weaponisation
   vector.

#### B.8.4 Dashboard integration (`tests/integration/test_dashboard_credentials.py`)

a. `test_me_credentials_page_loads_for_submitter`.
b. `test_admin_credentials_page_403_for_submitter`.
c. `test_admin_credentials_page_loads_for_admin`.
d. `test_legacy_admin_can_open_credentials_page` — pins the
   gate uses `_dashboard_role(request)`, not raw `role_map.get`,
   so legacy admin (`dashboard_admin_password` only) works.
e. `test_disabled_feature_shows_warning_banner`.
f. `test_htmx_put_swaps_row_in_place` — POST the form fragment,
   assert response body contains the new row markup with
   `hx-swap-oob` or equivalent (pins the HTMX swap contract).

### B.9 Audit + observability

- Every mutation writes `audit_log`:
  - `action="user_credentials_upsert"` / `"user_credentials_delete"`
  - `target_type="user_credentials"`,
    `target_id=f"{user_label}:{env_name}"`
  - `metadata_json` includes `env_name`, `admin_override` (bool),
    `had_previous` (bool).
  - `actor` = `request.state.api_key_label` (always present
    after API-key middleware).
- `env_name` IS allowed to land in logs/audit (it's the secret
  *name*, not the secret value). Verified against
  `redaction/engine.py:88` — the redactor matches the literal
  key `'credentials'`, not `'env_name'`, so audit rows render
  cleanly.
- Store layer hard-guards: any code path that would log the
  decrypted value first passes through a sentinel check that
  raises in dev/test (`assert RELAY_ALLOW_VALUE_LOGGING is
  False`). Belt + suspenders.

### B.10 Migration order + rollback

- Alembic `0013` (down_revision=`0012`). Schema-only. Rollback:
  drop table.
- Code is feature-flagged via `credentials_encryption_key is None`
  → store fernet=None → manager skips merge → routes 503. So a
  deployment can ship the binary, defer the migration, and the
  feature stays dark.

---

## C. Cross-cutting risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | A breaks shell-env-inheritance deployments | Spike-confirmed SDK merges; A.5 test `test_no_credentials_keeps_env_empty` |
| R2 | Manager learns framework-flavoured collaborator | Store is plain SQLAlchemy+Fernet, manager gets one optional kwarg + one new `actor_label` kwarg; no Starlette imports |
| R3 | Encryption-key loss/rotation | `key_fingerprint` per row; `gg-relay list-bricked-credentials` CLI; runtime `get_for_user` skips mismatched rows gracefully (B.8.1.i-l) |
| R4 | Owner-impersonation → credential exfiltration | **v2 fix:** lookup keyed by `actor_label` (unforgeable), not `owner`; pinned by `test_actor_owner_decoupling_prevents_credential_borrowing` (B.8.2.c) |
| R5 | Allowlist drift | Snapshot test B.8.1.e; PR review forced |
| R6 | XSS via env value rendering | Jinja2 autoescape on; UI never renders the value (only metadata) |
| R7 | Multi-worker cache incoherence | v2/v3 ship NO cache — each submit hits DB. No incoherence to manage. Latency budget B.6.4 shows <2ms tax. |
| R8 | Empty-string value silently shadows healthy DB row | Pydantic `min_length=1` (B.4.1); B.8.3.e pins the 422 |
| R9 | Dashboard cookie users can't reach `/api/v1/me/credentials` | Existing `DashboardCookieMiddleware` injects synthetic `X-API-Key`; covered by B.8.4 page-load tests |
| R10 | Retry path bypasses actor scoping | **v3 fix:** `manager.retry` now forwards its existing `actor` param into `self.submit(..., actor_label=actor)` (B.6.2.bis); `make actor-label-audit` CI target catches any future call site that forgets the kwarg; B.8.2.g pins the runtime behaviour |
| R11 | Plan latency claim untested | R7's "<2ms tax" is a back-of-envelope from Fernet decrypt (<50µs/row) + Postgres PK seek (<1ms). Not test-pinned. If the live deployment shows otherwise, re-introduce the cache per the v1 design (the schema doesn't need to change). |

---

## D. Execution order

1. **A** lands (zero-dependency, fixes the bug standalone).
2. **D.pre — dependency add**:
   - Add `cryptography>=42` to `pyproject.toml` under
     `[project.dependencies]` (verified `rg cryptography
     pyproject.toml` currently returns no match — Fernet is NOT
     yet a transitive dep).
   - Refresh `uv.lock` / re-run `pip-compile` so reproducible
     builds work.
   - Add `cryptography` to `requirements.txt` if the repo
     publishes a flat requirements file (verify per current
     packaging convention).
   - One-line CI smoke: `python -c "from cryptography.fernet
     import Fernet; Fernet.generate_key()"` so a missing dep
     fails the test job before any user-credentials test does.
3. **B.1** alembic 0013 + **B.3** store (with fernet=None defaults,
   so import-only behaviour). Store module-level
   `from cryptography.fernet import Fernet, InvalidToken` — the
   import itself is what breaks today without step 2.
4. **B.2** Config fields + lifespan wiring (`gg-relay generate-encryption-key`
   CLI added in same step).
5. **B.6** manager merge (with `actor_label` plumbed from router
   AND from `manager.retry` per B.6.2.bis).
6. **B.6.bis — actor-label CI audit**:
   - Add `make actor-label-audit` target that runs the rg from
     B.6.2.bis and fails CI if a new `manager.submit(...)` call
     site appears without `actor_label=`. This is the
     auto-regression net for the v2 retry-bypass class of bug.
7. **B.4** API routes (both `/me/` AND `/admin/` invoke the
   shared `_validate_env_name` helper per B.5).
8. **B.7** dashboard pages.
9. `gg-relay list-bricked-credentials` CLI (small, can ship in 3 or 6).
10. **B.8** test files alongside each step; no step ships without
    its tests green.

Each step is independently revertable. Each step extends — does
not modify — the public surface of the preceding step.

---

## E. Documentation deltas

- `docs/team-deployment.md` — new section "Per-user upstream
  credentials" covering: `gg-relay generate-encryption-key`,
  `RELAY_CREDENTIALS_ENCRYPTION_KEY`,
  `RELAY_DISABLE_USER_CREDENTIALS`, the actor-not-owner
  authorization model, and `gg-relay list-bricked-credentials`
  for rotation forensics.
- `docs/api.md` — `/api/v1/me/credentials` +
  `/api/v1/admin/credentials` reference; explicit "credentials
  are scoped to the authenticated identity, not `body.owner`"
  callout.
- `docs/dashboard-ux-copy.md` — credentials page strings added.
- `CHANGELOG.md` — two entries:
  - `Fixed: inprocess executor now honours
    runtime_ctx.credentials (was silently discarded).`
  - `Added: per-user upstream credentials self-service (Anthropic /
    Bedrock / Vertex env vars). Credentials are scoped to the
    authenticated identity and never to body.owner.`
- `.env.example` — `RELAY_CREDENTIALS_ENCRYPTION_KEY=` (commented
  with `gg-relay generate-encryption-key` hint) +
  `RELAY_DISABLE_USER_CREDENTIALS=false`.

---

## Appendix Z. v1 → v2 → v3 changelog (for reviewers who saw earlier rounds)

### v2 → v3 deltas (Santa round 2 → round 3)

Resolved Santa-round-2 critical issues:

1. **Retry path silently bypasses actor scoping** → B.6.2.bis
   added. `manager.retry` REUSES its existing `actor` parameter
   (semantically already "API key label of the retrier") by
   forwarding it as `actor_label=actor` into the inner
   `self.submit(...)`. No new public parameter, no API churn.
   Pinned by B.8.2.g and protected by the `make actor-label-audit`
   CI grep (D.6).
2. **Phantom `/api/v1/sessions/{sid}/retry` route** → removed
   from B.6.2 (route does not exist). B.6.2.bis enumerates the
   ACTUAL call sites: `submit_session` router, batch retry
   router, and the in-manager `self.submit` hop.
3. **Missing `cryptography` dependency** → D.2 ("dependency add")
   added as an explicit pre-step BEFORE the store. CI smoke test
   added so a missing dep fails before any credential test runs.
4. **Admin PUT bypasses allowlist** → B.4 admin PUT row notes
   "Same `env_name` allowlist enforced"; B.5 specifies a shared
   `_validate_env_name` helper used by BOTH `/me/` and `/admin/`
   routes; B.8.3.j and B.8.3.k pin the admin-route 400 for
   `LD_PRELOAD` and `PATH`.
5. **Bogus `test_client_install_root_env.py` citation** → A.3
   divergence-table cell rewritten to describe the existing
   inprocess convention without claiming a non-existent test
   pins it; A.5 adds `test_extra_env_overrides_claude_root_setdefault`
   to BACKFILL the missing regression net for this divergence.

Resolved Santa-round-2 non-blocking suggestions:

- B.3 `upsert` docstring now specifies the `ON CONFLICT DO
  UPDATE SET ... created_by_label = EXCLUDED.created_by_label`
  semantics so the admin-override contract is locked in code,
  not just in the plan.
- R7 cross-reference corrected (`A.6.4` → `B.6.4`).

### v1 → v2 deltas

Resolved Santa-round-1 critical issues:

1. **Alembic 0014 → 0013**, `down_revision='0012'`. Plan 9 keys
   table is in 0012, not 0013. Fixed B.1, B.10, D.
2. **Credential-borrowing via `body.owner`** → `SessionManager.submit`
   now takes a new `actor_label` kwarg; lookup is keyed by the
   unforgeable authenticated identity. Threat model in B.6.1,
   pinning test in B.8.2.c. Router change in B.6.2.
3. **A's "byte-for-byte aligned with docker" claim** → A.1
   reworded; A.3 explicitly documents the pre-existing
   `RELAY_TRACE_ID` divergence as out-of-scope; A.6 keeps the
   compatibility claim accurate.
4. **R4 `KeyInvalidated` reuse** was fiction → v2 ships
   **no cache**. Latency budget in B.6.4 shows <2ms tax. R7
   rewritten.

Resolved Santa-round-1 non-blocking suggestions:

- B.7 "sk-…ant-****" preview removed (no storage strategy was
  ever defined; v1 just had `created_by_label` and `updated_at`
  as the human-readable hint).
- B.8.1.e — allowlist drift snapshot test enumerated.
- B.8.1.i-l — fingerprint mismatch + InvalidToken read-side
  tests enumerated.
- B.4.1 — pydantic `UserCredentialUpsert` model with
  `extra="forbid"`, `min_length=1` defined.
- B.6.4 — explicit no-cache + perf rationale.
- D — `gg-relay generate-encryption-key` + `gg-relay
  list-bricked-credentials` added to execution order.
- E — same CLI tools added to docs deltas.
- F section removed (Goals §0 now states independence directly).
