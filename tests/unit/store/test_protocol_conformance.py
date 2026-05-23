"""Plan 7 Task 5 (D7.4) — Store Protocol 3-way split conformance.

Verifies:

* :class:`SqlAlchemyStore` structurally satisfies all three
  :func:`typing.runtime_checkable` Protocols
  (:class:`SessionStore` / :class:`FrameStore` / :class:`HITLStore`).
* A dummy class missing a Protocol method fails the isinstance check.
* The deprecated :class:`SessionRepository` alias warns on
  **instantiation** but importing :mod:`gg_relay.store` itself does not
  fire any :class:`DeprecationWarning`.
* The alias is behaviourally equivalent to the new concrete name
  (same isinstance lineage; same API surface usable).
"""
from __future__ import annotations

import importlib
import warnings

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from gg_relay.store import (
    FrameStore,
    HITLStore,
    SessionRepository,
    SessionStore,
    SqlAlchemyStore,
    create_all_tables,
    make_async_engine,
)


@pytest_asyncio.fixture
async def engine(tmp_path) -> AsyncEngine:
    eng = make_async_engine(f"sqlite+aiosqlite:///{tmp_path}/conformance.db")
    await create_all_tables(eng)
    yield eng
    await eng.dispose()


# ── runtime_checkable conformance ──────────────────────────────────────


def test_session_store_protocol_runtime_checkable(engine: AsyncEngine) -> None:
    """SqlAlchemyStore satisfies SessionStore at runtime."""
    store = SqlAlchemyStore(engine)
    assert isinstance(store, SessionStore)


def test_frame_store_protocol_runtime_checkable(engine: AsyncEngine) -> None:
    """SqlAlchemyStore satisfies FrameStore at runtime."""
    store = SqlAlchemyStore(engine)
    assert isinstance(store, FrameStore)


def test_hitl_store_protocol_runtime_checkable(engine: AsyncEngine) -> None:
    """SqlAlchemyStore satisfies HITLStore at runtime."""
    store = SqlAlchemyStore(engine)
    assert isinstance(store, HITLStore)


def test_dummy_class_missing_method_isinstance_false() -> None:
    """A class lacking a Protocol method must fail isinstance."""

    class PartialSession:
        async def create_session(self, **_: object) -> None:
            return None

        # Intentionally missing get_session / list_sessions / etc.

    partial = PartialSession()
    assert not isinstance(partial, SessionStore)
    assert not isinstance(partial, FrameStore)
    assert not isinstance(partial, HITLStore)


# ── deprecated alias behaviour ─────────────────────────────────────────


def test_session_repository_alias_instantiation_warns(
    engine: AsyncEngine,
) -> None:
    """Instantiating SessionRepository must emit DeprecationWarning."""
    with pytest.warns(DeprecationWarning, match="renamed to SqlAlchemyStore"):
        SessionRepository(engine)


def test_session_repository_alias_equivalent_behavior(
    engine: AsyncEngine,
) -> None:
    """Alias instance is interchangeable with SqlAlchemyStore.

    Same isinstance lineage (SqlAlchemyStore + all three Protocols) and
    exposes the same public methods.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        alias_instance = SessionRepository(engine)
    direct_instance = SqlAlchemyStore(engine)

    assert isinstance(alias_instance, SqlAlchemyStore)
    assert isinstance(alias_instance, SessionStore)
    assert isinstance(alias_instance, FrameStore)
    assert isinstance(alias_instance, HITLStore)

    public_attrs = {
        name for name in dir(direct_instance) if not name.startswith("_")
    }
    for name in public_attrs:
        assert hasattr(alias_instance, name), (
            f"alias missing public attribute {name!r}"
        )


def test_module_import_does_not_warn() -> None:
    """Importing/reloading gg_relay.store must not emit DeprecationWarning.

    The alias warning is bound to instantiation only — module load
    must stay silent so importing the package incurs no log noise.
    """
    import gg_relay.store as store_pkg
    import gg_relay.store.repository as store_repo

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        importlib.reload(store_repo)
        importlib.reload(store_pkg)
    dep_warnings = [
        w for w in captured if issubclass(w.category, DeprecationWarning)
    ]
    assert not dep_warnings, (
        f"unexpected DeprecationWarning(s) at import: "
        f"{[str(w.message) for w in dep_warnings]}"
    )
