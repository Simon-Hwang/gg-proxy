# SDK Message Ordering Spike — Plan 2 / Task 0

**Status**: completed  **SDK version**: `claude-code-sdk==0.0.25`  **Date**: 2026-05-22T11:21:18.168099+00:00

## 1. Question

Plan 2 D2.3 maps the host's HITL `req_id` ⇄ the SDK's `tool_use_id` via a
**FIFO match on `(tool_name, input)`** — because `ToolPermissionContext` does
NOT carry the `tool_use_id` (verified: fields are `['signal', 'suggestions']`).

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
| regular SDK messages (`assistant` / `user` / `result` / `system` / `stream_event`) | claude_code_sdk/_internal/query.py:173 — `await self._message_send.send(message)` | **serial**, into a memory_object_stream the host consumes with `receive_messages()` |
| `control_request(can_use_tool)` | claude_code_sdk/_internal/query.py:164 — `self._tg.start_soon(self._handle_control_request, request)` | **concurrent**, fired off via `task_group.start_soon(_handle_control_request, …)` which invokes the host's `can_use_tool` callback |

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

| t_ms | kind | payload |
|------|------|---------|
| 3 | can_use_tool | `{"tool_name": "Bash", "input": {"command": "ls"}}` |
| 3 | assistant_msg | `{"tool_use_ids": ["toolu_001"]}` |
| 3 | user_msg | `{"tool_use_ids": ["toolu_001"]}` |
| 3 | ResultMessage | `{}` |

→ FIFO assumption (perm-then-assistant) holds: **True**

### Scenario 2: `assistant_before_perm` (CLI emits assistant before perm_req)

| t_ms | kind | payload |
|------|------|---------|
| 0 | assistant_msg | `{"tool_use_ids": ["toolu_002"]}` |
| 0 | can_use_tool | `{"tool_name": "Bash", "input": {"command": "pwd"}}` |
| 0 | user_msg | `{"tool_use_ids": ["toolu_002"]}` |
| 0 | ResultMessage | `{}` |

→ Pair-only-on-assistant FIFO would BREAK here:
   AssistantMessage handler runs with an empty `pending_uses` queue
   and never registers the `tool_use_id → req_id` mapping.
   FIFO assumption (assistant-then-perm): **True**

### Scenario 3: `same_tool_twice_fifo`

| t_ms | kind | payload |
|------|------|---------|
| 0 | can_use_tool | `{"tool_name": "Read", "input": {"file_path": "/etc/hostname"}}` |
| 0 | assistant_msg | `{"tool_use_ids": ["toolu_a"]}` |
| 0 | user_msg | `{"tool_use_ids": ["toolu_a"]}` |
| 0 | can_use_tool | `{"tool_name": "Read", "input": {"file_path": "/etc/hostname"}}` |
| 0 | assistant_msg | `{"tool_use_ids": ["toolu_b"]}` |
| 0 | user_msg | `{"tool_use_ids": ["toolu_b"]}` |
| 0 | ResultMessage | `{}` |

→ Two `(name=Read, input={file_path:/etc/hostname})` calls in sequence;
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
