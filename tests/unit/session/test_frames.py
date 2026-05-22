"""Tests for frames.py typed builders (Plan 2 Task 2)."""
from __future__ import annotations

import re
from pathlib import Path

from gg_relay.session.frames import (
    make_error,
    make_install_done,
    make_install_error,
    make_msg_chunk,
    make_session_end,
    make_tool_request,
    make_tool_result,
)
from gg_relay.session.plugins import InstallReport

ISO_REGEX = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z"
)


def _common_assertions(frame: dict, expected_type: str, seq: int) -> None:
    assert frame["v"] == 1
    assert frame["type"] == expected_type
    assert frame["seq"] == seq
    assert ISO_REGEX.match(frame["ts"]), f"bad ts: {frame['ts']!r}"


def test_make_msg_chunk_shape() -> None:
    frame = make_msg_chunk(seq=5, data={"text": "hi"})
    _common_assertions(dict(frame), "msg.chunk", 5)
    assert frame["data"] == {"text": "hi"}


def test_make_tool_request_shape() -> None:
    frame = make_tool_request(
        seq=10, req_id="r-abc", tool="Bash", args={"command": "ls"}
    )
    _common_assertions(dict(frame), "tool.request", 10)
    assert frame["req_id"] == "r-abc"
    assert frame["tool"] == "Bash"
    assert frame["args"] == {"command": "ls"}


def test_make_tool_result_shape() -> None:
    frame = make_tool_result(
        seq=11, req_id="r-abc", ok=True, result={"stdout": "."}
    )
    _common_assertions(dict(frame), "tool.result", 11)
    assert frame["req_id"] == "r-abc"
    assert frame["ok"] is True
    assert frame["result"] == {"stdout": "."}


def test_make_tool_result_failure() -> None:
    frame = make_tool_result(
        seq=12, req_id="r-x", ok=False, result={"error": "denied"}
    )
    assert frame["ok"] is False


def test_make_session_end_shape() -> None:
    frame = make_session_end(
        seq=99,
        status="completed",
        tokens={"input_tokens": 100, "output_tokens": 50},
        cost_usd=0.0012,
    )
    _common_assertions(dict(frame), "session.end", 99)
    assert frame["status"] == "completed"
    assert frame["tokens"] == {"input_tokens": 100, "output_tokens": 50}
    assert frame["cost_usd"] == 0.0012


def test_make_error_shape() -> None:
    frame = make_error(seq=20, code="RuntimeError", message="boom")
    _common_assertions(dict(frame), "error", 20)
    assert frame["code"] == "RuntimeError"
    assert frame["message"] == "boom"
    assert "traceback" not in frame


def test_make_error_with_traceback() -> None:
    frame = make_error(
        seq=21, code="RuntimeError", message="boom", traceback_="line1\nline2"
    )
    assert frame["traceback"] == "line1\nline2"


def test_make_install_done_shape() -> None:
    report = InstallReport(
        schema_version="gg.install.v1",
        profile_id="minimal",
        selected_modules=("rules-core", "skills-core"),
        included_components=(),
        excluded_components=(),
        install_root=Path("/tmp/x/.claude"),
        installed_at="2026-05-22T00:00:00Z",
        duration_ms=842,
    )
    frame = make_install_done(seq=0, report=report)
    _common_assertions(dict(frame), "install.done", 0)
    assert frame["profile_id"] == "minimal"
    assert frame["modules"] == ["rules-core", "skills-core"]
    assert frame["duration_ms"] == 842
    assert frame["install_root"] == "/tmp/x/.claude"


def test_make_install_done_no_profile() -> None:
    report = InstallReport(
        schema_version="gg.install.v1",
        profile_id=None,
        selected_modules=("m1",),
        included_components=(),
        excluded_components=(),
        install_root=Path("/tmp/y"),
        installed_at="2026-05-22T00:00:00Z",
        duration_ms=1,
    )
    frame = make_install_done(seq=0, report=report)
    assert frame["profile_id"] is None
    assert frame["modules"] == ["m1"]


def test_make_install_error_shape() -> None:
    frame = make_install_error(
        seq=0, code="PluginInstallError", message="install.sh exit 2",
        stderr_tail="profile not found",
    )
    _common_assertions(dict(frame), "install.error", 0)
    assert frame["code"] == "PluginInstallError"
    assert frame["message"] == "install.sh exit 2"
    assert frame["stderr_tail"] == "profile not found"


def test_make_install_error_truncates_long_stderr() -> None:
    tail = "x" * 5000
    frame = make_install_error(seq=0, code="C", message="m", stderr_tail=tail)
    assert len(frame["stderr_tail"]) == 2048
    assert frame["stderr_tail"] == "x" * 2048


def test_make_install_error_default_stderr_empty() -> None:
    frame = make_install_error(seq=0, code="C", message="m")
    assert frame["stderr_tail"] == ""


def test_make_tool_result_omits_optional_when_none() -> None:
    """Result dict is always present (even empty); error key never appears."""
    frame = make_tool_result(seq=1, req_id="x", ok=True, result={})
    assert "result" in frame
    assert frame["result"] == {}
