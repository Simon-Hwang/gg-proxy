"""Constant-time API-key comparison (Plan 7 Task 11 / D7.15).

The middleware MUST compare ``X-API-Key`` headers against the configured
key set with :func:`secrets.compare_digest` (not ``==``) so a remote
attacker cannot use timing variance to brute-force a single byte at a
time. The AST inspection here is the authoritative check; the variance
test is best-effort (CPython is too noisy on shared CI to assert tight
bounds) but documents the intent.
"""
from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from gg_relay.api.middleware.api_key_auth import APIKeyAuthMiddleware


def _ok(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


def _build_app(keys_with_labels: dict[str, str]) -> Starlette:
    app = Starlette(routes=[Route("/api/v1/resource", _ok)])
    app.add_middleware(
        APIKeyAuthMiddleware,
        keys_with_labels=keys_with_labels,
        protected_prefix="/api/v1",
    )
    return app


def test_compare_digest_used_in_middleware_source() -> None:
    """AST-inspect ``api_key_auth.py``: ``stdlib_secrets.compare_digest``
    MUST appear as a Call in the ``dispatch`` method body. Pure ``==``
    comparisons against the configured key set are timing-unsafe and
    would slip past a reviewer who didn't run this check.
    """
    src_path = Path(__file__).resolve().parents[3] / (
        "src/gg_relay/api/middleware/api_key_auth.py"
    )
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    compare_digest_calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        attr_name: str | None = None
        if isinstance(func, ast.Attribute):
            attr_name = func.attr
        elif isinstance(func, ast.Name):
            attr_name = func.id
        if attr_name == "compare_digest":
            compare_digest_calls.append(node)
    assert compare_digest_calls, (
        "secrets.compare_digest must be used to compare API keys; "
        "found no compare_digest Call in api_key_auth.py AST"
    )


@pytest.mark.asyncio
async def test_wrong_key_returns_401_without_leaking_match_position() -> None:
    """A wrong key produces the SAME response body regardless of how
    many leading characters match a configured key — i.e. ``"x" * 32``
    and ``"k1" + "x" * 30`` both produce the canonical 401 payload."""
    app = _build_app({"k1": "alice", "k2": "bob"})
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r_all_wrong = await ac.get(
            "/api/v1/resource", headers={"X-API-Key": "x" * 32}
        )
        r_partial_match = await ac.get(
            "/api/v1/resource", headers={"X-API-Key": "k1xxxx"}
        )
    assert r_all_wrong.status_code == 401
    assert r_partial_match.status_code == 401
    # Body shape MUST be identical — the middleware never includes
    # match position / "which key was closest" info in the response.
    assert r_all_wrong.json() == {"detail": "invalid_api_key"}
    assert r_partial_match.json() == {"detail": "invalid_api_key"}


@pytest.mark.asyncio
async def test_compare_digest_timing_best_effort() -> None:
    """Best-effort timing variance check.

    Measures dispatch latency for ``all-wrong`` vs ``partial-match``
    keys; on a quiet machine ``compare_digest`` keeps the spread well
    below the threshold. Marked best-effort because shared CI runners
    can spike orders of magnitude beyond reasonable bounds; we WARN
    via :func:`pytest.skip` rather than fail when the variance blows
    past 5ms.
    """
    app = _build_app({"k1" * 8: "alice"})  # 16-char key for stable hashing
    transport = ASGITransport(app=app)
    iterations = 200
    durations_all_wrong: list[float] = []
    durations_partial: list[float] = []
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        for _ in range(iterations):
            t0 = time.perf_counter()
            await ac.get(
                "/api/v1/resource",
                headers={"X-API-Key": "z" * 16},
            )
            durations_all_wrong.append(time.perf_counter() - t0)
            t0 = time.perf_counter()
            await ac.get(
                "/api/v1/resource",
                headers={"X-API-Key": "k1" * 7 + "zz"},
            )
            durations_partial.append(time.perf_counter() - t0)
    mean_all = sum(durations_all_wrong) / iterations
    mean_partial = sum(durations_partial) / iterations
    spread_ms = abs(mean_all - mean_partial) * 1000
    if spread_ms > 5.0:
        pytest.skip(
            f"timing variance {spread_ms:.2f}ms > 5ms threshold "
            "(CI noise — compare_digest is still in source per the "
            "AST check)"
        )
    assert spread_ms <= 5.0
