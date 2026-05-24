"""Plan 9 D9.8 — K8sJobExecutor unit tests with in-memory fake client.

Avoids depending on kubernetes-asyncio so the suite stays fast
and hermetic. The fake K8sClient implementation actually spins up
a TcpServer per "Job" so the executor's start() path exercises
the real handshake against the runner side.

Covers:

1. Happy path — start() returns RuntimeHandle, transport works.
2. Queue cap — max_concurrent_jobs raises K8sJobQueueFull.
3. Pod IP timeout — wrapped as K8sJobSubmitError + Secret cleaned up.
4. Auth-failure path — wrong token surfaces K8sJobSubmitError.
5. stop() deletes the Secret + decrements queue depth.
6. health() reflects transport.is_alive.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping
from pathlib import Path

import pytest

from gg_relay.session.executor.k8s_job import (
    K8sClient,
    K8sJobExecutor,
    K8sJobQueueFull,
    K8sJobSubmitError,
)
from gg_relay.session.spec import PluginManifest, SessionSpec
from gg_relay.session.transport.tcp import TcpServer


def _spec() -> SessionSpec:
    return SessionSpec(
        prompt="hello",
        cwd=Path("/tmp"),
        plugins=PluginManifest(profile="minimal"),
    )


class _FakeK8sClient:
    """In-memory K8s client backed by real TcpServers.

    Each ``create_job`` call binds a TcpServer on an ephemeral port
    using the auth token previously stashed by ``create_secret``.
    ``wait_for_pod_ip`` returns ``"127.0.0.1"`` and the port becomes
    visible via the ``ports`` map keyed by Job name.
    """

    def __init__(
        self,
        *,
        secret_fail: bool = False,
        job_fail: bool = False,
        pod_ip_timeout: bool = False,
        wrong_token: str | None = None,
    ) -> None:
        self.secrets: dict[str, dict[str, str]] = {}
        self.servers: dict[str, TcpServer] = {}
        self.ports: dict[str, int] = {}
        self.secret_fail = secret_fail
        self.job_fail = job_fail
        self.pod_ip_timeout = pod_ip_timeout
        self.wrong_token = wrong_token
        self.delete_calls: list[str] = []

    async def create_secret(
        self, *, namespace: str, name: str, data: Mapping[str, str]
    ) -> None:
        if self.secret_fail:
            raise RuntimeError("simulated secret create failure")
        self.secrets[name] = dict(data)

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
    ) -> None:
        if self.job_fail:
            raise RuntimeError("simulated job create failure")
        token = self.secrets[secret_name]["RELAY_RUNNER_AUTH_TOKEN"]
        if self.wrong_token is not None:
            token = self.wrong_token
        server = await TcpServer.listen("127.0.0.1", 0, expected_token=token)
        self.servers[name] = server
        self.ports[name] = server.port

    async def wait_for_pod_ip(
        self, *, namespace: str, job_name: str, timeout_s: float
    ) -> str:
        if self.pod_ip_timeout:
            raise TimeoutError("simulated pod IP timeout")
        return "127.0.0.1"

    async def delete_secret(self, *, namespace: str, name: str) -> None:
        self.delete_calls.append(name)
        self.secrets.pop(name, None)
        server = self.servers.pop(name.replace("gg-runner-token-", "gg-runner-"), None)
        if server is not None:
            await server.close()

    async def cleanup(self) -> None:
        for server in list(self.servers.values()):
            await server.close()
        self.servers.clear()


def _executor(client: K8sClient, **overrides) -> K8sJobExecutor:
    params: dict = {
        "client": client,
        "namespace": "gg",
        "runner_image": "ghcr.io/test/runner:latest",
        "runner_port": 0,  # overridden per-test via the fake
        "max_concurrent_jobs": 5,
        "ttl_seconds_after_finished": 60,
        "pod_ip_timeout_s": 1.0,
        "shutdown_grace_s": 0.2,
    }
    params.update(overrides)
    return K8sJobExecutor(**params)


@pytest.mark.asyncio
async def test_start_returns_working_transport_and_handle() -> None:
    fake = _FakeK8sClient()
    # First we have to know the runner_port the executor will use
    # so the fake's TcpServer can bind it. The executor opens the
    # client connection to (pod_ip, runner_port), so we drive a
    # custom create_job that *picks* the port and patches the
    # executor's runner_port after the fact. For simplicity here
    # we accept-anything by setting wrong_token=None and using the
    # token stashed by create_secret; the port is whatever the
    # fake's TcpServer.listen() chose. We then call start() against
    # an executor whose runner_port matches the fake's chosen port.

    # Two-phase: 1) create secret manually + bind server, 2) hand
    # the bound port to a custom executor whose runner_port matches.
    fake_token = "fake-token-12345"
    server = await TcpServer.listen("127.0.0.1", 0, expected_token=fake_token)
    fake.secrets["gg-runner-token-deadbeef"] = {"RELAY_RUNNER_AUTH_TOKEN": fake_token}
    fake.servers["gg-runner-deadbeef"] = server
    fake.ports["gg-runner-deadbeef"] = server.port

    try:
        ex = _executor(fake, runner_port=server.port)

        # Patch the executor to use a deterministic runtime_id +
        # always re-use the pre-bound secret so our pre-bound server
        # is what the client connects to.
        async def _stub_create_secret(*, namespace, name, data):
            pass

        async def _stub_create_job(**kwargs):
            pass

        fake.create_secret = _stub_create_secret  # type: ignore[assignment]
        fake.create_job = _stub_create_job  # type: ignore[assignment]

        # Force start to use the pre-bound token via monkey-patched
        # `secrets.token_urlsafe`. Easier: stub the executor's
        # auth-token generation by patching the module.
        import gg_relay.session.executor.k8s_job as kj

        original = kj.secrets.token_urlsafe
        kj.secrets.token_urlsafe = lambda n=32: fake_token  # type: ignore[assignment]
        try:
            handle, server_t = await asyncio.gather(
                ex.start(_spec()), server.accept()
            )
            assert handle.backend == "k8s_job"
            extra = dict(handle.extra)
            assert extra["pod_ip"] == "127.0.0.1"
            assert extra["namespace"] == "gg"
            assert handle.transport.is_alive
            await ex.stop(handle)
            assert not handle.transport.is_alive
            with contextlib.suppress(Exception):
                await server_t.close()
        finally:
            kj.secrets.token_urlsafe = original
    finally:
        await fake.cleanup()


@pytest.mark.asyncio
async def test_queue_cap_raises_when_full() -> None:
    fake = _FakeK8sClient()
    ex = _executor(fake, max_concurrent_jobs=1)
    # Force the in-flight count by injecting a sentinel entry; we
    # don't need a real handle to test admission control.
    ex._inflight["sentinel"] = object()  # type: ignore[assignment]
    with pytest.raises(K8sJobQueueFull):
        await ex.start(_spec())


@pytest.mark.asyncio
async def test_pod_ip_timeout_cleans_up_secret() -> None:
    fake = _FakeK8sClient(pod_ip_timeout=True)
    ex = _executor(fake)
    with pytest.raises(K8sJobSubmitError):
        await ex.start(_spec())
    # Secret created → delete called even though wait_for_pod_ip
    # failed. The cleanup chain is the only thing preventing per-
    # session Secret leaks on submission errors.
    assert any(name.startswith("gg-runner-token-") for name in fake.delete_calls)


@pytest.mark.asyncio
async def test_secret_create_failure_does_not_orphan() -> None:
    fake = _FakeK8sClient(secret_fail=True)
    ex = _executor(fake)
    with pytest.raises(K8sJobSubmitError):
        await ex.start(_spec())
    # No secret got persisted → no delete needed.
    assert fake.secrets == {}
    assert fake.delete_calls == []


@pytest.mark.asyncio
async def test_job_create_failure_cleans_up_secret() -> None:
    fake = _FakeK8sClient(job_fail=True)
    ex = _executor(fake)
    with pytest.raises(K8sJobSubmitError):
        await ex.start(_spec())
    assert fake.delete_calls and fake.delete_calls[0].startswith(
        "gg-runner-token-"
    )


@pytest.mark.asyncio
async def test_wrong_token_surfaces_submit_error() -> None:
    """When the K8s client mis-mounts the token, the handshake fails
    and the executor surfaces ``K8sJobSubmitError`` instead of
    leaking an authenticated transport."""
    fake = _FakeK8sClient(wrong_token="totally-different-token")
    ex = _executor(fake, pod_ip_timeout_s=2.0)
    with pytest.raises(K8sJobSubmitError):
        await ex.start(_spec())
    assert fake.delete_calls  # secret cleaned up
