"""Container entry-point. PID 1 under ``tini``.

Lifecycle:

1. Read ``GG_RELAY_SPEC_JSON`` / ``GG_RELAY_SOCKET`` / ``ANTHROPIC_API_KEY``
   from env (validated up-front so the failure mode is "container exits
   immediately with a clear error" rather than "claude CLI launches and
   crashes 30s later").
2. ``await UnixSocketTransport.connect(socket_path)`` — retries while the
   host's bind() race is open.
3. Start the :class:`WireCoordinatorProxy` consume loop as a sibling task.
4. Hand the transport to :func:`make_wire_runner` which drives the SDK.
5. On any exit (clean / cancel / signal), cancel the consume loop and close
   the transport.

Signal handling:
- ``SIGTERM`` (sent by ``docker stop``) → cooperative ``CancelledError`` into
  the runner; the runner's ``finally`` chain emits a clean session.end frame
  and disconnects from the SDK.
- ``KeyboardInterrupt`` → exit 137 (Plan 3 D3.11 maps 137 → ``cancelled``).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

from gg_relay.session.client import make_wire_runner
from gg_relay.session.hitl.policy import ToolPolicy
from gg_relay.session.runner.proxy_client import WireCoordinatorProxy
from gg_relay.session.spec import SessionSpec
from gg_relay.session.transport.tcp import TcpServer
from gg_relay.session.transport.unixsocket import UnixSocketTransport

logger = logging.getLogger("gg_relay.wire_runner")

# Container-side policy: every tool goes through HITL because the *host* owns
# the policy decision. The wire runner never auto-accepts — that would bypass
# the host's audit log and ToolPolicy invariants.
_HOST_DELEGATING_POLICY = ToolPolicy(
    auto_accept_tools=frozenset(),
    hitl_tools=frozenset(),
    neutral_tools=frozenset(),
    path_required_tools=frozenset(),
    dangerous_patterns=(),
)


_REQUIRED_ENV_COMMON = ("GG_RELAY_SPEC_JSON", "ANTHROPIC_API_KEY")
_REQUIRED_ENV_UNIX = ("GG_RELAY_SOCKET",)
_REQUIRED_ENV_TCP = ("GG_RELAY_TCP_LISTEN", "RELAY_RUNNER_AUTH_TOKEN")


def _check_env(*, tcp_mode: bool) -> None:
    """Fail fast with a precise error if the launcher forgot to pass an env.

    The runner has two mutually-exclusive transport modes:

    * **Unix socket** (default, Plan 3): host pre-binds the AF_UNIX
      socket and passes its path via ``GG_RELAY_SOCKET``.
    * **TCP listen** (Plan 9 D9.8): runner binds a TCP socket on
      ``GG_RELAY_TCP_LISTEN`` (e.g. ``0.0.0.0:9001``) and waits for
      the host to connect with the token in
      ``RELAY_RUNNER_AUTH_TOKEN``.
    """
    required = _REQUIRED_ENV_COMMON + (
        _REQUIRED_ENV_TCP if tcp_mode else _REQUIRED_ENV_UNIX
    )
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            f"wire_runner: missing required env vars: {', '.join(missing)}"
        )


async def _connect_transport(*, tcp_mode: bool) -> Any:
    """Return the bidirectional transport for the chosen mode.

    Returns ``Any`` to side-step the difference between
    :class:`UnixSocketTransport` and :class:`TcpTransport` at the
    type-check level — both satisfy the :class:`SessionTransport`
    Protocol at runtime which is all the runner needs.
    """
    if tcp_mode:
        bind = os.environ["GG_RELAY_TCP_LISTEN"]
        token = os.environ["RELAY_RUNNER_AUTH_TOKEN"]
        host, _, port_str = bind.rpartition(":")
        if not host or not port_str.isdigit():
            raise SystemExit(
                f"wire_runner: invalid GG_RELAY_TCP_LISTEN={bind!r}; "
                f"expected 'host:port'"
            )
        server = await TcpServer.listen(host, int(port_str), expected_token=token)
        try:
            return await server.accept(timeout=120.0)
        except TimeoutError as e:
            raise SystemExit(
                "wire_runner: TCP listen timed out waiting for host connection"
            ) from e
    socket_path = Path(os.environ["GG_RELAY_SOCKET"])
    return await UnixSocketTransport.connect(socket_path, retry_timeout=15.0)


async def _amain() -> int:
    tcp_mode = bool(os.environ.get("GG_RELAY_TCP_LISTEN"))
    _check_env(tcp_mode=tcp_mode)

    spec = SessionSpec.from_json(os.environ["GG_RELAY_SPEC_JSON"])
    transport = await _connect_transport(tcp_mode=tcp_mode)

    coordinator = WireCoordinatorProxy(transport)
    consume_task = asyncio.create_task(
        coordinator.consume_loop(), name="wire-consume-loop"
    )

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _on_signal() -> None:
        logger.info("wire_runner: SIGTERM received, requesting shutdown")
        stop_event.set()

    # tini forwards SIGTERM to PID 2 (us). Register a cooperative handler so
    # the runner gets a chance to flush session.end before the container
    # halts. Use _suppress_signal_install_error so test envs that lack signal
    # support (e.g. running tests inside a non-main-thread asyncio loop) don't
    # crash here.
    for sig in (signal.SIGTERM, signal.SIGINT):
        with _suppress_signal_install_error():
            loop.add_signal_handler(sig, _on_signal)  # noqa: SIM117

    runner_fn = make_wire_runner(
        policy=_HOST_DELEGATING_POLICY, coordinator=coordinator
    )

    async def _run_runner() -> None:
        await runner_fn(transport, spec)

    runner_task: asyncio.Task[None] = asyncio.create_task(
        _run_runner(), name="wire-runner"
    )

    async def _wait_stop() -> None:
        await stop_event.wait()

    stop_task: asyncio.Task[None] = asyncio.create_task(
        _wait_stop(), name="wire-stop-wait"
    )
    done, _pending = await asyncio.wait(
        {runner_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    if stop_task not in done:
        stop_task.cancel()

    if runner_task not in done:
        # Signal arrived first; cancel the runner cooperatively. The runner's
        # finally chain still publishes session.end + disconnects the SDK.
        runner_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner_task

    consume_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, SystemExit):
        await consume_task

    await transport.close()

    # Surface unhandled runner exceptions (e.g. pre_run_cmd failures) as a
    # non-zero exit code so DockerExecutor / k8s_job can map the container
    # outcome to a failed session — without this the host bridge sees a
    # clean disconnect and SessionManager records 'completed' even after a
    # crash. Cancellation is still treated as graceful (exit 0).
    if runner_task.done() and not runner_task.cancelled():
        exc = runner_task.exception()
        if exc is not None:
            logger.error("wire_runner: runner task failed: %s", exc)
            return 1
    return 0


class _suppress_signal_install_error:
    """Context manager: silently swallow ``ValueError`` / ``NotImplementedError``
    from ``loop.add_signal_handler``. Some platforms (Windows, some test
    runners) don't support signal handlers on the event loop; we should not
    crash there."""

    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> bool:
        return exc_type is not None and issubclass(
            exc_type, (ValueError, NotImplementedError)
        )


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def main() -> None:
    """Synchronous entry-point that ``python -m gg_relay.session.runner.wire_runner``
    invokes. Maps ``KeyboardInterrupt`` to exit 137 (D3.11)."""
    _setup_logging()
    try:
        sys.exit(asyncio.run(_amain()))
    except KeyboardInterrupt:
        sys.exit(137)


if __name__ == "__main__":  # pragma: no cover — exercised via docker only
    main()
