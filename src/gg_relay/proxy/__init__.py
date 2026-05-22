"""Minimal forward HTTP/HTTPS proxy (Plan 3 §6 Task 12).

claude CLI in the docker runner is configured with
``HTTPS_PROXY=http://host.docker.internal:8888`` so its outbound traffic
flows through this proxy. The proxy enforces a strict allow-list of
upstream hosts (default: ``api.anthropic.com``) and records every
allow/deny decision into an audit log keyed by ``X-Relay-Session-Id``.

Implementation note: ``aiohttp.web`` does not natively support HTTP
``CONNECT`` (used for HTTPS tunnelling) so we use raw
``asyncio.start_server`` with a custom protocol handler. See Plan 3
§6 Task 12 — the original plan skeleton mentioned aiohttp but the
implementation swapped it out for direct asyncio.
"""
from gg_relay.proxy.audit import AuditLog
from gg_relay.proxy.server import (
    ALLOWED_HOSTS_DEFAULT,
    DEFAULT_PROXY_PORT,
    MinimalProxy,
)

__all__ = [
    "ALLOWED_HOSTS_DEFAULT",
    "DEFAULT_PROXY_PORT",
    "AuditLog",
    "MinimalProxy",
]
