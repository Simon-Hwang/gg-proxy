"""DockerExecutor — per-session container, NDJSON over unix socket.

Implements :class:`ExecutorBackend` Protocol against ``aiodocker`` so each
session gets its own ephemeral container running the gg-relay wire runner
(``python -m gg_relay.session.runner.wire_runner``).

Plan 3 D3.1-D3.19 decisions baked in:
- Image default: ``ghcr.io/gg-org/gg-relay-runner:latest`` (D3.4)
- Socket path: ``/var/run/gg-relay/{runtime_id}.sock`` (D3.5)
- Resources: 2g mem / 2 cpus / 512 pids by default (D3.6)
- Network: ``bridge`` + ``host.docker.internal:host-gateway`` for the
  bundled minimal proxy to be reachable (D3.7 + spike addendum)
- Bind mounts use ``rw,z`` for SELinux compatibility (D3.5 / new decision)
- Graceful stop: shutdown frame + 5 s wait + kill (D3.12)
- ``SessionRuntimeContext.credentials`` mapped into env, never persisted (D3.16)
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from gg_relay.session.frames import make_shutdown
from gg_relay.session.spec import RuntimeHandle, SessionRuntimeContext, SessionSpec
from gg_relay.session.transport.protocol import TransportClosed
from gg_relay.session.transport.unixsocket import UnixSocketServer

logger = logging.getLogger("gg_relay.executor.docker")


# Default sentinel runtime context — module-level so the function default is a
# single shared instance (avoids ruff B008).
_DEFAULT_RUNTIME_CTX = SessionRuntimeContext()


class _ContainerLike(Protocol):
    """Subset of aiodocker.DockerContainer we actually use; lets us mock it
    cleanly in tests without depending on the real class hierarchy."""

    id: str
    async def show(self) -> dict[str, Any]: ...
    async def wait(self) -> dict[str, Any]: ...
    async def kill(self) -> None: ...


class _ContainersLike(Protocol):
    async def run(
        self, *, config: dict[str, Any], name: str
    ) -> _ContainerLike: ...


class _DockerLike(Protocol):
    """aiodocker.Docker surface we depend on."""

    containers: _ContainersLike
    async def close(self) -> None: ...


_SIZE_UNITS: Mapping[str, int] = {"k": 1024, "m": 1024**2, "g": 1024**3}


def _parse_size(s: str) -> int:
    """Convert ``2g`` / ``512m`` / ``1024k`` to bytes."""
    if not s:
        raise ValueError("empty size string")
    unit = s[-1].lower()
    if unit in _SIZE_UNITS:
        return int(s[:-1]) * _SIZE_UNITS[unit]
    return int(s)  # raw bytes


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class DockerExecutor:
    """Per-session container executor.

    Construction does not touch Docker; ``start()`` is where the first daemon
    call happens. Callers MUST eventually invoke :meth:`close` (or use ``async
    with``) to release the underlying ``aiodocker.Docker`` HTTP session.
    """

    DEFAULT_IMAGE = "ghcr.io/gg-org/gg-relay-runner:latest"
    DEFAULT_SOCKET_ROOT = Path("/var/run/gg-relay")

    def __init__(
        self,
        *,
        image: str = DEFAULT_IMAGE,
        socket_root: Path = DEFAULT_SOCKET_ROOT,
        proxy_url: str | None = None,
        docker_client: _DockerLike | None = None,
        default_mem: str = "2g",
        default_cpus: float = 2.0,
        default_pids_limit: int = 512,
        accept_timeout: float = 30.0,
        shutdown_grace_s: float = 5.0,
        extra_hosts: Mapping[str, str] | None = None,
    ) -> None:
        self._image = image
        self._socket_root = socket_root
        self._proxy_url = proxy_url
        self._docker = docker_client
        self._owns_docker = docker_client is None
        self._mem = default_mem
        self._cpus = default_cpus
        self._pids_limit = default_pids_limit
        self._accept_timeout = accept_timeout
        self._shutdown_grace_s = shutdown_grace_s
        # spike addendum: claude CLI inside the container reaches the host's
        # bundled minimal proxy via host.docker.internal, which on Linux
        # without Docker Desktop only resolves when --add-host is passed.
        self._extra_hosts: dict[str, str] = dict(
            extra_hosts or {"host.docker.internal": "host-gateway"}
        )
        self._containers: dict[str, _ContainerLike] = {}
        self._servers: dict[str, UnixSocketServer] = {}

    async def _client(self) -> _DockerLike:
        """Lazily instantiate aiodocker.Docker() on first use."""
        if self._docker is None:
            from aiodocker import Docker  # local import: keeps tests light

            self._docker = cast(_DockerLike, Docker())
        return self._docker

    async def close(self) -> None:
        """Release the underlying aiodocker session (if we own it)."""
        if self._owns_docker and self._docker is not None:
            with contextlib.suppress(Exception):
                await self._docker.close()
            self._docker = None

    async def start(
        self,
        spec: SessionSpec,
        *,
        runtime_ctx: SessionRuntimeContext = _DEFAULT_RUNTIME_CTX,
    ) -> RuntimeHandle:
        """Spin up the container and return a RuntimeHandle holding the
        host-side transport.

        Side effects, in order:
          1. mkdir + bind a host-side AF_UNIX socket at
             ``{socket_root}/{runtime_id}.sock``
          2. ``docker run`` the runner image with the socket mounted in
          3. ``server.accept()`` to receive the runner's outbound connection
        """
        runtime_id = uuid.uuid4().hex
        # Match Plan 3's name prefix recommendation so container collisions
        # on a shared host (38 already running per env audit) are unlikely.
        container_name = f"gg-relay-{runtime_id[:8]}"
        socket_path = self._socket_root / f"{runtime_id}.sock"

        server = await UnixSocketServer.listen(socket_path)
        self._servers[runtime_id] = server

        env = self._build_env(spec, runtime_ctx, runtime_id)
        cfg = self._build_container_config(spec, env)

        client = await self._client()
        try:
            container = await client.containers.run(config=cfg, name=container_name)
        except Exception:
            # Container failed to start — release the socket so the next
            # session can rebind. Do NOT swallow the exception; the caller
            # needs to know start() didn't succeed.
            await server.close()
            self._servers.pop(runtime_id, None)
            raise
        self._containers[runtime_id] = container

        try:
            transport = await server.accept(timeout=self._accept_timeout)
        except TimeoutError:
            logger.error(
                "DockerExecutor: container %s did not connect within %.1fs",
                container_name,
                self._accept_timeout,
            )
            with contextlib.suppress(Exception):
                await container.kill()
            await server.close()
            self._containers.pop(runtime_id, None)
            self._servers.pop(runtime_id, None)
            raise

        return RuntimeHandle(
            backend="docker",
            runtime_id=runtime_id,
            transport=transport,
            started_at=datetime.now(UTC),
            extra=(
                ("container_id", container.id),
                ("container_name", container_name),
                ("image", self._image),
                ("socket_path", str(socket_path)),
            ),
        )

    async def stop(self, handle: RuntimeHandle) -> None:
        """Graceful stop: shutdown frame → wait → kill → close transport."""
        runtime_id = handle.runtime_id
        container = self._containers.pop(runtime_id, None)
        server = self._servers.pop(runtime_id, None)

        if container is not None:
            with contextlib.suppress(TransportClosed, Exception):
                # ``-1`` keeps the wire seq monotonic-without-gaps invariant
                # since the bridge owns the host seq counter.
                await asyncio.wait_for(
                    handle.transport.send(make_shutdown(-1)), timeout=1.0
                )
            try:
                await asyncio.wait_for(
                    container.wait(), timeout=self._shutdown_grace_s
                )
            except (TimeoutError, Exception):
                with contextlib.suppress(Exception):
                    await container.kill()

        with contextlib.suppress(Exception):
            await handle.transport.close()
        if server is not None:
            await server.close()

    async def health(self, handle: RuntimeHandle) -> bool:
        """``True`` iff the container reports running and the transport is alive."""
        container = self._containers.get(handle.runtime_id)
        if container is None:
            return False
        try:
            info = await container.show()
        except Exception:
            return False
        status = (info.get("State") or {}).get("Status")
        return status == "running" and handle.transport.is_alive

    # ── helpers ─────────────────────────────────────────────────────────

    def _build_env(
        self,
        spec: SessionSpec,
        runtime_ctx: SessionRuntimeContext,
        runtime_id: str,
    ) -> dict[str, str]:
        """Compose the container env dict.

        Order of precedence (later wins):
          1. baseline (GG_RELAY_*, RELAY_*)
          2. proxy vars when configured
          3. SessionRuntimeContext.credentials (ANTHROPIC_API_KEY etc.)
          4. SessionSpec.plugins.extra_env (caller override)
        """
        env: dict[str, str] = {
            "GG_RELAY_SPEC_JSON": spec.to_json(),
            "GG_RELAY_SOCKET": f"/var/run/gg-relay/{runtime_id}.sock",
            "RELAY_TRACE_ID": runtime_ctx.trace_id,
            "RELAY_SESSION_ID": runtime_id,
        }
        if self._proxy_url:
            env["HTTPS_PROXY"] = self._proxy_url
            env["HTTP_PROXY"] = self._proxy_url
            env["NO_PROXY"] = "localhost,127.0.0.1"
        for key, value in runtime_ctx.credentials.items():
            env[key] = value
        for key, value in spec.plugins.extra_env:
            env[key] = value
        return env

    def _build_container_config(
        self, spec: SessionSpec, env: dict[str, str]
    ) -> dict[str, Any]:
        binds = [
            f"{self._socket_root}:/var/run/gg-relay:rw,z",
            f"{spec.cwd}:/workspace:rw,z",
        ]
        return {
            "Image": self._image,
            "Env": [f"{k}={v}" for k, v in env.items()],
            "HostConfig": {
                "NetworkMode": "bridge",
                "AutoRemove": True,
                "Memory": _parse_size(self._mem),
                "NanoCpus": int(self._cpus * 1e9),
                "PidsLimit": self._pids_limit,
                "Binds": binds,
                "ExtraHosts": [f"{h}:{i}" for h, i in self._extra_hosts.items()],
            },
        }
