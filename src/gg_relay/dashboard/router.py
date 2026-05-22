"""HTMX dashboard router.

Authentication is a single shared admin account (D4.11); the password is
loaded from ``Config.dashboard_admin_password`` and compared with
``secrets.compare_digest``. The session middleware (added by the parent
app, NOT here) signs cookies with ``Config.dashboard_session_secret``.

The router rejects any non-``/dashboard/login`` request that lacks a
valid session cookie. All rendered values come from the redacted
session-detail payload — there is no raw spec, frame, or credential
template variable; the only string-data fields are pre-masked by
:class:`RedactionEngine` upstream.
"""
from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import SecretStr

from gg_relay.api.deps import get_coordinator, get_manager
from gg_relay.session.hitl.coordinator import HITLCoordinator, HITLNotPending
from gg_relay.session.manager import SessionManager, SessionNotFound

_HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

SESSION_USER_KEY = "user"


def _require_session(request: Request) -> None:
    """Reject if the session cookie does not carry an authenticated user."""
    user = request.session.get(SESSION_USER_KEY) if hasattr(
        request, "session"
    ) else None
    if not user:
        raise HTTPException(
            status_code=303, headers={"Location": "/dashboard/login"}
        )


# Module-level ``Depends`` instances — using them as default args keeps
# ruff B008 happy (no inline function calls in defaults).
_RequireSessionDep = Depends(_require_session)
_ManagerDep = Depends(get_manager)
_CoordinatorDep = Depends(get_coordinator)


@router.get("/login", response_class=HTMLResponse)
async def login_get(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "login.html", {"error": None}
    )


@router.post("/login")
async def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> Any:
    cfg = request.app.state.config
    admin_pw: SecretStr | None = getattr(cfg, "dashboard_admin_password", None)
    if (
        admin_pw is None
        or username != "admin"
        or not secrets.compare_digest(password, admin_pw.get_secret_value())
    ):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "invalid credentials"},
            status_code=401,
        )
    request.session[SESSION_USER_KEY] = username
    return RedirectResponse(url="/dashboard/sessions", status_code=303)


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    request.session.pop(SESSION_USER_KEY, None)
    return RedirectResponse(url="/dashboard/login", status_code=303)


@router.get("/sessions", response_class=HTMLResponse)
async def sessions_list(
    request: Request,
    _: None = _RequireSessionDep,
    manager: SessionManager = _ManagerDep,
) -> HTMLResponse:
    rows = await manager.list(limit=200)
    return templates.TemplateResponse(
        request, "sessions_list.html", {"sessions": rows}
    )


@router.get("/sessions/{session_id}", response_class=HTMLResponse)
async def session_detail(
    request: Request,
    session_id: str,
    _: None = _RequireSessionDep,
    manager: SessionManager = _ManagerDep,
    coordinator: HITLCoordinator = _CoordinatorDep,
) -> HTMLResponse:
    try:
        detail = await manager.get(session_id, frames_limit=200)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    pending = coordinator.pending_snapshot(session_id=session_id)
    return templates.TemplateResponse(
        request,
        "session_detail.html",
        {
            "detail": detail,
            "pending_hitl": [
                {"req_id": rid, "tool": v["tool"], "args": v["args"]}
                for rid, v in pending.items()
            ],
        },
    )


@router.post("/sessions/{session_id}/hitl/{req_id}")
async def session_hitl_resolve(
    request: Request,
    session_id: str,
    req_id: str,
    decision: str = Form(...),
    reason: str | None = Form(default=None),
    _: None = _RequireSessionDep,
    coordinator: HITLCoordinator = _CoordinatorDep,
) -> HTMLResponse:
    if decision not in {"accept", "deny"}:
        raise HTTPException(status_code=400, detail="invalid decision")
    full_req_id = req_id if ":" in req_id else f"{session_id}:{req_id}"
    decision_lit = cast(Literal["accept", "deny"], decision)
    try:
        await coordinator.resolve(full_req_id, decision_lit, reason=reason)
    except HITLNotPending as exc:
        raise HTTPException(
            status_code=409, detail="hitl already resolved"
        ) from exc
    return HTMLResponse(
        f"<div class='hitl-resolved'>{decision} (req_id={full_req_id})</div>"
    )
