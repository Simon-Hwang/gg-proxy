"""kubernetes-asyncio adapter for :class:`K8sJobExecutor` (Plan 9 D9.8).

This is the only module that imports ``kubernetes_asyncio``; the
import is deferred to construction time so the rest of gg-relay
runs without the ``[k8s]`` extra installed.

Production wiring (``api/main.py``) constructs
:class:`KubernetesAsyncIOClient` lazily the first time
``executor_kind="k8s_job"`` produces a session. Unit tests don't
exercise this file — they inject a stub satisfying the
:class:`K8sClient` Protocol directly.

The Job spec produced here implements:

* ``ownerReferences`` pointing at the per-session Secret so K8s GC
  removes the Job + Pod when the Secret is deleted (D9.8 BLOCKER B8).
* ``ttlSecondsAfterFinished`` to bound the etcd object count
  (R9.12).
* ``runAsNonRoot``, ``readOnlyRootFilesystem`` + dropped capabilities
  to inherit the same security posture as the main relay Deployment
  in ``deploy/k8s/deployment.yaml``.
* The wire runner is invoked with the ``GG_RELAY_TCP_LISTEN`` env
  set to ``0.0.0.0:<runner_port>`` so it binds the TCP listener
  (see ``session/runner/wire_runner.py``).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger("gg_relay.executor.k8s_client")


class KubernetesAsyncIOClient:
    """Thin wrapper around ``kubernetes_asyncio.client``.

    Initialisation loads in-cluster config (``ServiceAccount`` token
    + CA bundle from ``/var/run/secrets/kubernetes.io/serviceaccount``)
    on first use; falling back to ``$KUBECONFIG`` for out-of-cluster
    operator-local testing.
    """

    def __init__(self) -> None:
        self._ready = False
        self._core_v1: Any = None
        self._batch_v1: Any = None
        self._lock = asyncio.Lock()

    async def _ensure_ready(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            try:
                from kubernetes_asyncio import (  # type: ignore[import-not-found]
                    client,
                    config,
                )
            except ImportError as e:
                raise RuntimeError(
                    "kubernetes-asyncio is required for executor_kind=k8s_job; "
                    "install with `pip install 'gg-relay[k8s]'`"
                ) from e
            try:
                config.load_incluster_config()
            except config.ConfigException:
                # Out-of-cluster fallback for operator-local testing.
                await config.load_kube_config()
            api_client = client.ApiClient()
            self._core_v1 = client.CoreV1Api(api_client)
            self._batch_v1 = client.BatchV1Api(api_client)
            self._ready = True

    async def create_secret(
        self, *, namespace: str, name: str, data: Mapping[str, str]
    ) -> None:
        from kubernetes_asyncio import client

        await self._ensure_ready()
        body = client.V1Secret(
            metadata=client.V1ObjectMeta(name=name, labels={"app": "gg-runner"}),
            type="Opaque",
            string_data=dict(data),
        )
        await self._core_v1.create_namespaced_secret(namespace=namespace, body=body)

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
        from kubernetes_asyncio import client

        await self._ensure_ready()
        secret = await self._core_v1.read_namespaced_secret(
            name=secret_name, namespace=namespace
        )
        owner_ref = client.V1OwnerReference(
            api_version="v1",
            kind="Secret",
            name=secret_name,
            uid=secret.metadata.uid,
            block_owner_deletion=True,
            controller=True,
        )

        plain_env = [client.V1EnvVar(name=k, value=v) for k, v in env.items()]
        token_env = client.V1EnvVar(
            name="RELAY_RUNNER_AUTH_TOKEN",
            value_from=client.V1EnvVarSource(
                secret_key_ref=client.V1SecretKeySelector(
                    name=secret_name, key="RELAY_RUNNER_AUTH_TOKEN"
                )
            ),
        )
        container = client.V1Container(
            name="runner",
            image=image,
            command=["python", "-m", "gg_relay.session.runner.wire_runner"],
            env=plain_env + [token_env],
            ports=[
                client.V1ContainerPort(
                    name="runner",
                    container_port=runner_port,
                    protocol="TCP",
                )
            ],
            security_context=client.V1SecurityContext(
                allow_privilege_escalation=False,
                read_only_root_filesystem=True,
                run_as_non_root=True,
                run_as_user=10001,
                capabilities=client.V1Capabilities(drop=["ALL"]),
            ),
        )
        pod_spec = client.V1PodSpec(
            containers=[container],
            restart_policy="Never",
            automount_service_account_token=False,
        )
        job = client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=client.V1ObjectMeta(
                name=name,
                labels={"app": "gg-runner"},
                owner_references=[owner_ref],
            ),
            spec=client.V1JobSpec(
                ttl_seconds_after_finished=ttl_seconds_after_finished,
                backoff_limit=0,
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels={"app": "gg-runner"}),
                    spec=pod_spec,
                ),
            ),
        )
        await self._batch_v1.create_namespaced_job(namespace=namespace, body=job)

    async def wait_for_pod_ip(
        self, *, namespace: str, job_name: str, timeout_s: float
    ) -> str:
        await self._ensure_ready()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            pods = await self._core_v1.list_namespaced_pod(
                namespace=namespace,
                label_selector=f"job-name={job_name}",
            )
            for pod in pods.items:
                pod_ip = getattr(pod.status, "pod_ip", None)
                if pod_ip:
                    return str(pod_ip)
            await asyncio.sleep(0.5)
        raise TimeoutError(
            f"Pod for Job {job_name} did not get an IP within {timeout_s}s"
        )

    async def delete_secret(self, *, namespace: str, name: str) -> None:
        from kubernetes_asyncio import client

        await self._ensure_ready()
        opts = client.V1DeleteOptions(propagation_policy="Background")
        try:
            await self._core_v1.delete_namespaced_secret(
                name=name, namespace=namespace, body=opts
            )
        except Exception as e:  # noqa: BLE001 — k8s exc hierarchy is broad
            logger.warning(
                "k8s_client: delete_secret(%s) failed: %s; cascading GC "
                "will fall back to ttlSecondsAfterFinished",
                name,
                e,
            )


__all__ = ["KubernetesAsyncIOClient"]
