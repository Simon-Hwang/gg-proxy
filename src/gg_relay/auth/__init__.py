"""Plan 8 Task 22 / D8.29 — DB-backed API key auth.

Public surface:

  * :class:`KeyResolver` — Protocol every resolver must satisfy.
  * :class:`ResolvedKey` — frozen dataclass returned by ``resolve``.
  * :class:`ApiKeyStore` — CRUD layer over the ``api_keys`` table.
  * :class:`EnvKeyResolver` — bootstrap-only env → DB sync helper.
  * :class:`DBKeyResolver`  — TTL-cached production resolver.
  * :func:`hash_key`        — sha256 digest used by store + resolver.
"""
from gg_relay.auth.db_resolver import DBKeyResolver
from gg_relay.auth.env_resolver import EnvKeyResolver
from gg_relay.auth.protocol import KeyResolver, ResolvedKey
from gg_relay.auth.store import ApiKeyStore, hash_key

__all__ = [
    "ApiKeyStore",
    "DBKeyResolver",
    "EnvKeyResolver",
    "KeyResolver",
    "ResolvedKey",
    "hash_key",
]
