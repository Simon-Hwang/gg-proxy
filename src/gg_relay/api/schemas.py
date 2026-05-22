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

from pydantic import BaseModel, ConfigDict, Field


class PluginManifestIn(BaseModel):
    """Mirrors :class:`gg_relay.session.spec.PluginManifest` over the wire."""

    model_config = ConfigDict(extra="forbid")

    profile: Literal["minimal", "core", "go", "python", "full"] | None = None
    modules: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    with_components: list[str] = Field(default_factory=list)
    without_components: list[str] = Field(default_factory=list)
    extra_env: list[tuple[str, str]] = Field(default_factory=list)


class SessionSpecIn(BaseModel):
    """Public-facing :class:`SessionSpec` shape (no credentials)."""

    model_config = ConfigDict(extra="forbid")

    prompt: str
    cwd: str
    plugins: PluginManifestIn
    executor: Literal["docker", "inprocess"] = "docker"
    timeout_s: int = 1800
    tags: list[str] = Field(default_factory=list)


class SessionSubmitRequest(BaseModel):
    """POST /api/v1/sessions body.

    ``credentials`` is an out-of-spec key absorbed straight into the
    :class:`SessionRuntimeContext`; the API NEVER persists or echoes it
    back. ``trace_id`` lets the caller correlate via OTel.
    """

    model_config = ConfigDict(extra="forbid")

    spec: SessionSpecIn
    credentials: dict[str, str] = Field(default_factory=dict)
    trace_id: str | None = None


class SessionResponse(BaseModel):
    """A single session row, redacted, safe to return.

    Notice the absence of any ``credentials`` field — by design.
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


class SessionListResponse(BaseModel):
    sessions: list[SessionResponse]
    total: int


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
