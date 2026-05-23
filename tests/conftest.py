"""Root pytest fixtures.

Plan 8 Task 4 (D8.22) — the ``require_role`` dependency enforces
``cfg.role_mapping`` strictly: an empty mapping resolves every label
to ``"viewer"``, so any POST/DELETE/PATCH ``/api/v1/*`` returns 403.
That's the correct production default (operators MUST explicitly
grant ``submitter`` / ``admin`` via ``RELAY_ROLE_MAPPING_RAW``), but
the existing ~800 integration/unit tests pre-date the role surface
and don't seed a mapping. Modifying every ``_make_cfg`` helper would
be invasive churn for zero test-intent value.

The ``_test_role_mapping_default`` fixture below patches
``require_role._resolve_role`` so that *during tests*, an empty
``cfg.role_mapping`` grants ``"admin"`` to any authenticated request
(label present on ``request.state``). Negative-path tests for the
role surface MUST set ``cfg.role_mapping_raw`` explicitly — once
the map is non-empty the patch delegates straight to the original
resolver and the production behaviour kicks in.

Scope: ``autouse=True`` so every test in the tree (unit, integration,
e2e) gets the safety net. The patch is module-level on
``gg_relay.api.dependencies.require_role._resolve_role``, which means
both ``require_role`` and ``require_role_or_own_session`` (they
share the helper) see the test-mode behaviour through the same
patched lookup.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _test_role_mapping_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Grant ``admin`` to authenticated requests when role_mapping is empty.

    See module docstring for motivation. The patch:

      1. Only kicks in when ``cfg.role_mapping`` is empty AND the
         request carries an ``api_key_label`` (i.e. the API-key
         middleware authenticated the caller). Un-authed requests
         still hit the original fallthrough ``viewer``.
      2. Delegates to the un-patched resolver when ``role_mapping``
         is non-empty — so the role-surface tests that explicitly
         seed the map keep getting strict enforcement.
    """
    from gg_relay.api.dependencies import require_role as rr

    original = rr._resolve_role

    def patched(request: object) -> str:
        app = getattr(request, "app", None)
        app_state = getattr(app, "state", None) if app is not None else None
        cfg = (
            getattr(app_state, "config", None)
            if app_state is not None
            else None
        )
        if cfg is not None:
            role_map = getattr(cfg, "role_mapping", {}) or {}
            if not role_map:
                state = getattr(request, "state", None)
                label = (
                    getattr(state, "api_key_label", None)
                    if state is not None
                    else None
                )
                if label is not None:
                    if state is not None:
                        state.role = "admin"
                    return "admin"
        return original(request)

    monkeypatch.setattr(rr, "_resolve_role", patched)
