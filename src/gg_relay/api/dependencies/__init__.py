"""FastAPI dependencies package (Plan 8 D8.22).

The legacy single-file :mod:`gg_relay.api.deps` exports the shared
service handles (manager / store / coordinator). Plan 8 introduces
fine-grained authorisation helpers — kept in a dedicated package so
the file count grows linearly with the dependency surface (role
enforcement, dashboard auth, rate-limit identity, …) instead of
piling everything into ``deps.py``.

Routers should import authz helpers from
``gg_relay.api.dependencies.require_role``; the service-handle
``Depends`` constants stay in :mod:`gg_relay.api.deps` for backward
compatibility.
"""
