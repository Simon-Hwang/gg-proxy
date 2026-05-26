"""Pydantic IO schemas for the public REST API.

Naming convention:
- ``*Request``  — what the client POSTs.
- ``*Response`` — what we return.

Security guarantee: ``SessionResponse`` and friends MUST never carry
``credentials``. The runtime context is constructed from the request body
inside the router and injected straight into the SessionManager; no
serialiser pulls it back out.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ── pre_run_cmds 校验上限 ────────────────────────────────────────────
# 设计目标：把 API body 注入到容器内执行的能力收敛到一个可审计的小窗口。
PRE_RUN_CMDS_MAX_COUNT = 20
PRE_RUN_CMDS_MAX_ARGV_LEN = 32
PRE_RUN_CMDS_MAX_TOKEN_LEN = 200


class PluginManifestIn(BaseModel):
    """Mirrors :class:`gg_relay.session.spec.PluginManifest` over the wire."""

    model_config = ConfigDict(extra="forbid")

    profile: Literal["minimal", "core", "go", "python", "full"] | None = None
    modules: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    with_components: list[str] = Field(default_factory=list)
    without_components: list[str] = Field(default_factory=list)
    extra_env: list[tuple[str, str]] = Field(default_factory=list)
    # Per-session pre-run argv list. 每条命令是 argv 字符串数组，无 shell。
    # 仅 docker executor 下生效（SessionSpecIn 的 model_validator 强制）。
    pre_run_cmds: list[list[str]] = Field(
        default_factory=list,
        max_length=PRE_RUN_CMDS_MAX_COUNT,
        description=(
            "Sequential argv commands executed inside the runner container "
            "before the SDK starts (e.g. git fetch / git worktree add). "
            "Each entry is an argv list (no shell). Docker executor only."
        ),
    )

    @field_validator("pre_run_cmds")
    @classmethod
    def _validate_pre_run_cmds(
        cls, v: list[list[str]]
    ) -> list[list[str]]:
        for cmd in v:
            if not cmd:
                raise ValueError("pre_run_cmds entry must be a non-empty argv")
            if len(cmd) > PRE_RUN_CMDS_MAX_ARGV_LEN:
                raise ValueError(
                    f"pre_run_cmds argv exceeds {PRE_RUN_CMDS_MAX_ARGV_LEN} tokens"
                )
            for token in cmd:
                if not isinstance(token, str):
                    raise ValueError("pre_run_cmds tokens must be strings")
                if len(token) > PRE_RUN_CMDS_MAX_TOKEN_LEN:
                    raise ValueError(
                        f"pre_run_cmds token exceeds "
                        f"{PRE_RUN_CMDS_MAX_TOKEN_LEN} chars"
                    )
                # NUL 字符在 exec 中不能传递，提前拒绝。
                if "\x00" in token:
                    raise ValueError("pre_run_cmds tokens must not contain NUL")
        return v


class SessionSpecIn(BaseModel):
    """Public-facing :class:`SessionSpec` shape (no credentials)."""

    model_config = ConfigDict(extra="forbid")

    prompt: str
    cwd: str
    plugins: PluginManifestIn
    executor: Literal["docker", "inprocess"] = "docker"
    timeout_s: int = 1800
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _enforce_pre_run_executor(self) -> SessionSpecIn:
        # pre_run_cmds 在 inprocess 下会直接在 gg-relay 宿主机进程执行 argv，
        # 绕过 ToolPolicy/HITL，本期出于"安全是 P0"原则只允许 docker。
        # 后续若启用，需要 admin-only 配置开关 + allowlist。
        if self.executor != "docker" and self.plugins.pre_run_cmds:
            raise ValueError(
                "pre_run_cmds is only supported with executor='docker'; "
                "inprocess sessions execute argv on the host and are blocked "
                "in this release for safety."
            )
        return self


class SessionSubmitRequest(BaseModel):
    """POST /api/v1/sessions body.

    ``credentials`` is an out-of-spec key absorbed straight into the
    :class:`SessionRuntimeContext`; the API NEVER persists or echoes it
    back. ``trace_id`` lets the caller correlate via OTel.

    Plan 7 Task 6b / D7.26 — ``owner`` and ``description`` are optional
    collaboration metadata. When ``owner`` is omitted the router
    auto-attributes it from ``request.state.api_key_label`` (set by
    :class:`APIKeyAuthMiddleware`) so existing clients gain attribution
    without code changes. ``description`` is bounded at 512 chars on
    the way in via :class:`pydantic.Field`'s ``max_length`` so an
    over-long body is rejected at validation time (the router also
    applies a defensive in-place truncation as a belt-and-braces
    fallback for clients bypassing schema validation).
    """

    model_config = ConfigDict(extra="forbid")

    spec: SessionSpecIn
    credentials: dict[str, str] = Field(default_factory=dict)
    trace_id: str | None = None
    owner: str | None = None
    description: str | None = Field(default=None, max_length=512)


class SessionResponse(BaseModel):
    """A single session row, redacted, safe to return.

    Notice the absence of any ``credentials`` field — by design.

    Plan 7 Task 6b / D7.26 — ``owner`` and ``description`` echo the
    persisted collaboration metadata. ``owner`` is the auto-attributed
    or operator-supplied label; ``description`` is the (possibly
    truncated) free-form annotation. Truncation is signalled by the
    ``X-Description-Truncated: true`` response header (not in the body
    so machine clients can dispatch on header alone).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    status: str
    spec: dict[str, Any]
    tags: list[str]
    submitted_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    end_reason: str | None = None
    backend: str
    trace_id: str | None = None
    owner: str | None = None
    description: str | None = None


class SessionListResponse(BaseModel):
    """``GET /api/v1/sessions`` response with cursor pagination.

    Plan 7 D7.6 / Task 9. Carries two payload field families side-by-side
    so the wire-shape upgrade is non-breaking:

    * **New / preferred** — ``items`` + ``next_cursor``. Pass
      ``next_cursor`` back as ``?after=...`` to fetch the next page;
      ``None`` once the result set is exhausted.
    * **Deprecated (kept until 0.8.0)** — ``sessions`` is a verbatim
      alias of ``items``; ``total`` is a ``-1`` sentinel meaning
      "not computed" (the cursor design intentionally avoids the
      ``COUNT(*)`` scan that ``total`` used to require). Clients
      should migrate to ``items`` + ``next_cursor`` before 0.8.0.
    """

    items: list[SessionResponse]
    next_cursor: str | None = None
    sessions: list[SessionResponse]
    total: int = -1


# ── Plan 8 D8.20 / Task 12 — session search ──────────────────────────


class SearchSessionItem(BaseModel):
    """One row in :class:`SearchSessionsResponse.items`.

    Plan 8 D8.20 / Task 12. Carries the subset of the session row the
    search UI cares about — no frames, no spec dump — so the response
    stays compact even at the 200-row cap. ``prompt`` is the original
    spec prompt extracted from ``spec_json`` (best-effort: empty
    string when the row predates the prompt field).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    prompt: str = ""
    owner: str | None = None
    description: str | None = None
    status: str
    tags: list[str] = Field(default_factory=list)
    submitted_at: datetime
    ended_at: datetime | None = None


class SearchSessionsResponse(BaseModel):
    """``GET /api/v1/sessions/search`` response with cursor pagination.

    Mirrors the audit endpoint's ``items`` + ``next_cursor`` +
    ``has_more`` shape (Plan 8 Task 6 / D8.4) so dashboards reuse the
    same lazy-load infinite-scroll component.
    """

    model_config = ConfigDict(extra="forbid")

    items: list[SearchSessionItem]
    next_cursor: str | None = None
    has_more: bool = False


class FrameOut(BaseModel):
    seq: int
    ts: datetime
    type: str
    payload: dict[str, Any]


class SessionDetailResponse(SessionResponse):
    runtime_id: str | None = None
    frames: list[FrameOut] = Field(default_factory=list)


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = "user_request"


class PauseRequest(BaseModel):
    """POST /api/v1/sessions/{id}/pause body. Both fields are optional —
    a plain ``{}`` is the most common case (operator hits the pause
    button with no annotation). The ``reason`` is propagated into the
    SessionStateChanged event so dashboards / Feishu can show why."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = None


class ResumeRequest(BaseModel):
    """POST /api/v1/sessions/{id}/resume body. ``hint`` is forwarded to
    the SDK's ``client.query(hint)`` continuation — typically a free-form
    instruction nudging the agent in a new direction."""

    model_config = ConfigDict(extra="forbid")

    hint: str | None = None


class HITLResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["accept", "deny"]
    reason: str | None = None
    resolver: str | None = None


class HITLPendingItem(BaseModel):
    req_id: str
    tool: str
    args: dict[str, Any]


class HITLPendingResponse(BaseModel):
    session_id: str
    pending: list[HITLPendingItem]


# ── Plan 8 D8.6 / Task 9 — batch session/hitl operations ─────────────
# Batch endpoints share a common shape so the dashboard's bulk action
# toolbar (Task 10) can render a uniform progress UI regardless of
# which surface it targets.
#
# Status semantics:
#   * ``ok``    — the per-id action ran cleanly. ``new_session_id`` is
#                 populated for ``retry`` so the UI can link the new
#                 session immediately.
#   * ``error`` — the per-id action raised; ``error_code`` machine-
#                 readable identifier (mirrors the SDKError /
#                 hitl_already_resolved taxonomy) + ``error_message``
#                 free-form. Other ids in the same request still
#                 succeed — partial success is the explicit contract.
#
# Why 200 with a items array (not 207 Multi-Status): the HTTP layer
# stays simple, the cursor's batch endpoint precedent (Plan 7 D7.6)
# already established the "200 + per-item status" pattern, and
# machine clients can dispatch on ``summary.error > 0`` without
# parsing a non-standard 207 body.
#
# Caps: ``ids`` ≤ 100 for sessions, ≤ 50 for HITL. The HITL cap is
# tighter because each resolve hits the optimistic-lock path
# (Plan 7 D7.5) and we don't want a single batch holding 100 row
# locks at once. Both caps are pydantic ``max_length`` so an
# over-sized payload 422s before the router runs.


class BatchSessionRequest(BaseModel):
    """POST /api/v1/sessions/batch body (Plan 8 D8.6 / Task 9).

    ``ids`` carries the session uuids to act on — between 1 and 100
    inclusive. ``action`` is one of the two supported batch actions:

      * ``cancel`` — admin OR own-session for each id; failures are
        reported per-id (e.g. cross-owner submitter sees
        ``error_code='forbidden_cancel'``).
      * ``retry``  — submitter+; the manager rebuilds the spec and
        submits a fresh session whose ``parent_session_id`` points
        at the original. The new sid lands in
        :class:`BatchSessionItem.new_session_id`.

    ``reason`` is propagated to the audit row metadata so operators
    can leave a freeform note ("paused for cluster maintenance",
    "retrying after upstream fix"). Truncated to 200 chars at
    validation time.
    """

    model_config = ConfigDict(extra="forbid")

    ids: list[str] = Field(..., max_length=100, min_length=1)
    action: Literal["cancel", "retry"]
    reason: str | None = Field(None, max_length=200)


class BatchSessionItem(BaseModel):
    """One row in :class:`BatchSessionResponse.items`.

    The status / error_code split lets clients render a per-id
    progress checklist without parsing free-form messages: tick
    every ``ok`` row, group ``error`` rows by ``error_code`` to
    show "3 not found, 1 forbidden". ``new_session_id`` is only
    populated for ``retry`` actions.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    status: Literal["ok", "error"]
    error_code: str | None = None
    error_message: str | None = None
    new_session_id: str | None = None


class BatchSessionResponse(BaseModel):
    """POST /api/v1/sessions/batch response.

    Always 200 with per-id ``items``. ``summary`` is a precomputed
    ``{"ok": N, "error": M}`` dict so dashboards can render a status
    bar without re-counting client-side.
    """

    model_config = ConfigDict(extra="forbid")

    items: list[BatchSessionItem]
    summary: dict[str, int]


class BatchHITLRequest(BaseModel):
    """POST /api/v1/hitl/batch body (Plan 8 D8.6 / Task 9).

    ``ids`` carries the FULL HITL request ids (``"{session_id}:{short}"``)
    as returned by ``GET /api/v1/sessions/{sid}/hitl/pending``. The
    batch endpoint does not auto-namespace short ids because a batch
    typically spans multiple sessions.

    ``action`` is one of:
      * ``approve`` — mapped to the coordinator's ``"accept"`` decision.
      * ``reject``  — mapped to the coordinator's ``"deny"`` decision.

    The ``approve``/``reject`` wording is the user-facing surface
    consistent with the dashboard toolbar; the coordinator's internal
    Literal['accept', 'deny'] is the wire enum the runner consumes.

    Capped at 50 ids per request (tighter than session batch because
    each resolve hits the optimistic-locking path).
    """

    model_config = ConfigDict(extra="forbid")

    ids: list[str] = Field(..., max_length=50, min_length=1)
    action: Literal["approve", "reject"]
    reason: str | None = Field(None, max_length=200)


class BatchHITLItem(BaseModel):
    """One row in :class:`BatchHITLResponse.items`.

    ``error_code`` mirrors the single-resolve endpoint's taxonomy:

      * ``hitl_not_pending``     — request not currently pending in
        the in-process coordinator (already drained).
      * ``hitl_already_resolved`` — DB row shows a previous winning
        decision (cross-worker race or post-resolve replay).
      * ``internal_error``       — anything else; ``error_message``
        carries the original exception's message.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    status: Literal["ok", "error"]
    error_code: str | None = None
    error_message: str | None = None


class BatchHITLResponse(BaseModel):
    """POST /api/v1/hitl/batch response.

    Mirrors :class:`BatchSessionResponse` — same partial-success
    contract, same ``summary`` shape — so the dashboard can reuse
    a single component for both batch toolbars.
    """

    model_config = ConfigDict(extra="forbid")

    items: list[BatchHITLItem]
    summary: dict[str, int]


# ── Plan 8 D8.30 / Task 23 — per-owner cost attribution ──────────────
# Three response models powering the ``/api/v1/cost/*`` router and
# the dashboard cost page. Each carries explicit float fields rather
# than a generic ``dict[str, Any]`` so OpenAPI clients (and the
# golden snapshot in ``docs/openapi.snapshot.json``) document the
# wire shape exactly. The ``total_cost_usd`` field is non-Optional
# because the underlying ``sessions.cost_usd`` column is NOT NULL
# with a 0 default — a row that never recorded a cost surfaces as
# ``0.0`` rather than ``null`` so downstream consumers (charts, CSV
# diff) don't have to special-case the absent case.


class OwnerCostSummary(BaseModel):
    """One row in :class:`OwnerCostResponse.items`.

    ``owner`` is the API key / dashboard label; ``None`` when the
    session was submitted before Plan 7 D7.26 added the owner
    column (legacy rows). The dashboard renders ``None`` as
    ``"—"`` to keep the table column aligned.
    """

    model_config = ConfigDict(extra="forbid")

    owner: str | None = None
    session_count: int = 0
    total_cost_usd: float = 0.0


class OwnerCostResponse(BaseModel):
    """``GET /api/v1/cost/per-owner`` response.

    ``from_ts`` / ``to_ts`` echo the request filters (ISO 8601) so
    a client can confirm the window the totals were computed for
    without re-parsing the query string.
    """

    model_config = ConfigDict(extra="forbid")

    items: list[OwnerCostSummary]
    from_ts: str | None = None
    to_ts: str | None = None


class SessionCostBreakdown(BaseModel):
    """One row in :class:`SessionCostListResponse.items`.

    Compact shape — only the fields the cost breakdown table needs;
    no spec dump or frames. ``ended_at`` is included so the dashboard
    can render the elapsed wall-clock alongside cost for incomplete
    runs that already accumulated tokens.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    owner: str | None = None
    status: str
    submitted_at: datetime
    ended_at: datetime | None = None
    total_cost_usd: float = 0.0


class SessionCostListResponse(BaseModel):
    """``GET /api/v1/cost/per-session`` response.

    Cursor pagination shape mirrors :class:`SessionListResponse` so
    a future ``after=...`` upgrade is non-breaking; the Task-23 MVP
    always returns ``next_cursor=None``.
    """

    model_config = ConfigDict(extra="forbid")

    items: list[SessionCostBreakdown]
    next_cursor: str | None = None


class UserCostSummary(BaseModel):
    """``GET /api/v1/cost/summary`` response.

    ``team_total_cost_usd`` is admin-only — non-admin callers see
    ``None``. The dashboard renders it as the "team" stat next to
    the per-user stat so admins can compare their personal share
    against the team total at a glance.
    """

    model_config = ConfigDict(extra="forbid")

    user: str
    role: str
    period: str
    from_ts: str
    session_count: int = 0
    total_cost_usd: float = 0.0
    team_total_cost_usd: float | None = None
