"""Coverage for the small dataclass-serialization helpers in client.py."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from claude_code_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from gg_relay.session.client import (
    _serialize_block,
    _serialize_misc,
    _serialize_user,
    make_sdk_runner,
)
from gg_relay.session.executor.inprocess import InProcessExecutor
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import DEFAULT_POLICY
from gg_relay.session.spec import PluginManifest, SessionSpec
from gg_relay.session.transport.protocol import TransportClosed


def test_serialize_block_text() -> None:
    out = _serialize_block(TextBlock(text="hello"))
    assert out == {"type": "text", "text": "hello"}


def test_serialize_block_thinking() -> None:
    out = _serialize_block(ThinkingBlock(thinking="...", signature="sig-abc"))
    assert out == {"type": "thinking", "signature": "sig-abc"}


def test_serialize_block_tool_use() -> None:
    out = _serialize_block(ToolUseBlock(id="tu1", name="Read", input={"x": 1}))
    assert out == {"type": "tool_use", "id": "tu1", "name": "Read", "input": {"x": 1}}


def test_serialize_block_tool_result() -> None:
    out = _serialize_block(
        ToolResultBlock(tool_use_id="tu_x", content="r", is_error=False)
    )
    assert out["type"] == "tool_result"
    assert out["tool_use_id"] == "tu_x"
    assert out["is_error"] is False


def test_serialize_block_unknown_falls_back_to_repr() -> None:
    class _Foo:
        def __repr__(self) -> str:
            return "<foo>"

    out = _serialize_block(_Foo())
    assert out == {"type": "_Foo", "repr": "<foo>"}


def test_serialize_user_str_content() -> None:
    """UserMessage with str content (rare but in SDK type union) — preserved verbatim."""
    msg = UserMessage(content="raw-text-prompt")
    out = _serialize_user(msg)
    assert out["content"] == "raw-text-prompt"
    assert out["type"] == "UserMessage"


def test_serialize_misc_unknown_dataclass() -> None:
    """Future SDK message types we don't recognize go via dataclass fallback."""
    from dataclasses import dataclass

    @dataclass
    class _FutureSdkMsg:
        kind: str
        data: dict[str, Any]

    out = _serialize_misc(_FutureSdkMsg(kind="x", data={"a": 1}))
    assert out == {"type": "_FutureSdkMsg", "kind": "x", "data": {"a": 1}}


def test_serialize_misc_non_dataclass_fallback() -> None:
    class _Plain:
        def __repr__(self) -> str:
            return "<plain>"

    out = _serialize_misc(_Plain())
    assert out == {"type": "_Plain", "repr": "<plain>"}


def test_serialize_misc_system_message() -> None:
    out = _serialize_misc(SystemMessage(subtype="init", data={"a": 1}))
    assert out["type"] == "SystemMessage"
    assert out["subtype"] == "init"


# Runtime coverage for the runner's UserMessage-without-ToolResultBlock branch
# (line 319 in client.py) and the unknown-message fallback (line 326-327).


class _StubBase:
    def __init__(self, options: Any) -> None:
        self.options = options

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def query(self, prompt: str) -> None:
        return None


async def _drain(handle) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    while True:
        try:
            f = await asyncio.wait_for(handle.transport.recv(), timeout=1.0)
        except (TimeoutError, TransportClosed):
            break
        frames.append(dict(f))
        if f["type"] == "session.end":
            break
    return frames


def _spec(tmp_path: Path) -> SessionSpec:
    return SessionSpec(
        prompt="x",
        cwd=tmp_path,
        plugins=PluginManifest(profile="minimal"),
        executor="inprocess",
    )


async def test_user_message_without_tool_results_falls_back_to_msg_chunk(
    tmp_path: Path,
) -> None:
    """UserMessage with only TextBlocks (no ToolResultBlock) → msg.chunk."""

    class _C(_StubBase):
        async def receive_messages(self) -> AsyncIterator[Any]:
            yield UserMessage(content=[TextBlock(text="echo from user")])
            yield ResultMessage(
                subtype="success", duration_ms=0, duration_api_ms=0,
                is_error=False, num_turns=0, session_id="s",
            )

    runner = make_sdk_runner(
        policy=DEFAULT_POLICY,
        coordinator=HITLCoordinator(),
        sdk_factory=lambda opts: _C(opts),
    )
    executor = InProcessExecutor(runner=runner)
    handle = await executor.start(_spec(tmp_path))
    frames = await _drain(handle)
    await executor.stop(handle)

    chunks = [f for f in frames if f["type"] == "msg.chunk"]
    user_chunks = [c for c in chunks if c["data"].get("type") == "UserMessage"]
    assert len(user_chunks) == 1


async def test_user_message_with_str_content_emits_msg_chunk(tmp_path: Path) -> None:
    """UserMessage(content=str) goes via _serialize_user str branch."""

    class _C(_StubBase):
        async def receive_messages(self) -> AsyncIterator[Any]:
            yield UserMessage(content="raw-user-text")
            yield ResultMessage(
                subtype="success", duration_ms=0, duration_api_ms=0,
                is_error=False, num_turns=0, session_id="s",
            )

    runner = make_sdk_runner(
        policy=DEFAULT_POLICY,
        coordinator=HITLCoordinator(),
        sdk_factory=lambda opts: _C(opts),
    )
    executor = InProcessExecutor(runner=runner)
    handle = await executor.start(_spec(tmp_path))
    frames = await _drain(handle)
    await executor.stop(handle)

    user_chunk = next(
        f for f in frames
        if f["type"] == "msg.chunk" and f["data"].get("type") == "UserMessage"
    )
    assert user_chunk["data"]["content"] == "raw-user-text"


async def test_unknown_message_type_falls_back_to_msg_chunk(tmp_path: Path) -> None:
    """A message type the match block doesn't handle goes to the wildcard arm
    and gets serialized via _serialize_misc."""

    class _UnknownMsg:
        def __repr__(self) -> str:
            return "<UnknownMsg payload>"

    class _C(_StubBase):
        async def receive_messages(self) -> AsyncIterator[Any]:
            yield _UnknownMsg()
            yield ResultMessage(
                subtype="success", duration_ms=0, duration_api_ms=0,
                is_error=False, num_turns=0, session_id="s",
            )

    runner = make_sdk_runner(
        policy=DEFAULT_POLICY,
        coordinator=HITLCoordinator(),
        sdk_factory=lambda opts: _C(opts),
    )
    executor = InProcessExecutor(runner=runner)
    handle = await executor.start(_spec(tmp_path))
    frames = await _drain(handle)
    await executor.stop(handle)

    chunks = [f for f in frames if f["type"] == "msg.chunk"]
    unknown_chunk = next(c for c in chunks if "_UnknownMsg" in repr(c["data"]))
    assert "<UnknownMsg payload>" in unknown_chunk["data"]["repr"]


async def test_assistant_message_with_thinking_block(tmp_path: Path) -> None:
    """Covers ThinkingBlock branch in _serialize_block via the runner."""

    class _C(_StubBase):
        async def receive_messages(self) -> AsyncIterator[Any]:
            yield AssistantMessage(
                content=[
                    ThinkingBlock(thinking="planning", signature="sig"),
                    TextBlock(text="result"),
                ],
                model="stub",
            )
            yield ResultMessage(
                subtype="success", duration_ms=0, duration_api_ms=0,
                is_error=False, num_turns=0, session_id="s",
            )

    runner = make_sdk_runner(
        policy=DEFAULT_POLICY,
        coordinator=HITLCoordinator(),
        sdk_factory=lambda opts: _C(opts),
    )
    executor = InProcessExecutor(runner=runner)
    handle = await executor.start(_spec(tmp_path))
    frames = await _drain(handle)
    await executor.stop(handle)

    chunk = next(f for f in frames if f["type"] == "msg.chunk")
    content_types = [b.get("type") for b in chunk["data"]["content"]]
    assert "thinking" in content_types
    assert "text" in content_types
