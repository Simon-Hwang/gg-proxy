"""gg_relay.api — FastAPI application surface.

Importing this package is intentionally cheap: ``create_app`` and
``lifespan`` are re-exported via :mod:`gg_relay.api.main` and are
available as ``from gg_relay.api.main import create_app``. We do NOT
eagerly import ``main`` here because doing so would create a circular
import path through routers that themselves use ``api.deps``.
"""
