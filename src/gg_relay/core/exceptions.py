"""Core-layer exceptions — Plan 7 Task 8 (D7.5), Task 13 (D7.17), Task 14 (D7.25).

Lives in :mod:`gg_relay.core` (zero external deps) so both the FastAPI
routers and the SessionManager can catch the same class without circular
imports through the store/session boundary.

:class:`HITLAlreadyResolved` is the user-facing companion to
:class:`gg_relay.store.exceptions.ConcurrencyError`: when two callers
race to ``POST /sessions/{sid}/hitl/{req_id}``, exactly one wins and
the other gets a ``409`` response whose body carries the winning
decision (so the loser sees what actually happened instead of just a
generic "already resolved" message).

:class:`DurableEventDropError` is raised by the EventBus when a durable
tier event cannot be persisted (no store configured in strict mode, or
the configured store's :meth:`persist` raised). Callers MUST handle it
— silently dropping a durable event would defeat the entire purpose of
the disk-backed bus (Plan 7 D7.17).

:class:`SDKError` (Plan 7 D7.25 / Task 14) is the base of a small
taxonomy that wraps raw exceptions coming from the Claude SDK so the
API layer can map them to consistent HTTP status codes + an
``error_category`` field instead of leaking SDK class names. The
:func:`classify_sdk_error` helper buckets a raw exception into one of
six categories using the exception's class name + lowercased message;
the buckets are deliberately coarse so the taxonomy stays stable as
the SDK evolves.
"""
from __future__ import annotations

from typing import Any


class RetryConfigError(Exception):
    """Raised by :meth:`SessionManager.retry` when the original session's
    persisted spec cannot produce a fresh submission.

    Plan 8 D8.6 / Task 9. The retry path reconstructs a
    :class:`SessionSpec` from ``sessions.spec_json`` and calls
    :meth:`SessionManager.submit` again with ``parent_session_id`` set.
    If the persisted spec is missing required fields (no ``prompt``,
    no plugins manifest, etc.) we surface this exception instead of
    silently submitting a degenerate session. The batch endpoint maps
    it to a per-id ``error_code='retry_config_error'`` entry so a
    bulk retry can still succeed for the well-formed siblings.
    """


class TemplateConflictError(Exception):
    """Raised by :meth:`SqlAlchemyStore.create_template` when a
    ``(creator, name)`` pair already exists.

    Plan 8 D8.24 / Task 14. The store catches the underlying
    :class:`sqlalchemy.exc.IntegrityError` (triggered by the
    ``uq_prompt_templates_creator_name`` unique constraint) and
    re-raises this typed exception so the API router can map it to
    a clean ``409 template_name_conflict`` response without coupling
    to SQLAlchemy's exception hierarchy.

    The exception lives in :mod:`gg_relay.core` (zero external deps)
    so the router and the repository can both reference it without
    a circular import through the store boundary.
    """


class ApiKeyConflictError(Exception):
    """Raised by :meth:`gg_relay.auth.store.ApiKeyStore.create` when
    the supplied ``label`` collides with an existing row.

    Plan 8 D8.29 / Task 22. The store catches the underlying
    :class:`sqlalchemy.exc.IntegrityError` (triggered by the
    ``ux_api_keys_label`` unique constraint) and re-raises this
    typed exception so the admin POST endpoint can map it to a clean
    409 ``api_key_label_conflict`` response without coupling to
    SQLAlchemy's exception hierarchy. Lives in :mod:`gg_relay.core`
    so the auth + api packages can both reference it without an
    import cycle.
    """


class LastAdminError(Exception):
    """Raised when revoking the supplied admin key would leave zero
    active admin keys.

    Plan 8 D8.29 / Task 22 — defensive guard against the
    ``revoke-everyone-then-lock-yourself-out`` footgun. The admin
    DELETE endpoint counts active admins **before** the revoke
    completes; if the count is ``<= 1`` the revoke is refused with
    HTTP 400 ``last_admin_revoke_forbidden``.
    """


class SelfRevokeError(Exception):
    """Raised when an admin tries to revoke their own currently-active
    key via :func:`gg_relay.api.routers.admin_keys.revoke_api_key`.

    Plan 8 D8.29 / Task 22 — even though the last-admin guard
    technically covers the single-admin case, refusing self-revoke
    explicitly gives operators a clearer error and prevents the
    awkward "create a second admin to revoke yourself" recovery
    path that a sloppy click can trigger.
    """


class HITLAlreadyResolved(Exception):
    """HITL request was already resolved by an earlier decision.

    Carries the first decision (``status`` / ``resolver`` / ``reason``
    / ``resolved_at``) so the API layer can include it in the ``409``
    body. ``first_decision`` is optional because the in-memory race
    path (HITLNotPending → HITLAlreadyResolved) may not have a fresh
    DB row to read; callers should treat ``None`` as "we know the
    request was resolved but can't tell you who won".

    Plan 7 D7.5 / Task 8 — the partner of
    :class:`gg_relay.store.exceptions.ConcurrencyError` for the HITL
    workflow.
    """

    def __init__(
        self,
        req_id: str,
        *,
        first_decision: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"HITL request {req_id} already resolved")
        self.req_id = req_id
        self.first_decision = first_decision


class DurableEventDropError(Exception):
    """Raised when a durable-tier event cannot reach its persistent store.

    Two trigger conditions on :meth:`gg_relay.core.event_bus.EventBus.publish`:

    1. ``durable_store`` is unset AND the bus was constructed with
       ``strict_durable=True`` — publishing a ``delivery_tier="durable"``
       event without a backing store would silently drop audit data, so
       the bus raises instead of fanning out.
    2. The configured store's ``persist`` raised — the bus wraps the
       underlying exception so callers can ``except DurableEventDropError``
       without coupling to SQLAlchemy / Redis exception hierarchies.

    Callers (SessionManager, IM publish, SSE) MUST handle this — they
    may retry, surface to the operator, or trigger graceful degradation,
    but they must NOT swallow it. Plan 7 Task 15 will add a Prometheus
    counter for raised drops so operators can alert on the rate.
    """


class SDKError(Exception):
    """Base for the Claude SDK error taxonomy (Plan 7 D7.25 / Task 14).

    Carries ``category`` (machine-readable label surfaced as
    ``error_category`` in API responses), ``http_status`` (the
    suggested HTTP response code), and ``original`` (the raw
    underlying exception wrapped at the SessionManager boundary, kept
    for ``__cause__`` chaining + diagnostic logging).

    Subclasses override the two class-level attributes; the base
    class itself maps to the "unknown" bucket via
    :class:`SDKUnknownError` so callers catching the base never see
    bare ``SDKError`` instances in production.
    """

    category: str = "unknown"
    http_status: int = 500

    def __init__(
        self,
        msg: str,
        *,
        original: Exception | None = None,
    ) -> None:
        super().__init__(msg)
        self.original = original


class SDKConnectError(SDKError):
    """Lost / refused connection to the SDK transport.

    Transient — the operator should retry. Maps to 503 so an HTTP
    client treats it the same as a temporary backend outage.
    """

    category = "connect"
    http_status = 503


class SDKQueryError(SDKError):
    """Malformed or rejected query payload.

    The SDK rejected the call before any work happened (bad prompt,
    invalid options, etc.). Maps to 400 because retrying without a
    fix will keep failing.
    """

    category = "query"
    http_status = 400


class SDKPermissionError(SDKError):
    """The SDK rejected the request for credential / permission reasons.

    Covers 401 / 403 surface from the upstream API. Maps to 403 so
    the dashboard renders an actionable "check your API key" hint
    rather than a generic 500.
    """

    category = "permission"
    http_status = 403


class SDKTransportError(SDKError):
    """Protocol-level failure between gg-relay and the SDK.

    Examples: handshake mismatch, framing corruption, unexpected EOF
    on the SDK stream. Maps to 502 because the relay reached the SDK
    but couldn't complete a clean exchange.
    """

    category = "transport"
    http_status = 502


class SDKTimeoutError(SDKError):
    """The SDK didn't respond within the configured timeout.

    Maps to 504. Distinct from :class:`SDKConnectError` (which
    surfaces failure to *reach* the SDK) — a timeout means the SDK
    is reachable but slow.
    """

    category = "timeout"
    http_status = 504


class SDKUnknownError(SDKError):
    """Fallback bucket when no other category matches.

    Maps to 500 so unclassified failures are visibly server-side.
    Operators should monitor the rate of ``error_category=unknown``
    responses and feed any recurring patterns back into
    :func:`classify_sdk_error`.
    """

    category = "unknown"
    http_status = 500


def classify_sdk_error(exc: Exception) -> SDKError:
    """Map a raw SDK exception to a typed :class:`SDKError` subclass.

    Plan 7 D7.25 / Task 14. The classification is deliberately
    string-based (class name + lowercased message) so it stays
    robust as the SDK reshuffles its exception hierarchy. The order
    of checks matters — ``timeout`` is checked before ``connect``
    because some SDK timeout classes derive from connect errors.

    Already-typed :class:`SDKError` instances pass through unchanged
    so re-classifying the result of an earlier ``classify_sdk_error``
    call is a no-op (idempotent).
    """
    if isinstance(exc, SDKError):
        return exc
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "timeout" in name or "timeout" in msg or "timed out" in msg:
        return SDKTimeoutError(str(exc), original=exc)
    if (
        "permission" in name
        or "forbidden" in msg
        or "unauthorized" in msg
        or "401" in msg
        or "403" in msg
    ):
        return SDKPermissionError(str(exc), original=exc)
    if (
        "connect" in name
        or "connection" in msg
        or "refused" in msg
        or "unreachable" in msg
    ):
        return SDKConnectError(str(exc), original=exc)
    if "transport" in name or "protocol" in msg or "handshake" in msg:
        return SDKTransportError(str(exc), original=exc)
    if (
        "query" in name
        or "invalid" in msg
        or "malformed" in msg
        or "bad request" in msg
    ):
        return SDKQueryError(str(exc), original=exc)
    return SDKUnknownError(str(exc), original=exc)


__all__ = [
    "ApiKeyConflictError",
    "DurableEventDropError",
    "HITLAlreadyResolved",
    "LastAdminError",
    "RetryConfigError",
    "SDKConnectError",
    "SDKError",
    "SDKPermissionError",
    "SDKQueryError",
    "SDKTimeoutError",
    "SDKTransportError",
    "SDKUnknownError",
    "SelfRevokeError",
    "TemplateConflictError",
    "classify_sdk_error",
]
