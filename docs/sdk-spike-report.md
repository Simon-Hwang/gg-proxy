# SDK Spike Report

*Generated: 2026-05-22 07:22:02 UTC*
*claude-code-sdk version: `0.0.25`*

Spec: [`docs/superpowers/specs/2026-05-22-sdk-bootstrap-and-runtime-design.md`](superpowers/specs/2026-05-22-sdk-bootstrap-and-runtime-design.md) §7.2

## Summary

| ID | Status | Detail |
|---|---|---|
| S1.1 | PASS | ClaudeCodeOptions.can_use_tool parameter exists |
| S1.2 | PASS | can_use_tool requires async callable (returns Awaitable) |
| S1.3 | PASS | CanUseTool type alias requires async (Awaitable) |
| S1.4 | PASS | PermissionResultAllow / Deny are dataclasses with expected fields |
| S1.5 | INFO | PermissionResultDeny supports interrupt=True (abort whole session) |
| S1.6 | INFO | PermissionResultAllow supports updated_input (mutate tool args) |
| S2.1 | PASS | ClaudeSDKClient.interrupt() is async public method |
| S3.1 | PASS | HookCallback type alias requires async |
| S3.2 | PASS | ClaudeCodeOptions.hooks supports 6 events: ['PreToolUse', 'PostToolUse', 'UserPromptSubmit', 'Stop', 'SubagentStop', 'PreCompact'] |
| S4.1 | INFO | Transport is abstract with methods: ['close', 'connect', 'end_input', 'is_ready', 'read_messages', 'write'] — future v2 in-process bridge can subclass |
| S5.1 | INFO | ClaudeCodeOptions.env allows secret injection without env-leak via docker inspect |
| S5.2 | INFO | ClaudeCodeOptions.settings can point to per-session settings file |
| B1.1 | PASS | AUTO_ACCEPT path returns PermissionResultAllow without HITL roundtrip |
| B2.1 | PASS | HITL path blocks correctly; decision routed back via Future |
| B3.1 | PASS | HITL reject correctly produces PermissionResultDeny |
| B4.1 | PASS | HookMatcher(matcher='*', hooks=[async fn]) instantiates: HookMatcher(matcher='*', hooks=[<function part_b_mock_can_use_tool.<locals>.stop_hook at 0x7f4f5f8a6fc0>]) |
| B5.1 | PASS | ClaudeCodeOptions wires can_use_tool + hooks + env + cwd cleanly |

## API Surface Snapshot

```json
{
  "can_use_tool_annotation": "collections.abc.Callable[[str, dict[str, typing.Any], claude_code_sdk.types.ToolPermissionContext], collections.abc.Awaitable[claude_code_sdk.types.PermissionResultAllow | claude_code_sdk.types.PermissionResultDeny]] | None",
  "CanUseTool_alias": "collections.abc.Callable[[str, dict[str, typing.Any], claude_code_sdk.types.ToolPermissionContext], collections.abc.Awaitable[claude_code_sdk.types.PermissionResultAllow | claude_code_sdk.types.PermissionResultDeny]]",
  "ClaudeSDKClient_public_methods": [
    "connect",
    "disconnect",
    "get_server_info",
    "interrupt",
    "query",
    "receive_messages",
    "receive_response"
  ],
  "HookCallback_alias": "collections.abc.Callable[[dict[str, typing.Any], str | None, claude_code_sdk.types.HookContext], collections.abc.Awaitable[claude_code_sdk.types.HookJSONOutput]]",
  "hooks_field_annotation": "dict[typing.Union[typing.Literal['PreToolUse'], typing.Literal['PostToolUse'], typing.Literal['UserPromptSubmit'], typing.Literal['Stop'], typing.Literal['SubagentStop'], typing.Literal['PreCompact']], list[claude_code_sdk.types.HookMatcher]] | None",
  "Transport_abstract_methods": [
    "close",
    "connect",
    "end_input",
    "is_ready",
    "read_messages",
    "write"
  ],
  "wired_options_keys": [
    "continue_conversation",
    "cwd",
    "env",
    "debug_stderr",
    "can_use_tool",
    "hooks",
    "include_partial_messages"
  ]
}
```

## Decisions Locked

Based on the spike, the following design decisions in the spec are confirmed safe:

- **§7.1 HITL sync-block flow** — `ClaudeCodeOptions.can_use_tool` natively supports async PreToolUse blocking with `PermissionResultAllow | PermissionResultDeny` return.
- **§6.2 `interrupt` control frame** — `ClaudeSDKClient.interrupt()` is a real public method; control frame can dispatch directly.
- **§7.2 Fallback NOT triggered** — no need for the message-stream-interception fallback; the primary HITL path works as designed.
- **§5.1 Transport extension point** — `claude_code_sdk.Transport` is an abstract base class with 6 abstract methods (`connect`, `read_messages`, `write`, `end_input`, `is_ready`, `close`); future v2 in-process bridge can subclass it directly.

## Additional Findings (not in original spike scope)

- SDK exposes `permission_mode='acceptEdits'` natively — overlaps with our `ToolPolicy.AUTO_ACCEPT_TOOLS = {Edit, Write, MultiEdit, NotebookEdit}` but lacks path scoping. We still need our own `can_use_tool` to add cwd-subtree + dangerous-pattern checks.
- `PermissionResultDeny(interrupt=True)` aborts the entire session — useful for 'reject + stop' HITL decisions.
- `PermissionResultAllow(updated_input=...)` allows the host to **mutate tool args** before the SDK executes — opens future capabilities like 'auto-redact secret in args'.
- `ClaudeCodeOptions.hooks` accepts 6 lifecycle events: `PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Stop`, `SubagentStop`, `PreCompact`. Maps cleanly to our event stream needs (`session.end` ← `Stop`, etc).
- `ClaudeCodeOptions.env` / `extra_args` / `add_dirs` / `mcp_servers` cover the environment-injection needs raised in spec §12.5 secrets question — secrets can be passed through `env` dict at SDK init time, never logged.
