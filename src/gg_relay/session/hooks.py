"""Claude Code SDK hook relay.

The SDK invokes hooks inside the active session process. This module turns
those callbacks into regular relay frames so they flow through the same
redaction, persistence, event-bus, and API surfaces as streamed messages.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Mapping
from typing import Any

from claude_code_sdk.types import HookContext, HookJSONOutput, HookMatcher

from gg_relay.redaction import RedactionEngine
from gg_relay.session.frames import make_hook_frame
from gg_relay.session.transport.protocol import SessionTransport

logger = logging.getLogger("gg_relay.session.hooks")

_HOOK_EVENTS = (
    "PreToolUse",
    "PostToolUse",
    "UserPromptSubmit",
    "Stop",
    "SubagentStop",
    "PreCompact",
)


def _snake_event(name: str) -> str:
    return re.sub(r"(?<!^)([A-Z])", r"_\1", name).lower()


def _first_str(mapping: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _stable_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class HookRelay:
    """Build SDK hook callbacks that emit relay hook frames."""

    def __init__(
        self,
        *,
        transport: SessionTransport,
        relay_session_id: str,
        redactor: RedactionEngine,
        seq_base: int = 900_000,
    ) -> None:
        self._transport = transport
        self._relay_session_id = relay_session_id
        self._redactor = redactor
        self._seq = seq_base
        self._lock = asyncio.Lock()

    def make_hook_config(self) -> dict[str, list[HookMatcher]]:
        """Return a ClaudeCodeOptions-compatible hooks mapping."""
        return {
            event: [HookMatcher(matcher=None, hooks=[self._callback_for(event)])]
            for event in _HOOK_EVENTS
        }

    def _callback_for(self, event_type: str) -> Any:
        async def _hook(
            hook_input: dict[str, Any],
            tool_name: str | None,
            context: HookContext,
        ) -> HookJSONOutput:
            try:
                await self._emit(event_type, hook_input, tool_name, context)
            except Exception:
                logger.warning(
                    "failed to relay sdk hook event_type=%s session_id=%s",
                    event_type,
                    self._relay_session_id,
                    exc_info=True,
                )
            return {}

        return _hook

    async def _emit(
        self,
        event_type: str,
        hook_input: dict[str, Any],
        tool_name: str | None,
        context: HookContext,
    ) -> None:
        redacted = self._redactor.redact_dict(hook_input)
        if not isinstance(redacted, dict):
            redacted = {}
        effective_tool = tool_name or _first_str(
            hook_input, "tool_name", "name", "tool"
        )
        sdk_session_id = _first_str(
            hook_input, "session_id", "sdk_session_id"
        ) or getattr(context, "session_id", None)
        tool_use_id = _first_str(
            hook_input, "tool_use_id", "toolUseID", "toolUseId", "id"
        )
        parent_tool_use_id = _first_str(
            hook_input,
            "parent_tool_use_id",
            "parentToolUseID",
            "parentToolUseId",
        )
        async with self._lock:
            seq = self._seq
            self._seq += 1
        frame = make_hook_frame(
            seq,
            f"hook.{_snake_event(event_type)}",
            event_type=event_type,
            tool_name=effective_tool,
            tool_use_id=tool_use_id,
            parent_tool_use_id=parent_tool_use_id,
            sdk_session_id=sdk_session_id,
            input_hash=_stable_hash(redacted),
            input_redacted=redacted,
        )
        await self._transport.send(frame)  # type: ignore[arg-type]
