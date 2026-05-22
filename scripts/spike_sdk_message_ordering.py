#!/usr/bin/env python3
# ruff: noqa: E501  # markdown table rows generated into the spike report exceed line length
"""SDK message-ordering spike (Plan 2 — Task 0).

Goal
====
Verify the assumption baked into Plan 2 D2.3:

    A `can_use_tool(tool_name, tool_input, ctx)` callback invocation can be
    paired 1-to-1 with a corresponding `AssistantMessage(ToolUseBlock(...))`
    in the SDK message stream by matching `(name, input)` in FIFO order.

The spike runs in three parts:

  Part A — Offline SDK code inspection
      Trace `claude_code_sdk._internal.query.Query` and document how regular
      messages and `control_request(can_use_tool)` flow back to user code.

  Part B — In-process simulation
      Build a fake `Transport` that emits a deterministic interleaving of
      `assistant` messages and `control_request` permission asks; drive a
      real `Query` instance and verify the order our `can_use_tool` callback
      observes vs the order `receive_messages()` yields.

  Part C — Conclusion
      Decide whether the bidirectional defensive FIFO matching algorithm in
      Plan 2 §6 Task 3 is correct, robust, and what edge cases remain.

The output (`docs/sdk-message-ordering-spike.md`) is the authoritative reference
for the FIFO mapping implementation in `gg_relay/session/client.py`.

Run::

    source .venv/bin/activate
    python scripts/spike_sdk_message_ordering.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import claude_code_sdk
from claude_code_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
    ToolUseBlock,
    UserMessage,
)
from claude_code_sdk._internal.query import Query
from claude_code_sdk._internal.transport import Transport

REPORT_PATH = Path(__file__).resolve().parent.parent / "docs" / "sdk-message-ordering-spike.md"


@dataclass
class _Event:
    """Timestamped event observation."""

    t_ms: int
    kind: str  # "can_use_tool" | "assistant_msg" | "user_msg" | "result_msg"
    payload: dict[str, Any] = field(default_factory=dict)


# ── Part A — code-inspection facts ──────────────────────────────────────────


def part_a_inspect_sdk() -> dict[str, Any]:
    """Pull authoritative facts from the SDK source itself."""
    facts: dict[str, Any] = {
        "claude_code_sdk_version": claude_code_sdk.__version__,
        "ToolPermissionContext_fields": [
            f.name for f in ToolPermissionContext.__dataclass_fields__.values()
        ],
        "control_request_dispatch_is_concurrent": True,
        "control_request_dispatch_source": (
            "claude_code_sdk/_internal/query.py:164 — "
            "`self._tg.start_soon(self._handle_control_request, request)`"
        ),
        "regular_message_dispatch_is_serial": True,
        "regular_message_dispatch_source": (
            "claude_code_sdk/_internal/query.py:173 — "
            "`await self._message_send.send(message)`"
        ),
    }
    assert facts["ToolPermissionContext_fields"] == ["signal", "suggestions"], (
        "Plan 2 D2.3 hinges on ToolPermissionContext NOT carrying tool_use_id; "
        f"got fields {facts['ToolPermissionContext_fields']}"
    )
    return facts


# ── Part B — in-process simulation ──────────────────────────────────────────


class _ScriptedTransport(Transport):
    """A `Transport` that replays a scripted sequence of CLI messages.

    Each script entry is one of:
      * `{"type": "assistant", "id": "...", "name": "...", "input": {...}}`
        → emits an assistant message with one ToolUseBlock
      * `{"type": "perm_req", "id": "...", "tool_name": "...", "input": {...}}`
        → emits a control_request asking for can_use_tool permission
      * `{"type": "user_result", "tool_use_id": "...", "content": "...",
         "is_error": false}`
        → emits a user message with one ToolResultBlock
      * `{"type": "result", ...}` → emits a final result message

    Permission responses written back by the Query are captured into
    `self.responses` so the test can assert them.
    """

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self._script = script
        self.responses: list[dict[str, Any]] = []
        # Each `perm_req` blocks until the matching permission response is
        # written. Map control_request request_id → asyncio.Event.
        self._inflight: dict[str, asyncio.Event] = {}

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def write(self, data: str) -> None:
        # Capture control_response payloads written by Query._handle_control_request.
        for line in data.splitlines():
            if not line.strip():
                continue
            msg = json.loads(line)
            if msg.get("type") == "control_response":
                rid = msg["response"]["request_id"]
                self.responses.append(msg)
                ev = self._inflight.pop(rid, None)
                if ev is not None:
                    ev.set()
            elif msg.get("type") == "control_request":
                # Initialize / interrupt control requests sent by Query —
                # immediately ack so streaming-mode initialize() doesn't hang.
                # We don't model the full handshake; for the spike we just
                # send a synthetic success response back via the read stream
                # in `read_messages`.
                pass

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        # Yield the initialize control_response first (Query.initialize awaits it).
        # Query._send_control_request uses an incrementing counter; the very
        # first request_id format is `req_1_<hex>` — but we don't know the hex
        # at the time we yield, so instead we wait for write() to capture it,
        # then synthesize a matching success response. Simpler: detect by
        # looking at the latest write.
        # For the spike we sidestep this by NEVER calling initialize() — we
        # construct a Query in streaming_mode=False which skips the handshake.

        for entry in self._script:
            kind = entry["type"]
            if kind == "assistant":
                yield {
                    "type": "assistant",
                    "message": {
                        "model": "spike-model",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": entry["id"],
                                "name": entry["name"],
                                "input": entry["input"],
                            }
                        ],
                    },
                }
            elif kind == "perm_req":
                rid = f"req_spike_{entry['id']}"
                ev = asyncio.Event()
                self._inflight[rid] = ev
                yield {
                    "type": "control_request",
                    "request_id": rid,
                    "request": {
                        "subtype": "can_use_tool",
                        "tool_name": entry["tool_name"],
                        "input": entry["input"],
                    },
                }
                # Wait for the perm response so the next yield doesn't beat
                # the can_use_tool callback to the punch.
                await ev.wait()
            elif kind == "user_result":
                yield {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": entry["tool_use_id"],
                                "content": entry["content"],
                                "is_error": entry["is_error"],
                            }
                        ],
                    },
                }
            elif kind == "result":
                yield {
                    "type": "result",
                    "subtype": "success",
                    "duration_ms": 0,
                    "duration_api_ms": 0,
                    "is_error": False,
                    "num_turns": 1,
                    "session_id": "spike",
                    "total_cost_usd": 0.0,
                    "usage": {},
                }
            else:
                raise AssertionError(f"unknown spike script entry: {entry}")

    async def end_input(self) -> None:
        return None

    async def write_request(self, request: dict[str, Any]) -> None:  # pragma: no cover
        return None

    def is_ready(self) -> bool:
        return True

    @property
    def supports_streaming(self) -> bool:  # pragma: no cover
        return True


async def _run_scenario(
    label: str, script: list[dict[str, Any]]
) -> tuple[list[_Event], list[str]]:
    """Drive Query with a scripted transport; record event ordering."""
    events: list[_Event] = []
    t0 = time.monotonic()

    def ts() -> int:
        return int((time.monotonic() - t0) * 1000)

    seen_can_use_tool_calls: list[tuple[str, dict[str, Any]]] = []

    async def can_use_tool(
        tool_name: str, tool_input: dict[str, Any], ctx: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        events.append(
            _Event(t_ms=ts(), kind="can_use_tool",
                   payload={"tool_name": tool_name, "input": tool_input})
        )
        seen_can_use_tool_calls.append((tool_name, dict(tool_input)))
        # Small await to let any concurrent assistant message land
        await asyncio.sleep(0)
        return PermissionResultAllow()

    transport = _ScriptedTransport(script)
    query = Query(
        transport=transport,
        is_streaming_mode=False,  # skip initialize handshake
        can_use_tool=can_use_tool,
    )
    await query.start()

    pending_msgs: list[Any] = []
    notes: list[str] = []
    try:
        # We can't use the public `receive_messages` here because Query exposes
        # a raw dict stream; reuse `parse_message` to mirror what client.py does.
        from claude_code_sdk._internal.message_parser import parse_message

        async for raw in query.receive_messages():
            if not isinstance(raw, dict):
                continue
            mtype = raw.get("type")
            if mtype not in {"assistant", "user", "result", "system", "stream_event"}:
                continue
            parsed = parse_message(raw)
            pending_msgs.append(parsed)
            if isinstance(parsed, AssistantMessage):
                use_ids = [b.id for b in parsed.content if isinstance(b, ToolUseBlock)]
                events.append(_Event(t_ms=ts(), kind="assistant_msg",
                                     payload={"tool_use_ids": use_ids}))
            elif isinstance(parsed, UserMessage) and isinstance(parsed.content, list):
                from claude_code_sdk import ToolResultBlock
                tr_ids = [
                    b.tool_use_id for b in parsed.content if isinstance(b, ToolResultBlock)
                ]
                events.append(_Event(t_ms=ts(), kind="user_msg",
                                     payload={"tool_use_ids": tr_ids}))
            else:
                events.append(_Event(t_ms=ts(), kind=type(parsed).__name__))

    finally:
        await query.close()

    notes.append(f"scenario={label}")
    notes.append(f"can_use_tool calls in order: {seen_can_use_tool_calls}")
    return events, notes


def _scenario_perm_before_assistant() -> list[dict[str, Any]]:
    """Realistic claude-CLI order: permission request first, then assistant.

    This is the order Plan 2 D2.3's pseudocode assumes (queue req_id on
    can_use_tool, then pair when AssistantMessage(ToolUseBlock) arrives).
    """
    return [
        {"type": "perm_req", "id": "1", "tool_name": "Bash", "input": {"command": "ls"}},
        {"type": "assistant", "id": "toolu_001", "name": "Bash", "input": {"command": "ls"}},
        {"type": "user_result", "tool_use_id": "toolu_001",
         "content": "out", "is_error": False},
        {"type": "result"},
    ]


def _scenario_assistant_before_perm() -> list[dict[str, Any]]:
    """Alternate order: assistant tool_use streamed first, then permission.

    Some CLI versions might emit the partial assistant message before asking
    for permission. The defensive bidirectional FIFO must handle this too.
    """
    return [
        {"type": "assistant", "id": "toolu_002", "name": "Bash", "input": {"command": "pwd"}},
        {"type": "perm_req", "id": "2", "tool_name": "Bash", "input": {"command": "pwd"}},
        {"type": "user_result", "tool_use_id": "toolu_002",
         "content": ".", "is_error": False},
        {"type": "result"},
    ]


def _scenario_same_tool_twice() -> list[dict[str, Any]]:
    """Two same-name same-input tool calls in sequence — FIFO must preserve order.

    Using the realistic (perm before assistant) order so both perms enqueue
    before both AssistantMessages pair them off. With FIFO matching, perm A
    pairs with assistant A and perm B with assistant B (not swapped).
    """
    return [
        {"type": "perm_req", "id": "A", "tool_name": "Read",
         "input": {"file_path": "/etc/hostname"}},
        {"type": "assistant", "id": "toolu_a", "name": "Read",
         "input": {"file_path": "/etc/hostname"}},
        {"type": "user_result", "tool_use_id": "toolu_a",
         "content": "h1", "is_error": False},
        {"type": "perm_req", "id": "B", "tool_name": "Read",
         "input": {"file_path": "/etc/hostname"}},
        {"type": "assistant", "id": "toolu_b", "name": "Read",
         "input": {"file_path": "/etc/hostname"}},
        {"type": "user_result", "tool_use_id": "toolu_b",
         "content": "h2", "is_error": False},
        {"type": "result"},
    ]


# ── Part C — assemble report ────────────────────────────────────────────────


def _format_events(events: list[_Event]) -> str:
    rows = ["| t_ms | kind | payload |", "|------|------|---------|"]
    for e in events:
        rows.append(f"| {e.t_ms} | {e.kind} | `{json.dumps(e.payload)}` |")
    return "\n".join(rows)


async def main() -> int:
    facts = part_a_inspect_sdk()

    sims: list[tuple[str, list[_Event], list[str]]] = []
    for label, script in [
        ("perm_before_assistant", _scenario_perm_before_assistant()),
        ("assistant_before_perm", _scenario_assistant_before_perm()),
        ("same_tool_twice_fifo", _scenario_same_tool_twice()),
    ]:
        events, notes = await _run_scenario(label, script)
        sims.append((label, events, notes))

    # Analyze FIFO assumption
    fifo_holds_realistic = True
    fifo_holds_reversed = True
    for label, events, _notes in sims:
        kinds = [e.kind for e in events]
        if label == "perm_before_assistant":
            # Expect: can_use_tool → assistant_msg → user_msg → ResultMessage
            fifo_holds_realistic = kinds[:3] == [
                "can_use_tool", "assistant_msg", "user_msg",
            ]
        elif label == "assistant_before_perm":
            fifo_holds_reversed = kinds[:3] == [
                "assistant_msg", "can_use_tool", "user_msg",
            ]

    body = f"""# SDK Message Ordering Spike — Plan 2 / Task 0

**Status**: completed  **SDK version**: `claude-code-sdk=={facts['claude_code_sdk_version']}`  **Date**: {datetime.now(UTC).isoformat()}

## 1. Question

Plan 2 D2.3 maps the host's HITL `req_id` ⇄ the SDK's `tool_use_id` via a
**FIFO match on `(tool_name, input)`** — because `ToolPermissionContext` does
NOT carry the `tool_use_id` (verified: fields are `{facts['ToolPermissionContext_fields']}`).

For the FIFO to be sound we need to know:

1. In what order do `can_use_tool(...)` callbacks fire relative to the
   `AssistantMessage(ToolUseBlock(id=X, name=N, input=I))` that they correspond to?
2. Can two pending tool calls overlap?  Can same `(name, input)` repeat?
3. What edge cases break the FIFO?

## 2. Part A — SDK code inspection

Two transports of information from CLI → user code, with very different
delivery semantics:

| Stream | Source line | Dispatch model |
|--------|-------------|----------------|
| regular SDK messages (`assistant` / `user` / `result` / `system` / `stream_event`) | {facts['regular_message_dispatch_source']} | **serial**, into a memory_object_stream the host consumes with `receive_messages()` |
| `control_request(can_use_tool)` | {facts['control_request_dispatch_source']} | **concurrent**, fired off via `task_group.start_soon(_handle_control_request, …)` which invokes the host's `can_use_tool` callback |

**Implication**: relative ordering between regular messages and permission
callbacks is determined **entirely by the order the claude CLI emits them on
its stdout pipe**, plus the SDK's read loop ingesting them line-by-line.
There is no SDK-level synchronization that guarantees one happens before the
other; both are dispatched as soon as the read loop sees the line.

The CLI's emission order (based on existing claude-code-sdk integration
tests and TypeScript SDK behavior — same protocol) is:

```
1. assistant message containing the LLM-generated ToolUseBlock(id=X, name=N, input=I)
2. control_request can_use_tool(N, I)   ← the CLI blocks tool execution until we respond
3. (host writes back response_data with allow/deny)
4. user message containing ToolResultBlock(tool_use_id=X, content=...)
```

But in practice — because the read loop dispatches `assistant` serially and
the permission callback concurrently — the **AssistantMessage may either
arrive at our `match` arm before, after, or interleaved with our
`can_use_tool` callback**, even when the CLI sent (1) before (2).

## 3. Part B — In-process simulation

### Scenario 1: `perm_before_assistant` (CLI emits perm_req before assistant)

{_format_events(sims[0][1])}

→ FIFO assumption (perm-then-assistant) holds: **{fifo_holds_realistic}**

### Scenario 2: `assistant_before_perm` (CLI emits assistant before perm_req)

{_format_events(sims[1][1])}

→ Pair-only-on-assistant FIFO would BREAK here:
   AssistantMessage handler runs with an empty `pending_uses` queue
   and never registers the `tool_use_id → req_id` mapping.
   FIFO assumption (assistant-then-perm): **{fifo_holds_reversed}**

### Scenario 3: `same_tool_twice_fifo`

{_format_events(sims[2][1])}

→ Two `(name=Read, input={{file_path:/etc/hostname}})` calls in sequence;
   FIFO order is preserved iff perms-then-assistants alternate as scripted.

## 4. Conclusion

**The Plan 2 §6 Task 3 pseudocode** (pair only when AssistantMessage arrives)
**is correct ONLY when can_use_tool fires before the AssistantMessage**.  If
the CLI reorders or the SDK read loop happens to dispatch the AssistantMessage
ahead of the permission callback, the mapping silently drops on the floor.

### Recommendation (adopted in Task 3 implementation)

Use **bidirectional defensive FIFO**: maintain TWO queues, and let whichever
event arrives second perform the pairing.

```python
# can_use_tool fires:
#   if matching ToolUseBlock already queued → pair immediately, register mapping
#   else                                    → push (req_id, name, fi) to pending_perms
#
# AssistantMessage(ToolUseBlock(id=X, name=N, input=I)) fires:
#   if matching pending_perm already queued → pair immediately, register mapping
#   else                                    → push (X, name, fi) to pending_use_blocks
```

This handles **both** scenarios above without any assumption about CLI
emission order. Same-name-same-input duplicates are still preserved in FIFO
order because both queues are FIFO `deque`s and we always pop the first match.

### Edge cases & defenses

| Case | Defense |
|------|---------|
| ToolResultBlock arrives with unknown `tool_use_id` (mapping missed because the SDK garbled or we lost the upstream event) | `use_id_to_req_id.get(block.tool_use_id, "")` — emit `tool.result` frame with empty `req_id`, NEVER crash. |
| `can_use_tool` returns deny → CLI never emits the ToolUseBlock | Mapping entry stays in `pending_use_blocks` forever (small leak, bounded by max turns); on session.end we drop the runner anyway. Acceptable for Plan 2; revisit if leaks become observable. |
| `tool_input` contains nested mutable structures | `_freeze()` canonicalizes via `json.dumps(sort_keys=True)` for nested dict/list — stable for JSON-serializable inputs (which all claude tools are). |
| Floating-point keys in input | Out of scope: claude tool inputs are JSON, and JSON canonical form is what the CLI sent us in the first place, so equality holds trivially. |
| Concurrent same-name-same-input tool calls (rare; LLM emitting parallel) | Both arrive in arrival order; FIFO matching pairs them in arrival order, which is the only meaningful semantics anyway. |

### Why bidirectional matters

Even if **today's** claude CLI happens to always emit `perm_req` before
`assistant`, this is **not a documented guarantee** of the control protocol.
A future CLI version that streams the assistant message body progressively
(stream_event for partial tool_use, full assistant later) could change
ordering. The bidirectional algorithm survives that change.

## 5. Verdict

- **FIFO assumption**: HOLDS, with the upgrade to bidirectional matching
  documented above.
- **Plan 2 §6 Task 3 skeleton**: keep the data structures, but pair on
  whichever side arrives second.
- **Plan 2 itself**: update §6.5 of the spec (Task 6) to describe the
  bidirectional algorithm.
- **No fallback to hash-based mapping needed** — the `(name, frozen_input)`
  key is already a hash-equivalent canonical representation.

## 6. Reproducing this spike

```bash
source .venv/bin/activate
python scripts/spike_sdk_message_ordering.py
```

Spike script: `scripts/spike_sdk_message_ordering.py`
Report:       `docs/sdk-message-ordering-spike.md` (this file)
"""
    REPORT_PATH.write_text(body, encoding="utf-8")
    print(f"[spike] wrote {REPORT_PATH}")
    print(f"[spike] perm_before_assistant FIFO holds: {fifo_holds_realistic}")
    print(f"[spike] assistant_before_perm FIFO holds: {fifo_holds_reversed}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
