"""K8sJobExecutor — per-session Kubernetes ``Job`` runner (Plan 9 D9.8).

This is the v0.9.0 **opt-in, P1** alternative to
:class:`gg_relay.session.executor.docker.DockerExecutor`. It only
activates when ``Config.executor_kind == "k8s_job"``; the default
in-process / docker paths are untouched.

Lifecycle for ONE session:

1. ``submit_token``: generate a 32-byte URL-safe secret. This is the
   one-shot ``RELAY_RUNNER_AUTH_TOKEN`` the runner pod will validate
   on the TCP handshake (see ``session/transport/tcp.py``).
2. ``client.create_secret(name, {RELAY_RUNNER_AUTH_TOKEN: token})`` —
   never inject the token via env literal; the per-session Secret is
   the only place it lives at rest.
3. ``client.create_job(name, spec)`` with ``ownerReferences`` pointing
   at the Secret so K8s GC removes the Job (and downstream pod logs)
   when the Secret is deleted.
4. ``client.wait_for_pod_ip(job_name)`` — poll the Job until the
   spawned Pod has an assigned IP. Polling is bounded; if K8s never
   produces a Pod (image pull error, quota), we fail fast.
5. ``TcpTransport.connect(pod_ip, runner_port, auth_token=token)`` —
   the host connects to the runner, performs the auth handshake, and
   returns a working :class:`SessionTransport`.
6. On ``stop()``: send a shutdown frame, wait grace, then
   ``client.delete_secret(name)`` — the owner-reference GC cascades to
   the Job + Pod automatically. ``ttlSecondsAfterFinished`` on the
   Job keeps zombie objects bounded if the operator misses a stop().

The K8s client surface is a small :class:`K8sClient` Protocol so unit
tests can inject a fake without depending on ``kubernetes-asyncio``.
Production wiring constructs a real client behind the ``[k8s]`` extra.

ETCD back-pressure: ``Config.k8s_max_concurrent_jobs`` is enforced
INSIDE :meth:`start` (not at the API edge) so a hung Pod doesn't
silently leak a quota slot — every successful submit increments the
``K8S_JOB_QUEUE_DEPTH`` gauge and every ``stop()`` decrements it.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from gg_relay.session.frames import make_shutdown
from gg_relay.session.spec import RuntimeHandle, SessionRuntimeContext, SessionSpec
from gg_relay.session.transport.protocol import TransportClosed
from gg_relay.session.transport.tcp import AuthFailed, TcpTransport

logger = logging.getLogger("gg_relay.executor.k8s_job")

_DEFAULT_RUNTIME_CTX = SessionRuntimeContext()


class K8sJobQueueFull(Exception):
    """Raised when an in-flight submit would exceed
    ``Config.k8s_max_concurrent_jobs``. The API surface maps this to
    HTTP 503 ``etcd_pressure`` so clients back off cleanly."""


class K8sJobSubmitError(Exception):
    """Wraps any unrecoverable K8s API error during submit."""


@dataclass(frozen=True, slots=True)
class K8sJobHandle:
    """Per-session bookkeeping kept on the :class:`RuntimeHandle`.

    The Secret name is the GC anchor: deleting it cascades to the Job
    + Pod via ``ownerReferences``.
    """

    job_name: str
    secret_name: str
    pod_ip: str
    transport: TcpTransport


class K8sClient(Protocol):
    """Narrow K8s surface the executor depends on.

    Concrete production implementation lives behind the ``[k8s]``
    extra (``gg_relay.session.executor.k8s_client.KubernetesAsyncIOClient``).
    Tests inject a fake satisfying just these four methods.
    """

    async def create_secret(
        self, *, namespace: str, name: str, data: Mapping[str, str]
    ) -> None: ...

    async def create_job(
        self,
        *,
        namespace: str,
        name: str,
        secret_name: str,
        image: str,
        env: Mapping[str, str],
        runner_port: int,
        ttl_seconds_after_finished: int,
    ) -> None: ...

    async def wait_for_pod_ip(
        self, *, namespace: str, job_name: str, timeout_s: float
    ) -> str: ...

    async def delete_secret(self, *, namespace: str, name: str) -> None: ...


class K8sJobExecutor:
    """Per-session K8s ``Job`` executor.

    Construction does NOT touch the API server; the first call to
    :meth:`start` is where everything happens. Callers MUST always
    invoke :meth:`stop` (the SessionManager teardown chain handles
    this) so the per-session Secret + cascaded Job are GC'd
    deterministically rather than waiting for the TTL.
    """

    DEFAULT_RUNNER_IMAGE = "ghcr.io/gg-org/gg-relay-runner:latest"
    DEFAULT_RUNNER_PORT = 9001

    def __init__(
        self,
        *,
        client: K8sClient,
        namespace: str = "gg",
        runner_image: str = DEFAULT_RUNNER_IMAGE,
        runner_port: int = DEFAULT_RUNNER_PORT,
        max_concurrent_jobs: int = 50,
        ttl_seconds_after_finished: int = 600,
        pod_ip_timeout_s: float = 60.0,
        shutdown_grace_s: float = 5.0,
    ) -> None:
        self._client = client
        self._namespace = namespace
        self._runner_image = runner_image
        self._runner_port = runner_port
        self._max_concurrent_jobs = max_concurrent_jobs
        self._ttl = ttl_seconds_after_finished
        self._pod_ip_timeout_s = pod_ip_timeout_s
        self._shutdown_grace_s = shutdown_grace_s
        self._inflight: dict[str, K8sJobHandle] = {}

    @property
    def inflight_count(self) -> int:
        return len(self._inflight)

    async def close(self) -> None:
        """No-op for now — kept for ExecutorBackend parity with
        :class:`DockerExecutor.close`. The real ``KubernetesAsyncIOClient``
        owns the aiohttp session and is closed by the lifespan, not
        the executor."""

    async def start(
        self,
        spec: SessionSpec,
        *,
        runtime_ctx: SessionRuntimeContext = _DEFAULT_RUNTIME_CTX,
    ) -> RuntimeHandle:
        """Submit a per-session K8s Job + Secret, then open a TCP
        transport to the runner Pod.

        Raises :class:`K8sJobQueueFull` if the submit would exceed
        ``max_concurrent_jobs`` — admission control happens BEFORE we
        touch the API server so a quota burst can't briefly leak
        Secrets.
        """
        if self.inflight_count >= self._max_concurrent_jobs:
            _bump_failure("queue_full")
            raise K8sJobQueueFull(
                f"k8s_max_concurrent_jobs={self._max_concurrent_jobs} reached"
            )

        runtime_id = uuid.uuid4().hex[:12]
        secret_name = f"gg-runner-token-{runtime_id}"
        job_name = f"gg-runner-{runtime_id}"
        auth_token = secrets.token_urlsafe(32)

        try:
            await self._client.create_secret(
                namespace=self._namespace,
                name=secret_name,
                data={"RELAY_RUNNER_AUTH_TOKEN": auth_token},
            )
        except Exception as e:
            _bump_failure("secret_create")
            raise K8sJobSubmitError(f"create_secret failed: {e}") from e

        env = self._build_env(spec, runtime_ctx)
        try:
            await self._client.create_job(
                namespace=self._namespace,
                name=job_name,
                secret_name=secret_name,
                image=self._runner_image,
                env=env,
                runner_port=self._runner_port,
                ttl_seconds_after_finished=self._ttl,
            )
        except Exception as e:
            _bump_failure("job_create")
            with contextlib.suppress(Exception):
                await self._client.delete_secret(
                    namespace=self._namespace, name=secret_name
                )
            raise K8sJobSubmitError(f"create_job failed: {e}") from e

        try:
            pod_ip = await self._client.wait_for_pod_ip(
                namespace=self._namespace,
                job_name=job_name,
                timeout_s=self._pod_ip_timeout_s,
            )
        except Exception as e:
            _bump_failure("pod_ip_timeout")
            with contextlib.suppress(Exception):
                await self._client.delete_secret(
                    namespace=self._namespace, name=secret_name
                )
            raise K8sJobSubmitError(f"wait_for_pod_ip failed: {e}") from e

        try:
            transport = await TcpTransport.connect(
                pod_ip,
                self._runner_port,
                auth_token=auth_token,
                retry_timeout=15.0,
            )
        except (AuthFailed, ConnectionError) as e:
            _bump_failure("transport_connect")
            with contextlib.suppress(Exception):
                await self._client.delete_secret(
                    namespace=self._namespace, name=secret_name
                )
            raise K8sJobSubmitError(f"tcp connect failed: {e}") from e

        handle = K8sJobHandle(
            job_name=job_name,
            secret_name=secret_name,
            pod_ip=pod_ip,
            transport=transport,
        )
        self._inflight[secret_name] = handle
        _bump_queue_depth(len(self._inflight))
        return RuntimeHandle(
            backend="k8s_job",
            runtime_id=runtime_id,
            transport=transport,
            started_at=datetime.now(UTC),
            extra=(
                ("job_name", job_name),
                ("secret_name", secret_name),
                ("pod_ip", pod_ip),
                ("namespace", self._namespace),
            ),
        )

    async def stop(self, handle: RuntimeHandle) -> None:
        """Send shutdown, wait grace, then delete the per-session
        Secret (which cascades to the Job + Pod via owner refs)."""
        meta = dict(handle.extra)
        secret_name = meta.get("secret_name")
        if not secret_name:
            return
        assert isinstance(secret_name, str)
        record = self._inflight.pop(secret_name, None)
        _bump_queue_depth(len(self._inflight))
        if record is not None:
            with contextlib.suppress(TransportClosed, Exception):
                await record.transport.send(make_shutdown(-1))
            try:
                await asyncio.wait_for(
                    self._wait_for_close(record.transport),
                    timeout=self._shutdown_grace_s,
                )
            except TimeoutError:
                logger.warning(
                    "k8s_job_executor: runner %s did not close within "
                    "%.1fs; deleting Secret to force GC",
                    meta.get("job_name"),
                    self._shutdown_grace_s,
                )
            with contextlib.suppress(Exception):
                await record.transport.close()
        with contextlib.suppress(Exception):
            await self._client.delete_secret(
                namespace=self._namespace, name=secret_name
            )

    async def health(self, handle: RuntimeHandle) -> bool:
        """Liveness probe — transport.is_alive is enough; we never
        round-trip to the API server because that would multiply
        request load across N sessions."""
        meta = dict(handle.extra)
        secret_name = meta.get("secret_name")
        if not secret_name or not isinstance(secret_name, str):
            return False
        record = self._inflight.get(secret_name)
        return record is not None and record.transport.is_alive

    @staticmethod
    async def _wait_for_close(transport: TcpTransport) -> None:
        """Block until the runner closes the transport (recv() raises)."""
        while transport.is_alive:
            try:
                await transport.recv()
            except TransportClosed:
                return

    def _build_env(
        self, spec: SessionSpec, runtime_ctx: SessionRuntimeContext
    ) -> dict[str, str]:
        """Compose the runner env. ``ANTHROPIC_API_KEY`` and the auth
        token are NOT included here — the K8s Secret machinery in
        :class:`K8sClient.create_job` is responsible for both."""
        env = {
            "GG_RELAY_SPEC_JSON": spec.to_json(),
            "GG_RELAY_TCP_LISTEN": f"0.0.0.0:{self._runner_port}",
        }
        for k, v in (runtime_ctx.credentials or {}).items():
            if v is None:
                continue
            env[k] = str(v)
        if runtime_ctx.trace_id:
            env["RELAY_TRACE_ID"] = runtime_ctx.trace_id
        return env


def _bump_queue_depth(value: int) -> None:
    """Best-effort metric update — never let a metrics-registry miss
    block the executor path."""
    try:
        from gg_relay.tracing.metrics import K8S_JOB_QUEUE_DEPTH

        K8S_JOB_QUEUE_DEPTH.set(value)
    except Exception:  # noqa: BLE001 — defensive
        pass


def _bump_failure(reason: str) -> None:
    try:
        from gg_relay.tracing.metrics import K8S_JOB_CREATION_FAILURES_TOTAL

        K8S_JOB_CREATION_FAILURES_TOTAL.labels(reason=reason).inc()
    except Exception:  # noqa: BLE001 — defensive
        pass


__all__ = [
    "K8sClient",
    "K8sJobExecutor",
    "K8sJobHandle",
    "K8sJobQueueFull",
    "K8sJobSubmitError",
]
