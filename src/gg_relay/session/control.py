"""Shared pause/resume control plumbing (Plan 6 D6.11).

Sits between :class:`SessionManager` and the runner that owns the
:class:`ClaudeSDKClient`. The host pushes a :class:`ControlMessage`
(``pause`` or ``resume``) onto an inbox; the runner's control loop pops
it, invokes the SDK API, and pushes a :class:`ControlAck` back to wake
the host.

Two backends share the abstraction:

* **Docker executor** — the wire bridge translates pause/resume into
  :class:`PauseFrame` / :class:`ResumeFrame` over the unix-socket
  transport; the container-side proxy converts those frames into
  :class:`ControlMessage` items on a per-runner :class:`ControlChannel`.
  Acks travel back as :class:`PauseAckFrame` / :class:`ResumeAckFrame`.
* **In-process executor** — host and runner share the same event loop,
  so we hand them a :class:`ControlChannel` directly; no socket roundtrip.

The shared :class:`ControlLoop` consumer handles both cases: it doesn't
care whether the queue is backed by a transport-routing proxy or a plain
in-memory queue.
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

logger = logging.getLogger("gg_relay.session.control")

ControlOp = Literal["pause", "resume"]


@dataclass(frozen=True, slots=True)
class ControlMessage:
    """Single pause/resume directive flowing host → runner."""

    op: ControlOp
    req_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    """``payload`` carries the op-specific fields (``reason`` for pause,
    ``hint`` for resume) so the runner can stay generic."""


@dataclass(frozen=True, slots=True)
class ControlAck:
    """Runner → host reply for a single :class:`ControlMessage`."""

    op: ControlOp
    req_id: str
    ok: bool
    error: str | None = None


class _SDKClientLike(Protocol):
    """Duck-typed slice of :class:`claude_code_sdk.ClaudeSDKClient` the
    control loop relies on. Tests substitute a stub satisfying this
    signature without depending on the real SDK package."""

    async def interrupt(self) -> Any: ...
    async def query(self, prompt: str) -> Any: ...


class ControlChannel:
    """In-memory pause/resume bridge with ack-correlation.

    The host calls :meth:`host_request` and awaits a :class:`ControlAck`;
    the runner pulls via :meth:`runner_recv` and finalises via
    :meth:`runner_ack`. ``host_request`` blocks for at most
    ``ack_timeout_s`` after which it returns a synthetic
    ``error="bridge_ack_timeout"`` ack so the caller (typically
    :class:`SessionManager.pause`) can choose to surface a 504/retry rather
    than hanging forever on a misbehaving runner.

    Thread / loop note: this class assumes single-loop access; do not
    share an instance across event loops.
    """

    def __init__(self, *, ack_timeout_s: float = 5.0) -> None:
        self._inbox: asyncio.Queue[ControlMessage] = asyncio.Queue()
        self._ack_futs: dict[str, asyncio.Future[ControlAck]] = {}
        self._ack_timeout = ack_timeout_s
        self._counter = itertools.count(1)
        self._closed = False

    @property
    def ack_timeout_s(self) -> float:
        return self._ack_timeout

    @property
    def closed(self) -> bool:
        return self._closed

    def _next_req_id(self, op: ControlOp) -> str:
        return f"{op}-{next(self._counter)}"

    async def host_request(
        self, op: ControlOp, payload: dict[str, Any] | None = None
    ) -> ControlAck:
        """Push a ``pause``/``resume`` onto the runner queue, await its ack.

        Returns the ack — never raises (timeout is reported via
        ``ok=False`` + ``error='bridge_ack_timeout'`` so callers can
        switch on a single field).
        """
        if self._closed:
            return ControlAck(op=op, req_id="<closed>", ok=False, error="channel_closed")
        req_id = self._next_req_id(op)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ControlAck] = loop.create_future()
        self._ack_futs[req_id] = fut
        await self._inbox.put(ControlMessage(op=op, req_id=req_id, payload=dict(payload or {})))
        try:
            return await asyncio.wait_for(fut, timeout=self._ack_timeout)
        except TimeoutError:
            logger.warning(
                "ControlChannel.host_request(%s,req_id=%s) timed out after %.2fs",
                op,
                req_id,
                self._ack_timeout,
            )
            return ControlAck(
                op=op, req_id=req_id, ok=False, error="bridge_ack_timeout"
            )
        finally:
            self._ack_futs.pop(req_id, None)

    async def runner_recv(self) -> ControlMessage:
        """Block until the host pushes a pause/resume directive."""
        return await self._inbox.get()

    async def push(self, msg: ControlMessage) -> None:
        """Wire-side ingress: the docker proxy invokes this when a
        :class:`PauseFrame` / :class:`ResumeFrame` arrives on the transport.

        Distinct from :meth:`host_request` only in that the caller is the
        bridge translating wire frames, not the host scheduler itself —
        the bridge does NOT need an ack waiter (the ack will flow back
        as a transport frame and be re-injected via
        :meth:`host_inject_ack` on the host side of the wire).
        """
        await self._inbox.put(msg)

    async def runner_ack(self, ack: ControlAck) -> None:
        """Resolve the matching host future (no-op if already cancelled).

        Async to match :data:`AckSender` so the in-process path can hand
        :class:`ControlLoop` this method directly without an adapter; the
        wire path uses :meth:`WireCoordinatorProxy.send_ack` which is
        also async (it writes a frame onto the transport).
        """
        fut = self._ack_futs.get(ack.req_id)
        if fut is not None and not fut.done():
            fut.set_result(ack)

    def host_inject_ack(self, ack: ControlAck) -> None:
        """Wire-side ingress: the docker bridge invokes this when an ack
        frame arrives on the transport. Sync because :class:`WireBridge`
        runs inside its consume loop and just needs to flip a future.
        """
        fut = self._ack_futs.get(ack.req_id)
        if fut is not None and not fut.done():
            fut.set_result(ack)

    def close(self) -> None:
        """Mark channel closed; any pending futures resolve with an error."""
        if self._closed:
            return
        self._closed = True
        for req_id, fut in list(self._ack_futs.items()):
            if not fut.done():
                fut.set_result(
                    ControlAck(op="pause", req_id=req_id, ok=False, error="channel_closed")
                )


AckSender = Callable[[ControlAck], Awaitable[None]]
"""Callback invoked by :class:`ControlLoop` to deliver an ack to the host.

* Wire mode: writes a :class:`PauseAckFrame` / :class:`ResumeAckFrame`
  back over the transport.
* In-process mode: calls :meth:`ControlChannel.runner_ack` directly.
"""


class ControlLoop:
    """Per-runner consumer of pause/resume directives.

    Wraps a :class:`ControlChannel` (or any object exposing
    :meth:`runner_recv`) and a :class:`_SDKClientLike` handle, applying
    pause via ``client.interrupt()`` and resume via ``client.query(hint)``.

    The loop tracks a small ``paused`` boolean and rejects redundant ops
    with ``ok=False`` + a descriptive error string. The plan opts for a
    runner-side reject (instead of swallowing) because the host already
    serialises pause/resume via :class:`SessionManager`'s state machine
    and a redundant op signals a bug worth reporting.
    """

    def __init__(
        self,
        *,
        client: _SDKClientLike,
        recv: Callable[[], Awaitable[ControlMessage]],
        ack: AckSender,
    ) -> None:
        self._client = client
        self._recv = recv
        self._ack = ack
        self._paused = False
        self._stopped = asyncio.Event()

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    async def run(self) -> None:
        """Drain directives until cancelled.

        The loop NEVER swallows :class:`asyncio.CancelledError` — the runner
        cancels this task on session teardown so we can let the parent
        notice the cancellation via the awaited ``runner_task``.
        """
        try:
            while True:
                msg = await self._recv()
                await self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        finally:
            self._stopped.set()

    async def _dispatch(self, msg: ControlMessage) -> None:
        if msg.op == "pause":
            await self._handle_pause(msg)
        elif msg.op == "resume":
            await self._handle_resume(msg)
        else:  # pragma: no cover - defensive; ControlOp Literal blocks this at type
            await self._ack(
                ControlAck(
                    op=msg.op, req_id=msg.req_id, ok=False, error="unknown_op"
                )
            )

    async def _handle_pause(self, msg: ControlMessage) -> None:
        if self._paused:
            await self._ack(
                ControlAck(op="pause", req_id=msg.req_id, ok=False, error="already_paused")
            )
            return
        try:
            await self._client.interrupt()
        except Exception as exc:
            await self._ack(
                ControlAck(
                    op="pause",
                    req_id=msg.req_id,
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            return
        self._paused = True
        await self._ack(ControlAck(op="pause", req_id=msg.req_id, ok=True))

    async def _handle_resume(self, msg: ControlMessage) -> None:
        if not self._paused:
            await self._ack(
                ControlAck(op="resume", req_id=msg.req_id, ok=False, error="not_paused")
            )
            return
        hint_raw = msg.payload.get("hint")
        hint = hint_raw if isinstance(hint_raw, str) and hint_raw else "continue"
        try:
            await self._client.query(hint)
        except Exception as exc:
            await self._ack(
                ControlAck(
                    op="resume",
                    req_id=msg.req_id,
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            return
        self._paused = False
        await self._ack(ControlAck(op="resume", req_id=msg.req_id, ok=True))


async def cancel_control_task(task: asyncio.Task[None] | None) -> None:
    """Helper: cancel + await a control-loop task, swallowing the
    expected :class:`asyncio.CancelledError`. Used by the runner finally
    chain and by the executor shutdown path so both can be one-liners.
    """
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
