"""Plan 9 D9.13 — Redis stream wire schema (v1).

Pins the byte-for-byte format that :class:`RedisStreamEventBus`
writes to the ``gg-relay:events`` Redis stream and reads back from
XREAD. Single source of truth so:

* changing the wire format requires bumping ``SCHEMA_VERSION`` and
  shipping a migration that reads both old and new — never silent.
* fakeredis tests in D9.1 and the testcontainers cross-worker tests
  in D9.6 share one fixture / one parser.
* future :class:`gg_relay.cluster.kafka_bus.KafkaStreamEventBus`
  (post-Plan 9) can reuse :func:`encode_event` / :func:`decode_event`
  unchanged.

Wire format (one Redis stream entry per :class:`RelayEvent`):

    XADD gg-relay:events * \\
        v 1                          # schema_version
        type SessionCreated          # type(event).__name__
        event_id <uuid>              # str(event.event_id)
        ts <iso8601-utc>             # event.occurred_at.isoformat()
        session_id <uuid|null>       # getattr(event, "session_id", "")
        tier durable                 # event.delivery_tier
        payload <json>               # _event_payload(event)

The stream ID assigned by Redis (e.g. ``1716540000000-0``) is the
cross-worker cursor — :meth:`EventBus.subscribe_all` calls XREAD
with ``$`` (live tail from end) or with a stored ID for replay.

Why every value is a string:

Redis stream entries are ``Mapping[bytes | str, bytes | str]``. The
``redis-py`` client decodes responses to strings when
``decode_responses=True`` is set on the client; the wire layer
operates on the post-decode dict.

Why ``schema_version`` is the first field:

XADD preserves field order. Putting ``v`` first means
:func:`decode_event` can parse the version and dispatch to the
right decoder branch without scanning the whole entry — important
when a v0.9.x consumer pulls a v0.10.x event off the stream during
a (future) rolling upgrade. Today's parser raises
:class:`UnsupportedWireVersionError` for any version it doesn't
know, instead of silently truncating the payload.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID, uuid4

from gg_relay.core.events import RelayEvent
from gg_relay.store.durable_event import ReplayedEvent

SCHEMA_VERSION: Final[int] = 1
"""Bump this when changing the wire format (add a field, rename, etc).

The decoder branches on ``int(entry["v"])``; any value not in the
recognised set raises :class:`UnsupportedWireVersionError` so a
silent partial decode is impossible.

Bump procedure (when v0.10.x ships a wire change):

1. Add the new fields to :func:`encode_event` and increment the
   constant to ``2``.
2. Add a ``decode_v2`` branch to :func:`decode_event`.
3. Add round-trip tests for **both** v1 (read-only, legacy
   consumer) and v2 (read + write).
4. Document the v1→v2 rollover in ``docs/cluster.md`` so operators
   know which order to upgrade workers (consumers first).
"""

STREAM_KEY: Final[str] = "gg-relay:events"
"""Default Redis stream key — operators MAY override via
``RELAY_REDIS_STREAM_KEY`` when running multiple gg-relay clusters
against one Redis (e.g. staging + prod share one ElastiCache)."""


class UnsupportedWireVersionError(ValueError):
    """Raised by :func:`decode_event` for an unknown ``v`` field.

    Subclasses :class:`ValueError` so existing
    ``except ValueError`` catch-alls keep working; subscribers MAY
    catch this specifically to surface a Prometheus counter
    (``gg_relay_redis_wire_version_unsupported_total``)."""


def encode_event(event: RelayEvent) -> dict[str, str]:
    """Serialise ``event`` to the v1 wire format.

    Returns a flat ``{str: str}`` dict that :class:`RedisStreamEventBus`
    passes directly to ``XADD gg-relay:events *``. Every value is
    already a string — no further marshalling needed by the bus.

    The payload column carries dataclass fields not promoted to
    top-level wire fields. The promoted set is intentionally minimal
    (the values an alternate consumer might want to filter on
    without parsing JSON) — session_id and event_id specifically so
    a future consumer can XADD-then-filter without ``json.loads``
    per entry.
    """
    payload: dict[str, Any] = dataclasses.asdict(event)
    for stripped in ("event_id", "occurred_at"):
        payload.pop(stripped, None)
    return {
        "v": str(SCHEMA_VERSION),
        "type": type(event).__name__,
        "event_id": str(event.event_id),
        "ts": event.occurred_at.isoformat(),
        "session_id": getattr(event, "session_id", "") or "",
        "tier": event.delivery_tier,
        "payload": json.dumps(payload, default=str),
    }


def decode_event(entry: dict[str, str]) -> ReplayedEvent:
    """Deserialise a stream entry to a :class:`ReplayedEvent`.

    Returns a :class:`ReplayedEvent` (subclass of :class:`RelayEvent`)
    so SSE generators / IM subscribers that filter on
    ``isinstance(event, RelayEvent)`` see Redis-fed events the same
    as durable replays. The original wire-level class name is
    preserved in ``type_name`` for the SSE event field.

    Raises :class:`UnsupportedWireVersionError` for unrecognised
    versions; :class:`KeyError` for missing required fields (caller
    SHOULD log and skip the entry — never crash the subscriber).
    """
    version_raw = entry.get("v")
    if version_raw is None:
        raise UnsupportedWireVersionError(
            "missing 'v' (schema_version) field on Redis stream entry"
        )
    try:
        version = int(version_raw)
    except (TypeError, ValueError) as exc:
        raise UnsupportedWireVersionError(
            f"non-integer schema_version: {version_raw!r}"
        ) from exc
    if version != SCHEMA_VERSION:
        raise UnsupportedWireVersionError(
            f"unsupported wire schema_version={version}; "
            f"this consumer speaks {SCHEMA_VERSION}"
        )

    ts_raw = entry["ts"]
    try:
        occurred_at = datetime.fromisoformat(ts_raw)
    except ValueError:
        occurred_at = datetime.now(UTC)
    payload_raw = entry.get("payload") or "{}"
    try:
        payload = json.loads(payload_raw)
    except (TypeError, ValueError):
        payload = {}
    tier_raw = entry.get("tier")
    tier: Any = tier_raw if tier_raw in ("lossy", "durable") else "durable"
    event_id_raw = entry.get("event_id", "")
    try:
        event_id = UUID(event_id_raw) if event_id_raw else uuid4()
    except (TypeError, ValueError):
        event_id = uuid4()

    return ReplayedEvent(
        event_id=event_id,
        occurred_at=occurred_at,
        delivery_tier=tier,
        type_name=entry["type"],
        session_id=entry.get("session_id", "") or "",
        payload=payload if isinstance(payload, dict) else {},
        seq=0,
    )


__all__ = [
    "SCHEMA_VERSION",
    "STREAM_KEY",
    "UnsupportedWireVersionError",
    "decode_event",
    "encode_event",
]
