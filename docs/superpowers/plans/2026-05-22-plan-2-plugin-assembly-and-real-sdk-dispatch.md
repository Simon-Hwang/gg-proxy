# Plan 2 — Plugin Assembly + Real SDK Dataclass Dispatch

**作者**: gg-relay  **创建**: 2026-05-22  **状态**: ✅ Decisions locked, ready to execute

## 1. Goal

Plan 1 walking skeleton（commit `d9d6765`）已经把 in-process executor + stub SDK 跑通。本 Plan 升级到 **"能跑真 SDK + 能装真 gg-plugins"**，仍然 in-process（不引入 Docker / API / persistence）。

具体交付：

1. **PluginAssembler** —— 把 `PluginManifest.to_install_argv()` 真正跑 `gg-plugins/install.sh`，读 `install-state.json` 产 `InstallReport`
2. **真 SDK dataclass dispatch** —— `client.py` 从当前 dict-stub-friendly 切换到 `match` SDK 真 dataclass (`AssistantMessage` / `UserMessage` 含 `ToolResultBlock` / `ResultMessage` / `SystemMessage` / `StreamEvent`)
3. **`tool_use_id ↔ req_id` FIFO 映射** —— SDK 不通过 `ToolPermissionContext` 传 `tool_use_id`（已 spike 验证），改用 FIFO 队列在 `AssistantMessage(ToolUseBlock)` 时回填映射
4. **Typed frame builders** —— 独立函数 `make_xxx(...)`，替代当前 5× `cast(EventFrame, ...)`
5. **`install.done` / `install.error` 帧** —— 接上 Plan 1 已定义但未 emit 的事件
6. **真 API 冒烟测试** —— 1 个 `requires_api_key` 测试，CI 跳过

## 2. Scope

### In
- `src/gg_relay/session/plugins/{__init__.py, protocol.py, install_shell.py}` — assembler
- `src/gg_relay/session/frames.py` — typed frame builders
- `src/gg_relay/session/client.py` — 重写 receive_messages 派发 + FIFO 映射 + install 帧 emission
- `src/gg_relay/session/spec.py` — `to_install_argv` 暂删 `--json`
- `tests/unit/session/test_plugins_assembler.py` / `test_frames.py` / `test_client_dispatch.py`
- `tests/integration/test_real_sdk_dispatch.py` / `test_real_api_smoke.py` / `test_assembler_e2e.py`
- `docs/sdk-message-ordering-spike.md` — spike 结果记录

### Out
- Docker / wire transport — Plan 3
- HTTP API / persistence / dashboard / OTel / IM — Plan 4
- `gg-plugins/install.sh` 上游修改（如果发现 `--json` flag 该实现可以另开 PR）

## 3. Dependencies
- Plan 1 已合入 main（commit `d9d6765`）
- `gg-plugins` repo 在 `/data/workspace/github/gg-plugins`
- `claude-code-sdk` Python SDK 已在 `.venv` 安装
- `@anthropic-ai/claude-code` CLI 已在 host（`/root/.nvm/versions/node/v22.22.0/bin/claude` v2.1.133）
- Task 7 需要 `ANTHROPIC_API_KEY`（本地）

## 4. Locked Decisions

| ID | 决策 | 终值 |
|---|---|---|
| D2.1 | install.sh 调用时机 | `executor.start()` 之前，由 SessionManager / handler 调 `assembler.prepare(spec, install_dir=...)` |
| D2.2 | SDK dispatch | dataclass-only，stub 同步升级 yield 真 dataclass |
| D2.3 | `tool_use_id ↔ req_id` 映射 | **FIFO 队列**（详见 Task 3） |
| D2.4 | install.sh 调用方式 | `asyncio.create_subprocess_exec(*argv, stdout=PIPE, stderr=PIPE)` |
| D2.5 | 安装失败 | raise `PluginInstallError` → `executor.start()` 异常 → handler 看到 |
| D2.6 | frame builders | 独立函数 `make_msg_chunk(...)` 等 |
| D2.7 | 真 API 冒烟 | 加 1 个 `@pytest.mark.requires_api_key` 测试 |
| D2.8 | gg-plugins 路径 | env `GG_PLUGINS_HOME` 必填 |
| 新增 | `to_install_argv` `--json` flag | 暂删（gg-plugins 该 flag 当前未生效） |
| 新增 | `InstallReport` 来源 | 读 `<install_dir>/.claude/gg/install-state.json` |

## 5. Module Layout

```
src/gg_relay/session/
├── plugins/                       # NEW
│   ├── __init__.py                # 重导出 PluginAssembler / InstallShellAssembler / InstallReport / PluginInstallError
│   ├── protocol.py                # PluginAssembler Protocol + InstallReport dataclass + PluginInstallError exception
│   └── install_shell.py           # InstallShellAssembler
├── frames.py                      # NEW: typed builders
├── client.py                      # MODIFIED: dataclass dispatch + FIFO + install emission
├── spec.py                        # MODIFIED: to_install_argv 删 --json
└── ...

tests/
├── unit/session/
│   ├── test_plugins_assembler.py  # NEW
│   ├── test_frames.py             # NEW
│   └── test_client_dispatch.py    # NEW: FIFO + dataclass branch coverage
└── integration/
    ├── test_real_sdk_dispatch.py  # NEW: dataclass-yielding stubs
    ├── test_real_api_smoke.py     # NEW: @requires_api_key
    └── test_assembler_e2e.py      # NEW: 真跑 install.sh

scripts/
└── spike_sdk_message_ordering.py  # NEW (Task 0)

docs/
└── sdk-message-ordering-spike.md  # NEW (Task 0 output)
```

## 6. Task Breakdown

### Task 0 — SDK message ordering spike

**Goal**: 验证 Plan 2 §4 D2.3 FIFO 假设站得住，即 SDK 在同一 control loop 中按以下顺序投递：
1. `AssistantMessage` 含 `ToolUseBlock(id=X, name=N, input=I)` 通过 `receive_messages()` yield
2. `can_use_tool(N, I, ToolPermissionContext)` 被调用（与 `ToolUseBlock` 一一对应）
3. host 决定后，`UserMessage` 含 `ToolResultBlock(tool_use_id=X, ...)` 通过 `receive_messages()` yield

**Approach**:
1. 写 `scripts/spike_sdk_message_ordering.py`
2. 用真 `claude-code-sdk` + `ANTHROPIC_API_KEY`（如可用）或者直接 inspect SDK 内部 `_handle_control_request`（query.py:187）的代码路径并记录推论
3. 让 prompt 简单触发一次工具调用（如 "Read /etc/hostname"），记录 `(timestamp, event_type, content)` 序列
4. 出 `docs/sdk-message-ordering-spike.md`，至少包含：
   - 完整事件序列样本
   - 是否一一对应（含并发 tool call 场景）
   - FIFO 假设结论（hold / break / 替代方案）

**DOD**:
- spike 报告写好
- 若 FIFO 假设 break：Task 3 设计文档同步更新（fallback 到 `tool_input` canonical hash + LIFO）

**Tests**: spike 不写自动化测试，但 spike 脚本要 `python scripts/spike_sdk_message_ordering.py` 可运行

### Task 1 — `PluginAssembler` Protocol + `InstallShellAssembler` impl

**Files**: `plugins/protocol.py`, `plugins/install_shell.py`, `plugins/__init__.py`, `tests/unit/session/test_plugins_assembler.py`

**Skeleton**:

```python
# plugins/protocol.py
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from gg_relay.session.spec import SessionSpec


@dataclass(frozen=True, slots=True)
class InstallReport:
    """Parsed from <install_dir>/.claude/gg/install-state.json (gg.install.v1)."""
    schema_version: str               # "gg.install.v1"
    profile_id: str | None            # selected profile, may be None for module/skill-only
    selected_modules: tuple[str, ...]
    included_components: tuple[str, ...]
    excluded_components: tuple[str, ...]
    install_root: Path                # absolute Path inside install_dir
    installed_at: str                 # ISO 8601 from state file
    duration_ms: int                  # measured by assembler


class PluginInstallError(RuntimeError):
    def __init__(self, *, returncode: int, stderr: str, argv: tuple[str, ...]) -> None:
        super().__init__(f"install.sh exit {returncode}: {stderr[:512]}")
        self.returncode = returncode
        self.stderr = stderr
        self.argv = argv


@runtime_checkable
class PluginAssembler(Protocol):
    async def prepare(self, spec: SessionSpec, *, install_dir: Path) -> InstallReport: ...
```

```python
# plugins/install_shell.py
import asyncio
import json
import time
from asyncio.subprocess import PIPE
from pathlib import Path

from gg_relay.session.plugins.protocol import (
    InstallReport, PluginInstallError, SessionSpec,
)


class InstallShellAssembler:
    """Concrete PluginAssembler that shells out to gg-plugins/install.sh."""

    def __init__(self, plugins_home: Path) -> None:
        if not (plugins_home / "install.sh").is_file():
            raise FileNotFoundError(f"install.sh not found under {plugins_home}")
        self._home = plugins_home

    async def prepare(self, spec: SessionSpec, *, install_dir: Path) -> InstallReport:
        install_dir.mkdir(parents=True, exist_ok=True)
        argv = (str(self._home / "install.sh"), *spec.plugins.to_install_argv(home_dir=str(install_dir)))
        env_overrides = dict(spec.plugins.extra_env)
        t0 = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=PIPE, stderr=PIPE, cwd=str(self._home),
        )
        stdout, stderr = await proc.communicate()
        duration_ms = int((time.monotonic() - t0) * 1000)
        if proc.returncode != 0:
            raise PluginInstallError(
                returncode=proc.returncode or -1,
                stderr=stderr.decode("utf-8", errors="replace"),
                argv=argv,
            )
        state_path = install_dir / ".claude" / "gg" / "install-state.json"
        if not state_path.is_file():
            raise PluginInstallError(
                returncode=0,
                stderr=f"install.sh exit 0 but {state_path} missing",
                argv=argv,
            )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        return InstallReport(
            schema_version=state["schemaVersion"],
            profile_id=state.get("profileId"),
            selected_modules=tuple(state.get("selectedModules", ())),
            included_components=tuple(state.get("includedComponents", ())),
            excluded_components=tuple(state.get("excludedComponents", ())),
            install_root=Path(state["installRoot"]),
            installed_at=state["installedAt"],
            duration_ms=duration_ms,
        )
```

**Tests** (8):
1. `test_init_raises_on_missing_install_sh`
2. `test_prepare_invokes_install_sh_with_correct_argv` — mock `create_subprocess_exec`
3. `test_prepare_returns_report_on_success` — mock subprocess + write fixture `install-state.json`
4. `test_prepare_raises_on_nonzero_exit` — mock subprocess exit=1
5. `test_prepare_raises_when_state_file_missing` — exit 0 but no state file
6. `test_profile_only_manifest_argv` — `PluginManifest(profile="python")` → argv 含 `--profile python`
7. `test_skills_modules_combined_argv` — 多字段组合
8. `test_extra_env_propagated_to_subprocess` — `spec.plugins.extra_env` 传入

**DOD**:
- 全 8 tests 绿
- mypy --strict 0 error
- ruff 0 warning

### Task 2 — `frames.py` typed builders

**Files**: `src/gg_relay/session/frames.py`, `tests/unit/session/test_frames.py`

**Skeleton**:

```python
# frames.py
from __future__ import annotations
from datetime import UTC, datetime
from typing import Any, Literal, cast

from gg_relay.session.plugins.protocol import InstallReport
from gg_relay.session.transport.protocol import (
    ErrorFrame, EventFrame, MsgChunkFrame, SessionEndFrame,
    ToolRequestFrame, ToolResultFrame,
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _envelope(seq: int, type_: str, **rest: Any) -> dict[str, Any]:
    return {"v": 1, "type": type_, "seq": seq, "ts": _now_iso(), **rest}


def make_msg_chunk(seq: int, data: dict[str, Any]) -> MsgChunkFrame:
    return cast(MsgChunkFrame, _envelope(seq, "msg.chunk", data=data))


def make_tool_request(seq: int, req_id: str, tool: str, args: dict[str, Any]) -> ToolRequestFrame:
    return cast(ToolRequestFrame, _envelope(seq, "tool.request", req_id=req_id, tool=tool, args=args))


def make_tool_result(seq: int, req_id: str, ok: bool, result: dict[str, Any]) -> ToolResultFrame:
    return cast(ToolResultFrame, _envelope(seq, "tool.result", req_id=req_id, ok=ok, result=result))


def make_session_end(
    seq: int, status: Literal["completed", "failed", "cancelled"], *,
    tokens: dict[str, Any], cost_usd: float,
) -> SessionEndFrame:
    return cast(SessionEndFrame, _envelope(seq, "session.end", status=status, tokens=tokens, cost_usd=cost_usd))


def make_error(seq: int, code: str, message: str, *, traceback_: str | None = None) -> ErrorFrame:
    payload: dict[str, Any] = {"code": code, "message": message}
    if traceback_ is not None:
        payload["traceback"] = traceback_
    return cast(ErrorFrame, _envelope(seq, "error", **payload))


def make_install_done(seq: int, report: InstallReport) -> EventFrame:
    return cast(EventFrame, _envelope(seq, "install.done",
        profile_id=report.profile_id, modules=list(report.selected_modules),
        duration_ms=report.duration_ms, install_root=str(report.install_root)))


def make_install_error(seq: int, code: str, message: str, *, stderr_tail: str = "") -> EventFrame:
    return cast(EventFrame, _envelope(seq, "install.error",
        code=code, message=message, stderr_tail=stderr_tail[-2048:]))
```

**Tests** (8): one per builder, assert shape (`type`, `seq`, `ts`, payload fields) + types via runtime introspection.

**Note**: `install.done` / `install.error` 还不在 `EventFrame` 联合体里。Task 2 同步把它们加进 `transport/protocol.py`：

```python
class InstallDoneFrame(_BaseFrame):
    type: Literal["install.done"]
    profile_id: NotRequired[str]
    modules: list[str]
    duration_ms: int
    install_root: str

class InstallErrorFrame(_BaseFrame):
    type: Literal["install.error"]
    code: str
    message: str
    stderr_tail: NotRequired[str]

EventFrame = (
    MsgChunkFrame | ToolRequestFrame | ToolResultFrame | SessionEndFrame
    | ErrorFrame | InstallDoneFrame | InstallErrorFrame
)
```

**DOD**: 8 tests + protocol.py 同步 + `mypy --strict`.

### Task 3 — `client.py` 真 SDK dataclass dispatch + FIFO 映射

**Files**: `src/gg_relay/session/client.py`（重写 dispatch loop + can_use_tool）, `tests/unit/session/test_client_dispatch.py`

**Key change (Task 0 spike upgrade — bidirectional FIFO)**: 维护 **两个**
deque：`pending_perms`（`can_use_tool` 已收到、还没看到 `ToolUseBlock`）和
`pending_use_blocks`（`ToolUseBlock` 已收到、还没看到 `can_use_tool`）。
任一事件 fire 时，先尝试在对侧队列里按 `(name, frozen_input)` FIFO 匹配；
匹配到就登记 `use_id → req_id`，匹配不到就把自己 push 到本侧队列等待。
spike 报告：`docs/sdk-message-ordering-spike.md`。

**Skeleton**（关键部分）:

```python
from collections import deque
from claude_code_sdk import (
    AssistantMessage, UserMessage, SystemMessage, ResultMessage, StreamEvent,
    TextBlock, ToolUseBlock, ToolResultBlock,
)

# ... existing imports + factory args ...

async def runner(transport: InMemoryTransport, spec: SessionSpec) -> None:
    seq = 0
    # FIFO of (req_id, tool_name, frozen_input) waiting to be paired with ToolUseBlock
    pending_uses: deque[tuple[str, str, frozenset[tuple[str, Any]]]] = deque()
    # tool_use_id (from AssistantMessage) → req_id (from can_use_tool)
    use_id_to_req_id: dict[str, str] = {}

    def _freeze(d: dict[str, Any]) -> frozenset[tuple[str, Any]]:
        # canonical, hashable representation; nested dicts/lists turned into json string
        return frozenset((k, json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v)
                         for k, v in d.items())

    async def can_use_tool(tool_name, tool_input, context):
        nonlocal seq
        d = policy.decide(tool_name, tool_input, spec.cwd)
        if d == Decision.ACCEPT:
            return PermissionResultAllow()
        if d == Decision.DENY:
            return PermissionResultDeny(message=f"policy denied {tool_name}")
        req_id = f"r-{uuid.uuid4().hex[:12]}"
        pending_uses.append((req_id, tool_name, _freeze(tool_input)))
        seq += 1
        await transport.send(make_tool_request(seq, req_id, tool_name, tool_input))
        decision = await coordinator.request(req_id, tool=tool_name, args=tool_input)
        if decision == "accept":
            return PermissionResultAllow()
        return PermissionResultDeny(message="HITL rejected")

    # ... options + client = sdk_factory(options) inside try ...

    async for msg in client.receive_messages():
        seq += 1
        match msg:
            case ResultMessage():
                seq += 1
                await transport.send(make_session_end(
                    seq, "completed",
                    tokens=dict(msg.usage) if msg.usage else {},
                    cost_usd=msg.total_cost_usd or 0.0,
                ))
                break
            case AssistantMessage(content=blocks):
                # Pair ToolUseBlocks with pending_uses by (name, input) FIFO
                for block in blocks:
                    if isinstance(block, ToolUseBlock):
                        key = (block.name, _freeze(block.input))
                        for idx, (rid, name, fi) in enumerate(pending_uses):
                            if (name, fi) == key:
                                use_id_to_req_id[block.id] = rid
                                del pending_uses[idx]
                                break
                await transport.send(make_msg_chunk(seq, _serialize_assistant(msg)))
            case UserMessage(content=blocks) if isinstance(blocks, list):
                for block in blocks:
                    if isinstance(block, ToolResultBlock):
                        req_id = use_id_to_req_id.pop(block.tool_use_id, "")
                        seq += 1
                        await transport.send(make_tool_result(
                            seq, req_id=req_id,
                            ok=not (block.is_error or False),
                            result=_serialize_tool_result(block),
                        ))
            case SystemMessage() | StreamEvent():
                await transport.send(make_msg_chunk(seq, _serialize_misc(msg)))
            case _:
                await transport.send(make_msg_chunk(seq, {"repr": repr(msg)}))
```

Helpers:
```python
def _serialize_assistant(m: AssistantMessage) -> dict[str, Any]: ...
def _serialize_tool_result(b: ToolResultBlock) -> dict[str, Any]: ...
def _serialize_misc(m: Any) -> dict[str, Any]: ...
```

**Tests** (10):
1. `test_result_message_emits_session_end_with_tokens_and_cost`
2. `test_assistant_message_emits_msg_chunk`
3. `test_user_message_with_tool_result_emits_tool_result_with_mapped_req_id`
4. `test_fifo_mapping_single_call` — 1 can_use_tool → 1 ToolUseBlock → 1 ToolResultBlock，req_id 正确穿透
5. `test_fifo_mapping_two_sequential_calls` — 2 个串行 tool call，req_id 正确穿透
6. `test_fifo_mapping_same_name_same_input_twice` — 同 tool 同参数两次，按 FIFO 顺序匹配
7. `test_fifo_mapping_unknown_tool_use_id_yields_empty_req_id` — defensive
8. `test_system_and_stream_messages_emit_msg_chunk`
9. `test_runner_exception_emits_error_frame` — 已 Plan 1 覆盖，但回归
10. `test_cancellation_does_not_emit_error_frame` — 已 Plan 1 覆盖，但回归

**DOD**:
- 10 tests 绿（含 6 个新 FIFO 测试 + 4 个 dispatch 分支）
- 所有现有 Plan 1 测试仍绿（dataclass dispatch 也要兼容 stub 升级）
- mypy --strict + ruff 全清

### Task 4 — `install.done` / `install.error` 帧 emission

**Files**: `client.py`（runner 加 `install_report: InstallReport | None` 参数）

D2.1 决定 install 在 `executor.start()` 之前由 SessionManager 调；所以 runner 进来时 install **已经做完**，runner 只负责把 `InstallReport` 转成 `install.done` 帧 emit 出去。

**Change**: `make_sdk_runner(*, policy, coordinator, install_report=None, sdk_factory=...)` — 如果 `install_report` 传入，runner 第一个 frame 就是 `install.done`。

**Tests** (3):
1. `test_install_report_emits_install_done_first` — 第 0 帧是 install.done
2. `test_no_install_report_skips_install_done` — backward compat
3. `test_install_done_payload_matches_report`

### Task 5 — `make_sdk_runner` refactor 使用 frame builders

把 `client.py` 中 5× `cast(EventFrame, _envelope(...))` 全部替换成 `make_xxx(...)`。删 `_envelope` 私有 helper（移到 `frames.py` 作内部 helper）。

无新 test（regression 由 Task 3 + Plan 1 现有覆盖）。

**DOD**: `client.py` 全无 `cast` + 全无 `_envelope`。

### Task 6 — Spec sync

**Files**: `docs/superpowers/specs/2026-05-22-sdk-bootstrap-and-runtime-design.md`

- §4.2 PluginManifest: 标记 `to_install_argv` 暂删 `--json` 的理由
- §4.6 新增 PluginAssembler 子节
- §6 frames: 加 install.done / install.error 描述
- §6.5 新增"Tool use ID ↔ Req ID FIFO 映射"子节，包含 spike 引用 + FIFO 算法描述 + 边界情况

### Task 7 — Integration test: dataclass-yielding stub

**File**: `tests/integration/test_real_sdk_dispatch.py`

构造 stub clients yield REAL dataclass 实例（不再用 dict）：

```python
async def stub_receive_messages():
    yield AssistantMessage(
        content=[
            TextBlock(text="I'll run a tool."),
            ToolUseBlock(id="toolu_123", name="Read", input={"file_path": "/etc/hostname"}),
        ],
        model="claude-sonnet-4-7", parent_tool_use_id=None,
    )
    yield UserMessage(
        content=[ToolResultBlock(tool_use_id="toolu_123", content="myhost", is_error=False)],
        parent_tool_use_id=None,
    )
    yield ResultMessage(
        subtype="success", duration_ms=1200, duration_api_ms=1100, is_error=False,
        num_turns=1, session_id="sess_abc",
        total_cost_usd=0.0012, usage={"input_tokens": 100, "output_tokens": 50},
    )
```

**Tests** (5):
1. `test_dataclass_dispatch_basic_round_trip`
2. `test_dataclass_tool_use_to_result_id_mapping`
3. `test_dataclass_result_message_tokens_and_cost_propagate`
4. `test_dataclass_concurrent_same_tool_fifo_ordering` — 同 turn 两个同名 tool call
5. `test_dataclass_unmapped_tool_result_empty_req_id`

### Task 8 — Integration test: real API smoke + assembler e2e

**Files**: `tests/integration/test_real_api_smoke.py`, `tests/integration/test_assembler_e2e.py`

```python
# test_real_api_smoke.py
@pytest.mark.requires_api_key
@pytest.mark.requires_sdk
async def test_real_api_minimal_round_trip(tmp_path):
    """Hit Anthropic API once. Requires ANTHROPIC_API_KEY + claude CLI on PATH."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")
    policy = ToolPolicy()
    coordinator = HITLCoordinator()
    runner = make_sdk_runner(policy=policy, coordinator=coordinator)
    transport = InMemoryTransport()
    executor = InProcessExecutor(runner=runner)
    spec = SessionSpec(
        prompt="Reply with exactly the single word: OK",
        cwd=tmp_path, plugins=PluginManifest(profile="minimal"),
        executor="inprocess",
    )
    handle = await executor.start(spec)
    frames = []
    while True:
        try:
            f = await asyncio.wait_for(handle.transport.recv(), timeout=30)
            frames.append(f)
            if f["type"] == "session.end":
                break
        except TransportClosed:
            break
    await executor.stop(handle)
    assert any(f["type"] == "msg.chunk" for f in frames)
    end = frames[-1]
    assert end["type"] == "session.end" and end["status"] == "completed"
    assert end["tokens"].get("input_tokens", 0) > 0
```

```python
# test_assembler_e2e.py
@pytest.mark.requires_sdk
async def test_install_minimal_profile_e2e(tmp_path):
    plugins_home = Path(os.environ.get("GG_PLUGINS_HOME", "/data/workspace/github/gg-plugins"))
    if not (plugins_home / "install.sh").exists():
        pytest.skip("gg-plugins not at GG_PLUGINS_HOME")
    assembler = InstallShellAssembler(plugins_home=plugins_home)
    spec = SessionSpec(prompt="x", cwd=tmp_path,
                       plugins=PluginManifest(profile="minimal"), executor="inprocess")
    report = await assembler.prepare(spec, install_dir=tmp_path / "home")
    assert report.schema_version == "gg.install.v1"
    assert report.profile_id == "minimal"
    assert "rules-core" in report.selected_modules
    assert (tmp_path / "home" / ".claude" / "gg" / "install-state.json").exists()
```

### Task 9 — Coverage + README + final commit

- `pytest tests/ --cov=gg_relay --cov-fail-under=90` 绿（Plan 2 后阈值上调到 90 from 80）
- `pytest tests/ -m "not requires_api_key" -v` CI 路径全绿
- README 加 "Plan 2: Plugin assembly + real SDK" 段（含 `GG_PLUGINS_HOME` 配置 + `pip install gg-relay[sdk]` 提示）
- spec sync（Task 6 已做）
- `examples/walking_skeleton_demo.py` 仍能跑（注意 stub 升级到 dataclass 后）
- 最终 commit + branch squash merge

## 7. Test Strategy

| 层 | 数量 | 覆盖 |
|---|---|---|
| Unit: assembler | 8 | argv 拼装、subprocess mock、state 文件解析 |
| Unit: frames | 8 | 每个 builder 形状 + 类型 |
| Unit: client.py dispatch | 10 | 6 FIFO + 4 dispatch 分支 |
| Unit: install report emission | 3 | install.done 帧 |
| Integration: dataclass stub | 5 | end-to-end with 真 dataclass |
| Integration: real API | 1 | requires_api_key |
| Integration: assembler e2e | 1 | requires_sdk + GG_PLUGINS_HOME |
| **Total 新增** | **36** | + 68 Plan 1 = **104** |

## 8. Risks

- **R1**: Task 0 spike 推翻 FIFO 假设 → 切换到 hash-based 映射，Task 3 重写
- **R2**: `--json` flag 暂删后 PluginManifest 没传 `--json`，是否影响 install.sh 默认行为？已 verify：install.sh 不会因没 `--json` 失败，state 文件总会写
- **R3**: 真 API 冒烟测试要钱（每次 ~$0.0012 + token），但只在 `requires_api_key` marker 下跑
- **R4**: `_freeze()` 对 list/dict 用 `json.dumps(sort_keys=True)` 是稳定的，但浮点数边界值要小心（cookie cutter）
- **R5**: stub 升级到 dataclass 后老测试可能挂 → Task 7 同步修

## 9. Deferred

- `--json` flag 上游 PR（gg-plugins repo）
- install 缓存层
- per-session hitl_policy override（Plan 4）

## 10. Self-Review checklist

- [x] Task 0 spike 完成 + 报告写好 → `docs/sdk-message-ordering-spike.md`（bidirectional FIFO）
- [x] Task 1 — PluginAssembler + InstallShellAssembler — done (10 unit tests)
- [x] Task 2 — frames.py 8 builders + protocol.py 联合 — done (13 unit tests)
- [x] Task 3 — client.py SDK dataclass dispatch + bidirectional FIFO — done (15 unit tests; demo + walking_skeleton upgraded to yield dataclasses)
- [x] Task 4 — install.done emission — done (3 unit tests)
- [x] Task 5 — client.py refactor 用 frame builders — done implicitly as part of Task 3 rewrite (no `cast(EventFrame, _envelope(...))` calls remain; all sends go through `make_*` builders)
- [x] Task 6 — spec sync — done (§4.2 `--json` note + §4.6 PluginAssembler full def + §5.3 摘要 + §6.2 install.done/install.error 修订 + §6.5 FIFO mapping)
- [x] Task 7 — dataclass-yielding stub + real_api_smoke — done (5 stub tests green + 1 requires_api_key test that skips cleanly without ANTHROPIC_API_KEY)
- [x] Task 8 — assembler e2e — done (1 @requires_sdk test, skips cleanly when GG_PLUGINS_HOME unset; passes against real /data/workspace/github/gg-plugins)
- [x] Task 9 — coverage + README + final commit — done (`--cov-fail-under=90`, README adds Plan 2 section)
- [x] 每 task TDD
- [x] `pytest tests/ -m "not requires_api_key"` 在 CI 全绿 (128 passed)
- [x] `mypy src/` 0 error / `ruff check src/ tests/` 0 warning
- [x] `gg_relay.session.*` 覆盖率 ≥ 95% (99.8% project-wide; `session/client.py` 99%)
- [x] spec 同步

---

**预估**: 10 task × ~2 subagent dispatch ≈ 25 dispatch，~60min wall-clock（不含真 API smoke 测试 + assembler e2e）
