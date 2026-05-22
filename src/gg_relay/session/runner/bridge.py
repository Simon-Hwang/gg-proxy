"""WireBridge — host-side EventFrame consumer for the docker backend.

In the in-process backend, the host has direct access to the
:class:`HITLCoordinator` and the SDK runs in the same loop, so the runner's
EventFrames are routed wherever the calling handler / SessionManager wants.

In the docker backend, EventFrames arrive over a unix socket; we need a
host-side coroutine that:

  1. Drains :class:`SessionTransport`.recv() until ``session.end`` or
     :class:`TransportClosed`.
  2. Routes ``tool.request`` to :class:`HITLCoordinator.request` and writes
     the decision back as a ``tool.decision`` ControlFrame.
  3. Buffers every other EventFrame (msg.chunk, install.done, error,
     session.end) into :attr:`frames`. Plan 4's persistence layer reads
     this buffer.
  4. Answers ``ping`` with ``pong`` (Task 9 heartbeat).
  5. On shutdown(), emits a ``shutdown`` ControlFrame and waits up to
     ``grace`` seconds for the runner to publish ``session.end``.

The bridge owns the recv side of the transport; the runner side of the
transport owns the send side. Symmetric with WireCoordinatorProxy.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

from gg_relay.session.control import ControlAck
from gg_relay.session.frames import (
    make_error,
    make_pause,
    make_ping,
    make_resume,
    make_shutdown,
    make_tool_decision,
)
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.transport.protocol import (
    EventFrame,
    SessionTransport,
    TransportClosed,
)

logger = logging.getLogger("gg_relay.bridge")


class BridgeAckTimeout(Exception):
    """Raised when :meth:`WireBridge.pause` / :meth:`WireBridge.resume`
    don't receive the matching ack within the configured timeout (default
    5s). The route layer maps this to HTTP 504.
    """


class WireBridge:
    """Host-side event consumer / control responder for the docker backend.

    Construction does NOT start any work; call :meth:`run` (typically wrapped
    in ``asyncio.create_task``) to begin draining the transport. The bridge
    is single-use: once :attr:`finished` is set, do not call run() again.
    """

    def __init__(
        self,
        transport: SessionTransport,
        coordinator: HITLCoordinator,
        *,
        sequence_seed: int = 0,
        heartbeat_interval_s: float = 5.0,
        heartbeat_misses_before_unhealthy: int = 3,
        on_heartbeat_timeout: Callable[[], Awaitable[None]] | None = None,
        ack_timeout_s: float = 5.0,
    ) -> None:
        self._transport = transport
        self._coordinator = coordinator
        self._frames: list[EventFrame] = []
        self._seq = sequence_seed
        self._shutdown_event = asyncio.Event()
        self._finished_event = asyncio.Event()
        self._tool_tasks: set[asyncio.Task[None]] = set()
        # Heartbeat state (Plan 3 D3.10). The host sends `ping`; the runner
        # replies `pong`; we tolerate `heartbeat_misses_before_unhealthy`
        # missed pongs before declaring the runner dead.
        self._heartbeat_interval = heartbeat_interval_s
        self._heartbeat_misses_threshold = heartbeat_misses_before_unhealthy
        self._on_heartbeat_timeout = on_heartbeat_timeout
        self._heartbeat_misses = 0
        self._last_pong_seq: int = -1
        self._heartbeat_unhealthy = False
        self._heartbeat_task: asyncio.Task[None] | None = None
        # Plan 6 D6.11 pause/resume state.
        self._ack_timeout_s = ack_timeout_s
        self._ack_futs: dict[str, asyncio.Future[ControlAck]] = {}
        self._pause_seq = 0

    @property
    def frames(self) -> list[EventFrame]:
        """All EventFrames the runner emitted (read-only snapshot reference).

        Plan 4 will replace this with a sink Protocol (store + tracing); for
        now Plan 3 keeps a simple in-memory list so integration tests can
        assert on the sequence."""
        return self._frames

    @property
    def finished(self) -> bool:
        return self._finished_event.is_set()

    async def wait_finished(self, timeout: float | None = None) -> None:
        if timeout is None:
            await self._finished_event.wait()
            return
        await asyncio.wait_for(self._finished_event.wait(), timeout=timeout)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    @property
    def heartbeat_unhealthy(self) -> bool:
        """True once we observed ``heartbeat_misses_before_unhealthy``
        consecutive missed pongs. Plan 4's SessionManager will probe this
        between bridge runs."""
        return self._heartbeat_unhealthy

    async def run(self) -> None:
        """Consume EventFrames until ``session.end`` arrives or the transport
        closes. Also spawns the heartbeat sender so ``run()`` is a single
        owning entry-point for all the bridge's coroutines."""
        if self._heartbeat_interval > 0:
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(), name="wire-bridge-heartbeat"
            )
        try:
            while not self._shutdown_event.is_set():
                try:
                    frame = await self._transport.recv()
                except TransportClosed:
                    break
                ftype = frame.get("type")
                if ftype == "tool.request":
                    task = asyncio.create_task(self._handle_tool_request(frame))
                    # Track so shutdown can await them; auto-discard on
                    # completion to keep the set bounded.
                    self._tool_tasks.add(task)
                    task.add_done_callback(self._tool_tasks.discard)
                elif ftype == "pong":
                    # Runner is alive; reset the miss counter.
                    self._heartbeat_misses = 0
                    self._last_pong_seq = int(frame.get("seq", -1))
                elif ftype in ("pause.ack", "resume.ack"):
                    # Plan 6 D6.11: route the ack back to the pending
                    # :meth:`pause` / :meth:`resume` caller. Buffer the
                    # frame for persistence too so the SessionManager can
                    # publish a SessionStateChanged after a successful
                    # ack — keeps the wire-frame audit log complete.
                    self._frames.append(frame)
                    self._handle_pause_ack(frame)
                else:
                    self._frames.append(frame)
                    if ftype == "session.end":
                        break
        finally:
            if self._heartbeat_task is not None:
                self._heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._heartbeat_task
                self._heartbeat_task = None
            self._finished_event.set()

    async def _handle_tool_request(self, frame: EventFrame) -> None:
        req_id = cast(str, frame.get("req_id", ""))
        tool = cast(str, frame.get("tool", "?"))
        args = cast(dict[str, Any], frame.get("args", {}) or {})
        try:
            decision = await self._coordinator.request(req_id, tool=tool, args=args)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The coordinator should never raise mid-request, but if it does
            # we MUST still answer the runner — otherwise the SDK call in
            # the container hangs until container timeout.
            logger.exception("coordinator.request raised for req_id=%s", req_id)
            decision = "deny"
        with contextlib.suppress(TransportClosed):
            await self._transport.send(
                make_tool_decision(self._next_seq(), req_id, decision)
            )

    async def _heartbeat_loop(self) -> None:
        """Periodically send ``ping`` ControlFrames. The receive loop counts
        misses by checking ``_heartbeat_misses`` between iterations: each
        ``ping`` increments the counter; an incoming ``pong`` resets it. After
        ``_heartbeat_misses_threshold`` consecutive misses the bridge marks
        itself unhealthy, optionally invokes the caller-supplied
        ``on_heartbeat_timeout`` callback, and exits (so SessionManager can
        decide whether to call ``executor.stop()`` or restart)."""
        try:
            while not self._shutdown_event.is_set():
                if self._heartbeat_misses >= self._heartbeat_misses_threshold:
                    await self._handle_heartbeat_timeout()
                    return
                try:
                    await self._transport.send(make_ping(self._next_seq()))
                except TransportClosed:
                    return
                self._heartbeat_misses += 1
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=self._heartbeat_interval,
                    )
                    return
                except TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise

    async def _handle_heartbeat_timeout(self) -> None:
        """Mark unhealthy, emit an `error` EventFrame for downstream
        consumers, and invoke the user callback if any."""
        self._heartbeat_unhealthy = True
        logger.warning(
            "WireBridge: %d consecutive missed pongs — runner marked unhealthy",
            self._heartbeat_misses_threshold,
        )
        # Buffer an error frame so the persistence layer + IM card render
        # the cause-of-death.
        self._frames.append(
            cast(
                EventFrame,
                make_error(
                    self._next_seq(),
                    "heartbeat_timeout",
                    f"{self._heartbeat_misses_threshold} consecutive missed pongs",
                ),
            )
        )
        if self._on_heartbeat_timeout is not None:
            with contextlib.suppress(Exception):
                await self._on_heartbeat_timeout()

    async def pause(self, *, reason: str | None = None) -> ControlAck:
        """Send a :class:`PauseFrame` and await its ack (Plan 6 D6.11).

        Raises :class:`BridgeAckTimeout` after ``ack_timeout_s`` so the
        SessionManager can map the failure onto a 504 Gateway Timeout
        rather than blocking the API request indefinitely. Returns the
        :class:`ControlAck` payload on success (caller inspects ``ok`` to
        decide between 202 and 409).
        """
        self._pause_seq += 1
        req_id = f"pause-{self._pause_seq}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ControlAck] = loop.create_future()
        self._ack_futs[req_id] = fut
        try:
            with contextlib.suppress(TransportClosed):
                await self._transport.send(
                    make_pause(self._next_seq(), req_id, reason=reason)
                )
            try:
                return await asyncio.wait_for(fut, timeout=self._ack_timeout_s)
            except TimeoutError as exc:
                raise BridgeAckTimeout(
                    f"pause ack timeout after {self._ack_timeout_s:.1f}s req_id={req_id}"
                ) from exc
        finally:
            self._ack_futs.pop(req_id, None)

    async def resume(self, *, hint: str | None = None) -> ControlAck:
        """Send a :class:`ResumeFrame` and await its ack (Plan 6 D6.11)."""
        self._pause_seq += 1
        req_id = f"resume-{self._pause_seq}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ControlAck] = loop.create_future()
        self._ack_futs[req_id] = fut
        try:
            with contextlib.suppress(TransportClosed):
                await self._transport.send(
                    make_resume(self._next_seq(), req_id, hint=hint)
                )
            try:
                return await asyncio.wait_for(fut, timeout=self._ack_timeout_s)
            except TimeoutError as exc:
                raise BridgeAckTimeout(
                    f"resume ack timeout after {self._ack_timeout_s:.1f}s req_id={req_id}"
                ) from exc
        finally:
            self._ack_futs.pop(req_id, None)

    def _handle_pause_ack(self, frame: EventFrame) -> None:
        req_id_raw = frame.get("req_id", "")
        req_id = req_id_raw if isinstance(req_id_raw, str) else ""
        if not req_id:
            return
        fut = self._ack_futs.get(req_id)
        if fut is None or fut.done():
            return
        ftype = frame.get("type", "")
        err_raw = frame.get("error")
        err = err_raw if isinstance(err_raw, str) else None
        # ControlOp Literal accepts "pause" / "resume" only; ftype is
        # validated by the elif branch above.
        ack = ControlAck(
            op="pause" if ftype == "pause.ack" else "resume",
            req_id=req_id,
            ok=bool(frame.get("ok", False)),
            error=err,
        )
        fut.set_result(ack)

    async def shutdown(self, *, grace: float = 5.0) -> None:
        """Politely tell the runner to exit, wait up to ``grace`` for
        ``session.end``, then close the transport.

        Idempotent — calling shutdown() twice is safe (second call short-
        circuits if already in flight)."""
        if self._shutdown_event.is_set():
            return
        with contextlib.suppress(TransportClosed):
            await self._transport.send(make_shutdown(self._next_seq()))
        self._shutdown_event.set()
        try:
            await asyncio.wait_for(self._finished_event.wait(), timeout=grace)
        except TimeoutError:
            logger.warning(
                "WireBridge.shutdown: runner did not publish session.end within %.1fs",
                grace,
            )
        # Cancel any in-flight tool.request handlers — the runner is going
        # away so its tool.decision reply is moot anyway.
        for task in list(self._tool_tasks):
            task.cancel()
        for task in list(self._tool_tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        with contextlib.suppress(Exception):
            await self._transport.close()
