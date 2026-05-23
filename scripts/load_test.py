"""Locust load test scaffold for gg-relay (Plan 7 Task 4 / D7.10).

Three user profiles selectable via ``--tags`` so a single file can drive
three distinct ``make`` targets:

    * ``rest``      — REST submit + poll (typical SDK-client traffic)
    * ``dashboard`` — HTMX Kanban polling (operator UI traffic)
    * ``sse``       — Best-effort SSE stream pressure (no native Locust
      SSE stats — see :class:`SSEUser` docstring)

Usage::

    make load-rest         # 100 users, 10/s spawn, 5 minutes
    make load-dashboard    # 50 users, 5/s spawn, 5 minutes
    make load-sse          # 10 users, 1/s spawn, 5 minutes

Environment variables (all optional):

    * ``RELAY_API_KEY``             — REST ``X-API-Key`` (default ``test-key``)
    * ``RELAY_DASHBOARD_USER``      — Dashboard login user (default ``admin``)
    * ``RELAY_DASHBOARD_PASSWORD``  — Dashboard login password (default ``admin``)
    * ``RELAY_LOADTEST_EXECUTOR``   — Session executor for fixture/REST submits
      (``inprocess`` default — keeps load on the API surface, not Docker)

The fixture session created on ``test_start`` is shared by ``rest`` polling
and ``sse`` streaming, so the swarm focuses on the hot read/stream paths
instead of session-create overhead.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

import httpx
from locust import HttpUser, between, events, tag, task

API_KEY = os.environ.get("RELAY_API_KEY", "test-key")
DASHBOARD_USER = os.environ.get("RELAY_DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD = os.environ.get("RELAY_DASHBOARD_PASSWORD", "admin")
EXECUTOR = os.environ.get("RELAY_LOADTEST_EXECUTOR", "inprocess")

_FIXTURE_SESSION_ID: str | None = None


def _api_headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY, "Content-Type": "application/json"}


def _build_session_payload(label: str) -> dict[str, Any]:
    return {
        "spec": {
            "prompt": f"load-test fixture ({label})",
            "cwd": "/tmp",
            "plugins": {"profile": "minimal"},
            "executor": EXECUTOR,
            "timeout_s": 60,
            "tags": ["loadtest", label],
        },
        "credentials": {},
    }


@events.test_start.add_listener
def _create_fixture_session(environment: Any, **_: Any) -> None:
    """Create one shared session before the swarm starts.

    Reused by ``rest`` polling and ``sse`` streaming so that the load
    focuses on the read/stream paths rather than session-create
    overhead. Failures are logged but non-fatal — the SSE profile will
    simply no-op, and the REST profile will continue to create its own
    sessions per task.
    """
    global _FIXTURE_SESSION_ID
    host = getattr(environment, "host", None) or "http://localhost:8080"
    label = f"fixture-{uuid.uuid4().hex[:8]}"
    try:
        resp = httpx.post(
            f"{host}/api/v1/sessions",
            json=_build_session_payload(label),
            headers=_api_headers(),
            timeout=10.0,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[loadtest] fixture session create errored: {exc!r}")
        return
    if resp.status_code in (200, 202):
        try:
            _FIXTURE_SESSION_ID = resp.json()["id"]
        except Exception as exc:  # noqa: BLE001
            print(f"[loadtest] fixture session response malformed: {exc!r}")
            return
        print(f"[loadtest] fixture session_id = {_FIXTURE_SESSION_ID}")
    else:
        print(
            f"[loadtest] fixture session create failed: "
            f"{resp.status_code} {resp.text[:200]}"
        )


class RESTUser(HttpUser):
    """Typical SDK-client traffic: submit a fresh session, then poll its detail."""

    wait_time = between(1, 3)

    @tag("rest")
    @task
    def submit_and_poll(self) -> None:
        with self.client.post(
            "/api/v1/sessions",
            json=_build_session_payload("rest-user"),
            headers=_api_headers(),
            name="POST /api/v1/sessions",
            catch_response=True,
        ) as r:
            if r.status_code not in (200, 202):
                r.failure(f"submit failed: {r.status_code}")
                return
            try:
                sid = r.json()["id"]
            except Exception as exc:  # noqa: BLE001
                r.failure(f"submit response malformed: {exc!r}")
                return
        self.client.get(
            f"/api/v1/sessions/{sid}",
            headers=_api_headers(),
            name="GET /api/v1/sessions/{sid}",
        )


class DashboardUser(HttpUser):
    """Operator UI traffic: Kanban board polling (HTMX 5s refresh)."""

    wait_time = between(3, 7)

    def on_start(self) -> None:
        # Log in once per user so subsequent ``/dashboard/*`` requests
        # carry the signed session cookie expected by ``_require_session``.
        self.client.post(
            "/dashboard/login",
            data={"username": DASHBOARD_USER, "password": DASHBOARD_PASSWORD},
            allow_redirects=False,
            name="POST /dashboard/login",
        )

    @tag("dashboard")
    @task
    def kanban(self) -> None:
        self.client.get("/dashboard/kanban", name="GET /dashboard/kanban")


class SSEUser(HttpUser):
    """Best-effort SSE pressure: open one stream, hold 5s, then close.

    NOTE: Locust has no native SSE statistics. We open the stream, sleep,
    then mark the response a success if the status looks healthy. Use the
    ``users`` / ``RPS`` / ``response_time`` columns to monitor pressure
    on the SSE endpoint, NOT to measure end-to-end SSE delivery latency.
    """

    wait_time = between(1, 2)

    @tag("sse")
    @task
    def stream_5s(self) -> None:
        sid = _FIXTURE_SESSION_ID
        if sid is None:
            return
        with self.client.get(
            f"/api/v1/sessions/{sid}/events",
            headers={"X-API-Key": API_KEY, "Accept": "text/event-stream"},
            stream=True,
            catch_response=True,
            name="GET /api/v1/sessions/{sid}/events [SSE, 5s hold]",
        ) as r:
            if r.status_code != 200:
                r.failure(f"sse failed: {r.status_code}")
                return
            time.sleep(5)
            r.success()
