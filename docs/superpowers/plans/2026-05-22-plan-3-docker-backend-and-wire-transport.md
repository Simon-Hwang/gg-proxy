# Plan 3 — Docker Backend + UnixSocketTransport + Minimal Host Proxy

**作者**: gg-relay  **创建**: 2026-05-22  **状态**: ✅ Decisions locked, ready to execute

## 1. Goal

Plan 2 完成"in-process + 真 SDK + 真 plugins"。本 Plan 切换到生产级 Docker 后端：

1. 每个 session 起独立容器（per_session，已确认）
2. host ↔ container 通过 Unix domain socket 走 NDJSON 帧（已确认）
3. container 内 runner 消费 ControlFrame；host 侧 bridge 把 HITLCoordinator 决定推到容器
4. 镜像 baked-in `gg-plugins` + `@anthropic-ai/claude-code`（已确认）
5. CI 在 gg-plugins release 触发自动 build + push 到 GHCR
6. **内置 minimal host proxy**：claude CLI 通过 `HTTPS_PROXY` 走 gg-relay 内置 proxy，仅放行 `api.anthropic.com`，按 session_id 打 audit log
7. `InProcessExecutor` 保留（dev 用）

ExecutorBackend Protocol Plan 1 已留好（K8s 预留）。

## 2. Scope

### In
- `src/gg_relay/session/transport/unixsocket.py` — UnixSocketTransport（NDJSON over AF_UNIX SOCK_STREAM）
- `src/gg_relay/session/transport/__init__.py` — re-export
- `src/gg_relay/session/executor/docker.py` — DockerExecutor（aiodocker）
- `src/gg_relay/session/runner/{__init__.py, wire_runner.py, bridge.py}` — 容器入口 + host bridge
- `src/gg_relay/session/client.py` — refactor 抽 `_make_runner_core`；新增 `make_wire_runner`
- `src/gg_relay/session/spec.py` — 新增 `SessionRuntimeContext`（Plan 4 D4.17 提前落地，Plan 3 已需要 credentials）
- `src/gg_relay/proxy/{__init__.py, server.py, audit.py}` — minimal forward proxy
- `images/gg-relay-runner/{Dockerfile, entrypoint.py, .dockerignore, README.md}`
- `.github/workflows/build-runner-image.yml`
- `pyproject.toml` — 加 `aiodocker>=0.21`, `aiohttp>=3.9`
- `tests/integration/test_unixsocket_transport.py`
- `tests/integration/test_docker_executor.py` — `@requires_docker`
- `tests/unit/proxy/test_minimal_proxy.py`
- `tests/unit/session/test_wire_bridge.py`
- `scripts/spike_docker_round_trip.sh` + `docs/docker-runner-spike.md`

### Out
- K8s backend — Plan 5+
- Container runtime / image-slim — 后续
- 多 manifest 增量 install / 镜像缓存 — 后续
- Proxy dashboard 页面 — Plan 4（或推 v2）
- Proxy 限速/配额 — v2

## 3. Dependencies
- Plan 2 已合入 main
- 本机 Docker daemon 可用（已 verify, Engine 24.0.6）
- `pip install aiodocker aiohttp`
- ANTHROPIC_API_KEY 在 host（用于 Task 0 spike + integration test）
- GHCR push 权限（GHA `GITHUB_TOKEN` 自带）
- `gg-plugins` repo URL 可达（git clone）

## 4. Locked Decisions

| ID | 决策 | 终值 |
|---|---|---|
| D3.1 | socket frame encoding | NDJSON (`\n`-delimited JSON) |
| D3.2 | 镜像 base | `python:3.11-slim` + Node 20 + `@anthropic-ai/claude-code` + `gg-plugins`（**四件套**） |
| D3.3 | install.sh 时机 | build-time baked in（已确认） |
| D3.4 | tag 命名 | `ghcr.io/<org>/gg-relay-runner:gg-plugins-v{X.Y.Z}` + `latest` |
| D3.5 | socket 路径 | `/var/run/gg-relay/{runtime_id}.sock`，host mkdir+chmod 0777，container bind-mount |
| D3.6 | container 资源 | default `mem=2g, cpus=2, pids=512`，可 override |
| D3.7 | container 网络模式 | `--network=bridge`（**改**，原 `none` 不可行；claude CLI 必须外联） |
| D3.8 | registry | GHCR |
| D3.9 | CI 触发 | `workflow_dispatch` + `repository_dispatch[gg_plugins_release]` |
| D3.10 | heartbeat | runner 每 5s ping，host 3 次未 pong 标 unhealthy + cancel |
| D3.11 | 退出码映射 | 0=completed, 137/SIGTERM=cancelled, 其他=failed |
| D3.12 | stop() timeout | 5s graceful (shutdown frame) + kill |
| D3.13 | claude CLI HTTPS 出口 | **gg-relay 内置 minimal proxy (B1-min)**：aiohttp ~200 行；白名单 `api.anthropic.com`；按 `X-Relay-Session-Id` header 写 audit 文件 log |
| D3.14 | 镜像 plugins 范围 | `--profile full` |
| D3.15 | CLI 版本管理 | Dockerfile `ARG CLAUDE_CLI_VERSION=2.1.133` pin |
| D3.16 | credentials 注入 | `SessionRuntimeContext.credentials: Mapping[str, str]`（**不**进 SessionSpec / 不持久化 / 不渲染） |
| D3.17 | auth backend | Plan 3 仅 `ANTHROPIC_API_KEY` |
| D3.18 | docker client | `aiodocker>=0.21` |
| D3.19 | PID 1 信号 | Dockerfile 加 `tini` 作 ENTRYPOINT init |
| 新增 | UnixSocketTransport API | 拆 `listen() -> UnixSocketServer` + `connect()` + `Server.accept()` |
| 新增 | Mount SELinux | mount mode `"rw,z"` 兼容 SELinux host |

## 5. Module Layout

```
src/gg_relay/
├── session/
│   ├── transport/
│   │   ├── inmemory.py             # 不变
│   │   └── unixsocket.py           # NEW
│   ├── executor/
│   │   ├── inprocess.py            # 不变
│   │   └── docker.py               # NEW
│   ├── runner/                     # NEW 子包
│   │   ├── __init__.py
│   │   ├── wire_runner.py          # container 内 entry
│   │   ├── bridge.py               # host 侧 bridge
│   │   └── proxy_client.py         # WireCoordinatorProxy
│   ├── client.py                   # MODIFIED: 抽 _make_runner_core + make_wire_runner
│   └── spec.py                     # MODIFIED: 新增 SessionRuntimeContext
└── proxy/                          # NEW
    ├── __init__.py
    ├── server.py                   # aiohttp HTTP forward proxy
    └── audit.py                    # audit log writer

images/gg-relay-runner/
├── Dockerfile                      # multi-stage
├── entrypoint.py                   # PID 1 wrapper (tini exec)
├── .dockerignore
└── README.md

.github/workflows/
└── build-runner-image.yml          # GHA workflow

tests/
├── unit/session/
│   ├── test_wire_bridge.py
│   ├── test_wire_coordinator_proxy.py
│   └── test_docker_executor_mock.py     # mock aiodocker
├── unit/proxy/
│   └── test_minimal_proxy.py
└── integration/
    ├── test_unixsocket_transport.py     # socketpair 内进程
    ├── test_proxy_smoke.py              # 真起 aiohttp + curl
    └── test_docker_executor.py          # @requires_docker

scripts/
└── spike_docker_round_trip.sh           # Task 0

docs/
├── docker-runner-spike.md               # Task 0 output
└── superpowers/specs/2026-05-22-sdk-bootstrap-and-runtime-design.md  # MODIFIED §5/§6
```

## 6. Task Breakdown

### Task 0 — Docker + claude CLI + ANTHROPIC_API_KEY round-trip spike

**Goal**: 在真 Docker container 中跑 `claude --print "say hi"`，验证：
- 在 `python:3.11-slim` + Node 20 + `@anthropic-ai/claude-code@2.1.133` 容器中能正常起 claude CLI
- ANTHROPIC_API_KEY 通过 env 注入有效
- `HTTPS_PROXY=http://host.docker.internal:8888` 透传给 claude CLI 后能走 proxy 联通 Anthropic（host 上用 tinyproxy 或 mitmproxy 临时模拟）
- claude CLI stdin/stdout 流（stream-json 模式）在容器内 PID > 1 时仍正常

**Files**:
- `scripts/spike_docker_round_trip.sh` — 一键脚本：build minimal image → docker run → 触发 claude CLI → 解析 stdout
- `docs/docker-runner-spike.md` — 记录：
  - 命令行 + 镜像 size 实测
  - 启动到首个 token 延迟
  - 出现的问题（NVM 版 node vs apt-get nodejs / npm cache / proxy CONNECT 协议）+ 解决
  - 结论：D3.2 / D3.13 / D3.19 是否需要调整

**DOD**: spike 报告写好。若 D3.13 host proxy 协议层有问题（如 claude CLI 对 mitmproxy CA 不信任），文档化解决方法（mount CA cert 等）。

### Task 1 — `UnixSocketTransport`

**Files**: `src/gg_relay/session/transport/unixsocket.py`, `tests/integration/test_unixsocket_transport.py`

**Skeleton**:

```python
from __future__ import annotations
import asyncio
import json
import socket
from pathlib import Path
from typing import cast
from .protocol import ControlFrame, EventFrame, TransportClosed


class UnixSocketTransport:
    """NDJSON over AF_UNIX SOCK_STREAM. Drain-then-close semantics (spec §6.4)."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._r = reader
        self._w = writer
        self._closing = False

    @classmethod
    def from_socket(cls, sock: socket.socket) -> "UnixSocketTransport":
        """Build transport from a connected socket (used by socketpair tests)."""
        loop = asyncio.get_running_loop()
        # Wrap raw socket via open_unix_connection won't work; use create_connection_protocol manually.
        # Cleanest: use loop.create_connection with sock=sock.
        # Simpler: open_unix_connection of fd? - actually use StreamReader/StreamReaderProtocol manually.
        reader = asyncio.StreamReader(loop=loop)
        protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
        transport, _ = loop.run_until_complete(...)  # see actual impl
        # The proper way: use asyncio.streams.open_connection with sock kwarg in 3.10+
        ...  # impl detail in real code

    @classmethod
    async def connect(cls, path: Path, *, retry_timeout: float = 10.0) -> "UnixSocketTransport":
        """Client side. Retry until the socket file exists + server accepts."""
        deadline = asyncio.get_event_loop().time() + retry_timeout
        last_err: Exception | None = None
        while asyncio.get_event_loop().time() < deadline:
            try:
                reader, writer = await asyncio.open_unix_connection(str(path))
                return cls(reader, writer)
            except (FileNotFoundError, ConnectionRefusedError) as e:
                last_err = e
                await asyncio.sleep(0.1)
        raise ConnectionError(f"could not connect to {path} within {retry_timeout}s") from last_err

    async def send(self, frame: ControlFrame | EventFrame) -> None:
        if self._closing:
            raise TransportClosed("transport is closing")
        data = json.dumps(frame, separators=(",", ":"), default=str).encode() + b"\n"
        self._w.write(data)
        try:
            await self._w.drain()
        except (ConnectionResetError, BrokenPipeError) as e:
            self._closing = True
            raise TransportClosed("peer closed during send") from e

    async def recv(self) -> EventFrame | ControlFrame:
        # drain-then-close: only raise after StreamReader returns b"" (EOF)
        line = await self._r.readline()
        if not line:
            raise TransportClosed("peer closed (EOF)")
        try:
            return cast(EventFrame, json.loads(line.decode()))
        except json.JSONDecodeError as e:
            raise TransportClosed(f"malformed frame: {e}") from e

    @property
    def is_alive(self) -> bool:
        return not self._closing

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._w.close()
        try:
            await self._w.wait_closed()
        except Exception:  # noqa: BLE001
            pass


class UnixSocketServer:
    """Host side: bind + listen + accept."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._server: asyncio.AbstractServer | None = None
        self._accepted: asyncio.Queue[UnixSocketTransport] = asyncio.Queue()

    @classmethod
    async def listen(cls, path: Path) -> "UnixSocketServer":
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        self = cls(path)
        self._server = await asyncio.start_unix_server(self._on_accept, path=str(path))
        path.chmod(0o666)  # let non-root container connect (SELinux: add :z mount)
        return self

    async def _on_accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await self._accepted.put(UnixSocketTransport(reader, writer))

    async def accept(self, *, timeout: float = 30.0) -> UnixSocketTransport:
        return await asyncio.wait_for(self._accepted.get(), timeout=timeout)

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
```

**Tests** (8):
1. `test_listen_creates_socket_file` — socket file 存在 + 0o666
2. `test_connect_after_listen` — round-trip 单帧
3. `test_round_trip_event_and_control_frames` — 双向
4. `test_drain_after_peer_close` — writer 写 5 帧 + close → reader 读完 5 帧后才 TransportClosed
5. `test_recv_blocks_until_send` — explicit timeout
6. `test_malformed_json_raises_transport_closed` — 防御
7. `test_large_frame_round_trip` — 1MB payload
8. `test_connect_retries_until_server_listens` — listen 在 1s 后才发生，connect 用 retry_timeout=2

**DOD**: 8 tests 绿 + mypy + ruff 全清。

### Task 2 — `WireCoordinatorProxy` + `wire_runner.py`

**Files**: `src/gg_relay/session/runner/__init__.py`, `runner/proxy_client.py`, `runner/wire_runner.py`

**Skeleton (proxy_client.py)**:

```python
from __future__ import annotations
import asyncio
from typing import Any, Literal

from gg_relay.session.hitl.coordinator import HITLCoordinator, HITLNotPending  # Plan 4 might extend
from gg_relay.session.transport.protocol import (
    ControlFrame, EventFrame, SessionTransport,
)


class WireCoordinatorProxy:
    """Container-side HITLCoordinator stand-in: sends tool.request over transport,
    awaits a matching tool.decision ControlFrame, returns decision string.

    Caller is responsible for invoking `consume_loop()` as a background task so that
    incoming ControlFrames get routed to pending futures.
    """

    def __init__(self, transport: SessionTransport) -> None:
        self._transport = transport
        self._pending: dict[str, asyncio.Future[Literal["accept", "deny"]]] = {}

    async def request(self, req_id: str, *, tool: str, args: dict[str, Any]) -> Literal["accept", "deny"]:
        # `tool.request` frame is sent by client.py runner before calling us; we only wait here.
        fut: asyncio.Future[Literal["accept", "deny"]] = asyncio.get_running_loop().create_future()
        if req_id in self._pending:
            raise ValueError(f"duplicate req_id {req_id}")
        self._pending[req_id] = fut
        try:
            return await fut
        finally:
            self._pending.pop(req_id, None)

    async def consume_loop(self) -> None:
        """Background task: route ControlFrames to pending futures.
        Exits when transport closes."""
        from gg_relay.session.transport.protocol import TransportClosed
        try:
            while True:
                frame = await self._transport.recv()
                if frame.get("type") == "tool.decision":
                    req_id = frame.get("req_id")
                    decision = frame.get("decision")
                    fut = self._pending.get(req_id)
                    if fut and not fut.done():
                        fut.set_result(decision)
                elif frame.get("type") == "shutdown":
                    # Signal main runner to exit; bridge sends this on graceful stop
                    raise SystemExit(0)
        except TransportClosed:
            # Resolve all pending with "deny" to unblock waiters
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_result("deny")
```

**Skeleton (wire_runner.py)**:

```python
"""Container entry. PID 1 (under tini). Loads SessionSpec + SessionRuntimeContext
from env, connects to socket, spawns make_wire_runner."""

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

from gg_relay.session.client import make_wire_runner
from gg_relay.session.hitl.policy import ToolPolicy
from gg_relay.session.runner.proxy_client import WireCoordinatorProxy
from gg_relay.session.spec import SessionSpec
from gg_relay.session.transport.unixsocket import UnixSocketTransport

logger = logging.getLogger("gg_relay.wire_runner")


async def main() -> None:
    spec = SessionSpec.from_json(os.environ["GG_RELAY_SPEC_JSON"])
    socket_path = Path(os.environ["GG_RELAY_SOCKET"])
    transport = await UnixSocketTransport.connect(socket_path, retry_timeout=15)

    coordinator = WireCoordinatorProxy(transport)
    consume_task = asyncio.create_task(coordinator.consume_loop())

    # Policy: container side trusts everything as NEEDS_HITL; host policy already decided.
    # All allow/deny logic is host-side.
    policy = ToolPolicy(auto_accept_tools=frozenset(), hitl_tools=frozenset(["*"]),
                        neutral_tools=frozenset(), dangerous_patterns=(),
                        path_required_tools=frozenset())

    runner = make_wire_runner(policy=policy, coordinator=coordinator)
    try:
        await runner(transport, spec)
    finally:
        consume_task.cancel()
        try:
            await consume_task
        except (asyncio.CancelledError, SystemExit):
            pass
        await transport.close()


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


if __name__ == "__main__":
    _setup_logging()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(137)
```

`SessionSpec.from_json` 是 Task 2 顺带补的 classmethod。

**Tests** (5 for proxy_client, run with mock transport):
1. `test_request_sends_via_caller_and_blocks_until_decision`
2. `test_duplicate_req_id_raises`
3. `test_consume_loop_routes_decision_to_future`
4. `test_consume_loop_resolves_pending_with_deny_on_transport_close`
5. `test_shutdown_frame_triggers_systemexit`

### Task 3 — `WireBridge`（host 侧）

**Files**: `src/gg_relay/session/runner/bridge.py`, `tests/unit/session/test_wire_bridge.py`

```python
class WireBridge:
    """Host-side: consume EventFrames from wire transport; route tool.request to coordinator;
    reply with tool.decision; respond to ping with pong; emit shutdown on stop."""

    def __init__(self, transport: SessionTransport, coordinator: HITLCoordinator) -> None:
        self._transport = transport
        self._coordinator = coordinator
        self._frames: list[EventFrame] = []  # captured for persistence; Plan 4 hooks store here
        self._shutdown = asyncio.Event()

    async def run(self) -> None:
        try:
            while not self._shutdown.is_set():
                frame = await self._transport.recv()
                if frame.get("type") == "tool.request":
                    asyncio.create_task(self._handle_tool_request(frame))
                elif frame.get("type") == "ping":
                    await self._transport.send({"type": "pong", "seq": frame["seq"], ...})
                else:
                    self._frames.append(frame)
                    if frame.get("type") == "session.end":
                        break
        except TransportClosed:
            pass

    async def _handle_tool_request(self, frame: EventFrame) -> None:
        req_id = frame["req_id"]
        decision = await self._coordinator.request(req_id, tool=frame["tool"], args=frame["args"])
        await self._transport.send({"type": "tool.decision", "req_id": req_id, "decision": decision, ...})

    async def shutdown(self, *, grace: float = 5.0) -> None:
        await self._transport.send({"type": "shutdown", "seq": -1, "ts": _now_iso()})
        self._shutdown.set()
        try:
            await asyncio.wait_for(self._await_session_end(), timeout=grace)
        except TimeoutError:
            pass
        await self._transport.close()
```

**Tests** (6): mock transport pair, assert tool.request → coordinator.request → tool.decision frame; ping/pong; shutdown 流；session.end 触发 break; close 后 run 退出 cleanly.

### Task 4 — `DockerExecutor`

**Files**: `src/gg_relay/session/executor/docker.py`, `tests/unit/session/test_docker_executor_mock.py`

`pyproject.toml` 加 `aiodocker>=0.21`.

```python
from aiodocker import Docker
from aiodocker.containers import DockerContainer

from gg_relay.session.spec import SessionRuntimeContext, SessionSpec, RuntimeHandle
from gg_relay.session.transport.unixsocket import UnixSocketServer


class DockerExecutor:
    def __init__(self, *,
                 image: str = "ghcr.io/gg-org/gg-relay-runner:latest",
                 socket_root: Path = Path("/var/run/gg-relay"),
                 proxy_url: str | None = None,
                 client: Docker | None = None,
                 default_mem: str = "2g",
                 default_cpus: float = 2.0) -> None:
        self._image = image
        self._socket_root = socket_root
        self._proxy_url = proxy_url
        self._docker = client or Docker()
        self._mem = default_mem
        self._cpus = default_cpus
        self._containers: dict[str, DockerContainer] = {}
        self._servers: dict[str, UnixSocketServer] = {}

    async def start(self, spec: SessionSpec, *, runtime_ctx: SessionRuntimeContext) -> RuntimeHandle:
        runtime_id = uuid.uuid4().hex
        socket_path = self._socket_root / f"{runtime_id}.sock"
        server = await UnixSocketServer.listen(socket_path)
        self._servers[runtime_id] = server

        env = {
            "GG_RELAY_SPEC_JSON": spec.to_json(),
            "GG_RELAY_SOCKET": f"/var/run/gg-relay/{runtime_id}.sock",
            "RELAY_TRACE_ID": runtime_ctx.trace_id,
            "RELAY_SESSION_ID": runtime_id,
            **dict(runtime_ctx.credentials),
            **dict(spec.plugins.extra_env),
        }
        if self._proxy_url:
            env["HTTPS_PROXY"] = self._proxy_url
            env["HTTP_PROXY"] = self._proxy_url
            env["NO_PROXY"] = "localhost,127.0.0.1"

        cfg = {
            "Image": self._image,
            "Env": [f"{k}={v}" for k, v in env.items()],
            "HostConfig": {
                "NetworkMode": "bridge",
                "AutoRemove": True,
                "Memory": _parse_size(self._mem),
                "NanoCpus": int(self._cpus * 1e9),
                "PidsLimit": 512,
                "Binds": [
                    f"{self._socket_root}:/var/run/gg-relay:rw,z",
                    f"{spec.cwd}:/workspace:rw,z",
                ],
            },
        }
        container = await self._docker.containers.run(config=cfg, name=f"gg-relay-{runtime_id[:8]}")
        self._containers[runtime_id] = container

        transport = await server.accept(timeout=30.0)
        return RuntimeHandle(
            backend="docker", runtime_id=runtime_id, transport=transport,
            started_at=datetime.now(UTC),
            extra=(("container_id", container.id), ("image", self._image)),
        )

    async def stop(self, handle: RuntimeHandle) -> None:
        container = self._containers.pop(handle.runtime_id, None)
        if container:
            try:
                # graceful: send shutdown frame, wait container exit
                await asyncio.wait_for(
                    handle.transport.send({"type": "shutdown", "seq": -1, "ts": _now_iso()}),
                    timeout=1,
                )
                await asyncio.wait_for(container.wait(), timeout=5)
            except (TimeoutError, TransportClosed, Exception):
                with contextlib.suppress(Exception):
                    await container.kill()
        with contextlib.suppress(Exception):
            await handle.transport.close()
        server = self._servers.pop(handle.runtime_id, None)
        if server:
            await server.close()

    async def health(self, handle: RuntimeHandle) -> bool:
        container = self._containers.get(handle.runtime_id)
        if not container:
            return False
        try:
            info = await container.show()
            return info["State"]["Status"] == "running" and handle.transport.is_alive
        except Exception:
            return False


def _parse_size(s: str) -> int:
    """'2g' -> 2*1024*1024*1024 etc."""
    units = {"k": 1024, "m": 1024**2, "g": 1024**3}
    return int(s[:-1]) * units[s[-1].lower()]
```

**Tests** (7): mock `Docker()` client; assert config dict shape (network=bridge, env contains credentials, mounts contain `:z`); assert server.accept called; assert stop sends shutdown frame; assert health returns False after stop.

### Task 5 — `client.py` refactor 抽 `_make_runner_core` + `make_wire_runner`

把 Plan 2 完成的 `make_sdk_runner` 内部 dispatch loop 抽出来：

```python
async def _make_runner_core(
    transport: SessionTransport,
    spec: SessionSpec,
    coordinator: HITLCoordinator | WireCoordinatorProxy,
    policy: ToolPolicy,
    sdk_factory: SdkFactory,
    install_report: InstallReport | None = None,
) -> None:
    """The shared body — SDK dispatch + FIFO + error frame.
    Used by both make_sdk_runner (in-process) and make_wire_runner (docker)."""
    ...


def make_sdk_runner(*, policy, coordinator, install_report=None, sdk_factory=ClaudeSDKClient):
    async def runner(transport, spec):
        await _make_runner_core(transport, spec, coordinator, policy, sdk_factory, install_report)
    return runner


def make_wire_runner(*, policy, coordinator: WireCoordinatorProxy, sdk_factory=ClaudeSDKClient):
    """Container-side runner. Coordinator is a WireCoordinatorProxy."""
    async def runner(transport, spec):
        await _make_runner_core(transport, spec, coordinator, policy, sdk_factory, None)
    return runner
```

无新 test（Plan 2 + Task 3 已覆盖）。

### Task 6 — Dockerfile（4 件套 + tini）

**Files**: `images/gg-relay-runner/Dockerfile`, `.dockerignore`, `README.md`

```dockerfile
# ---- Stage 1: gg-plugins install ----
FROM node:20-bookworm-slim AS plugins-builder
ARG GG_PLUGINS_VERSION
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/gg-plugins
RUN git clone --depth 1 --branch ${GG_PLUGINS_VERSION} \
    https://github.com/<org>/gg-plugins.git . \
 && npm ci --omit=dev --no-audit --no-fund

# ---- Stage 2: runtime ----
FROM python:3.11-slim
ARG CLAUDE_CLI_VERSION=2.1.133

# System deps: tini for PID 1, Node for claude CLI + gg-plugins
RUN apt-get update && apt-get install -y --no-install-recommends \
    tini ca-certificates curl gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g @anthropic-ai/claude-code@${CLAUDE_CLI_VERSION} \
 && npm cache clean --force

COPY --from=plugins-builder /opt/gg-plugins /opt/gg-plugins
ENV GG_PLUGINS_HOME=/opt/gg-plugins

# gg-relay
COPY . /opt/gg-relay
WORKDIR /opt/gg-relay
RUN pip install --no-cache-dir -e .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# tini as PID 1
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "gg_relay.session.runner.wire_runner"]
```

`.dockerignore`: 排除 `.git`, `.venv`, `tests`, `docs`, `*.md`, `__pycache__`.

`README.md`: 镜像构建/调试命令、本地 build 步骤、ARG 列表。

### Task 7 — `entrypoint.py`

实际 wire_runner.py 本身已是 entry，无需独立 entrypoint.py。Task 7 改名为 **runner sanity wrapper**：在 `wire_runner.main()` 顶部加：
- env 必填校验（GG_RELAY_SPEC_JSON / GG_RELAY_SOCKET / ANTHROPIC_API_KEY）
- claude CLI which 检查
- signal handler: SIGTERM → 让 main loop graceful exit

无独立文件，归并到 Task 2。

### Task 8 — CI workflow

**Files**: `.github/workflows/build-runner-image.yml`

```yaml
name: Build runner image
on:
  workflow_dispatch:
    inputs:
      gg_plugins_version:
        description: gg-plugins git tag
        required: true
      claude_cli_version:
        description: claude CLI npm version
        required: true
        default: "2.1.133"
  repository_dispatch:
    types: [gg_plugins_release]
jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4
      - id: ver
        run: |
          GG_VER="${{ inputs.gg_plugins_version || github.event.client_payload.tag }}"
          CLI_VER="${{ inputs.claude_cli_version || '2.1.133' }}"
          echo "gg=$GG_VER" >> $GITHUB_OUTPUT
          echo "cli=$CLI_VER" >> $GITHUB_OUTPUT
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: docker/build-push-action@v5
        with:
          context: .
          file: images/gg-relay-runner/Dockerfile
          build-args: |
            GG_PLUGINS_VERSION=${{ steps.ver.outputs.gg }}
            CLAUDE_CLI_VERSION=${{ steps.ver.outputs.cli }}
          push: true
          tags: |
            ghcr.io/${{ github.repository_owner }}/gg-relay-runner:gg-plugins-${{ steps.ver.outputs.gg }}
            ghcr.io/${{ github.repository_owner }}/gg-relay-runner:latest
          cache-from: type=gha
          cache-to: type=gha,mode=max
```

README 加一段说明如何在 gg-plugins 侧 setup release-hook（curl `gh api repos/<org>/gg-relay/dispatches -d '{"event_type":"gg_plugins_release","client_payload":{"tag":"v0.4.2"}}'`）。

### Task 9 — `install.done` / `ping` / `pong` 实现

- wire_runner 启动后调 `assembler.prepare()`（实际镜像已 baked，只需读 install-state.json）→ emit `install.done`
- runner 5s 周期发 `ping`，host bridge 收到立即回 `pong`
- bridge 3 次连续未收 `pong` → `executor.stop(handle)` + emit `error code=heartbeat_timeout`

新增 `transport/protocol.py`:

```python
class PingFrame(_BaseFrame):
    type: Literal["ping"]

class PongFrame(_BaseFrame):
    type: Literal["pong"]

class ShutdownFrame(_BaseFrame):
    type: Literal["shutdown"]
```

加 `make_ping(seq)` / `make_pong(seq)` / `make_shutdown()` builders.

**Tests** (4): ping/pong round-trip; heartbeat timeout triggers stop + error frame; shutdown frame triggers graceful runner exit.

### Task 10 — Docker integration test (`@requires_docker`)

**File**: `tests/integration/test_docker_executor.py`

```python
@pytest.fixture
def runner_image():
    """Build a test image locally on first run. Cached via session-scoped fixture."""
    ...

@pytest.mark.requires_docker
@pytest.mark.requires_api_key  # claude CLI 实际跑要 ANTHROPIC_API_KEY
async def test_docker_executor_full_round_trip(tmp_path, runner_image):
    executor = DockerExecutor(image=runner_image, proxy_url=None)  # 暂不走 proxy
    coordinator = HITLCoordinator()
    spec = SessionSpec(prompt="Reply with the word OK", cwd=tmp_path,
                       plugins=PluginManifest(profile="minimal"), executor="docker")
    runtime_ctx = SessionRuntimeContext(
        credentials={"ANTHROPIC_API_KEY": os.environ["ANTHROPIC_API_KEY"]},
        trace_id="test-trace-0", public_callback_base="http://localhost",
    )
    handle = await executor.start(spec, runtime_ctx=runtime_ctx)
    bridge = WireBridge(handle.transport, coordinator)
    bridge_task = asyncio.create_task(bridge.run())
    
    frames = []
    try:
        await asyncio.wait_for(bridge_task, timeout=120)
    finally:
        await bridge.shutdown()
        await executor.stop(handle)
    
    assert any(f["type"] == "install.done" for f in bridge._frames)
    assert bridge._frames[-1]["type"] == "session.end"
```

CI: 在 GHA runner 上跑（自带 Docker），`requires_docker` marker；本地用 Docker Desktop。

### Task 11 — Coverage + spec + final

- `pytest tests/ -m "not requires_docker and not requires_api_key" --cov` 绿 ≥ 90%
- spec sync: §5 加 DockerExecutor 子节、§6 加 UnixSocketTransport / ping/pong frames、§9 host proxy
- README 加 "Docker backend usage" 段
- `examples/docker_executor_demo.py` 跑通
- final squash merge

### Task 12 — Minimal host proxy

**Files**: `src/gg_relay/proxy/{__init__.py, server.py, audit.py}`, `tests/unit/proxy/test_minimal_proxy.py`, `tests/integration/test_proxy_smoke.py`

**`server.py`** (~150 行 aiohttp):

```python
from aiohttp import web, ClientSession, ClientTimeout
from urllib.parse import urlsplit
import asyncio

ALLOWED_HOSTS = frozenset({"api.anthropic.com"})

class MinimalProxy:
    def __init__(self, *, audit, allowed_hosts: frozenset[str] = ALLOWED_HOSTS) -> None:
        self._audit = audit
        self._allowed = allowed_hosts
        self._upstream = ClientSession(timeout=ClientTimeout(total=300))

    def make_app(self) -> web.Application:
        app = web.Application()
        # HTTP CONNECT method for HTTPS tunneling
        app.router.add_route("CONNECT", "/{tail:.*}", self.handle_connect)
        # plain HTTP forwarding (not used by claude CLI but defensive)
        app.router.add_route("*", "/{tail:.*}", self.handle_http)
        return app

    async def handle_connect(self, request: web.Request) -> web.StreamResponse:
        """CONNECT host:port HTTP/1.1 — establish tunnel."""
        host_port = request.match_info["tail"]
        host, _, port = host_port.partition(":")
        port = int(port or 443)
        session_id = request.headers.get("X-Relay-Session-Id", "unknown")
        
        if host not in self._allowed:
            await self._audit.deny(session_id=session_id, host=host, reason="host_not_in_whitelist")
            return web.Response(status=403, text="host not allowed")
        
        await self._audit.allow(session_id=session_id, host=host, port=port)
        
        # Tunnel bytes between client (claude CLI socket) and upstream (api.anthropic.com:443)
        # aiohttp's web framework doesn't natively support CONNECT well — use low-level
        # asyncio.open_connection for upstream, hijack the request transport.
        ...
```

注意：aiohttp 对 CONNECT 不直接支持，可能要用 `asyncio.start_server` 直接写 raw socket forward。骨架先放着，implementer subagent 详细实现。

**`audit.py`**:

```python
import json
from datetime import UTC, datetime
from pathlib import Path

class AuditLog:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, event: dict) -> None:
        event["ts"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")

    async def allow(self, *, session_id: str, host: str, port: int) -> None:
        self._write({"event": "allow", "session_id": session_id, "host": host, "port": port})

    async def deny(self, *, session_id: str, host: str, reason: str) -> None:
        self._write({"event": "deny", "session_id": session_id, "host": host, "reason": reason})
```

**Tests** (6):
1. `test_connect_allowed_host_succeeds`
2. `test_connect_blocked_host_returns_403`
3. `test_audit_log_writes_allow_event`
4. `test_audit_log_writes_deny_event`
5. `test_missing_session_id_header_logged_as_unknown`
6. `test_upstream_unavailable_returns_502`

Integration test (`test_proxy_smoke.py`): 真起 proxy + curl 通过 proxy 访问 `https://api.anthropic.com/v1/messages`（POST 一个 minimal payload，期待 401 因为没 API key 也算 reachable）。

**注**：proxy 服务由 `gg-relay serve` 在 Plan 4 启动；Plan 3 只交付 module + 单元测试。

## 7. Test Strategy

| 层 | 数量 | 覆盖 |
|---|---|---|
| Unit: unixsocket transport | 8 | 含 drain-after-close, large frame |
| Unit: WireCoordinatorProxy | 5 | request/decision routing |
| Unit: WireBridge | 6 | mock transport pair |
| Unit: DockerExecutor (mock aiodocker) | 7 | config shape, mounts, env |
| Unit: ping/pong + heartbeat | 4 | timeout cascade |
| Unit: minimal proxy | 6 | whitelist, audit log |
| Integration: docker e2e | 1 | `@requires_docker @requires_api_key` |
| Integration: proxy smoke | 1 | 真 curl |
| **Total 新增** | **~38** | + 104 prior = **~142** |

## 8. Risks

- **R1 (HIGH)**: spike Task 0 暴露 claude CLI 在容器内有问题（如必须 OAuth login 而非 API key）→ 推回 spec，可能改 D3.17
- **R2**: aiohttp 不原生支持 CONNECT proxy → 转用 `asyncio.start_server` raw TCP 实现 Task 12
- **R3**: `--auto-remove=True` + 异常 → 排错难。默认开启，DEBUG 模式可关
- **R4**: socket mount SELinux/AppArmor 在某些 host 上仍可能阻断；README 给 troubleshooting
- **R5**: 镜像 size 1.5-2GB → 接受，Plan 5+ slim
- **R6**: gg-plugins 没发版机制 → CI workflow 接 repository_dispatch，gg-plugins 侧需要手工触发或加配套 GHA
- **R7**: `aiodocker` 版本更新可能 API break → pin `aiodocker>=0.21,<0.23`

## 9. Deferred

- K8s backend — Plan 5+
- 镜像 slim 优化 / multi-variant — v2
- Proxy dashboard 页面 / 限速 / 配额 — v2
- mTLS for socket — v2
- container 资源 quota 动态调整 — Plan 4 加 `SessionSpec.resources` 字段

## 10. Self-Review checklist

- [ ] Task 0 spike 完成 + 报告写好
- [ ] 每 task TDD
- [ ] `pytest tests/ -m "not requires_docker and not requires_api_key"` 在 CI 全绿
- [ ] mypy + ruff 全清
- [ ] `docker build images/gg-relay-runner/` 本地成功
- [ ] `examples/walking_skeleton_demo.py` 仍 exit 0
- [ ] `examples/docker_executor_demo.py` 跑通（手工，本地有 Docker + API key）
- [ ] spec / plan template 同步
- [ ] subagent-driven-development

---

**预估**: 12 task × ~3 dispatch ≈ 40 dispatch，~120min wall-clock + docker build 时间
