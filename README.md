# gg-relay

Python middleware/relay service over Claude Code SDK.

Provides session management, OpenTelemetry tracing, IM integration (Feishu/DingTalk/Slack), and a visual Kanban dashboard.

## Quick Start

```bash
# Install
uv pip install -e ".[dev]"

# Configure
cp .env.example .env
# Edit .env with your API keys

# Run migrations
gg-relay migrate

# Start server
gg-relay serve
```

## Architecture

See [PLAN.md](./PLAN.md) for the full implementation plan.

## Development

```bash
# Run tests
pytest

# Lint
ruff check src/ tests/
mypy src/

# Format
ruff format src/ tests/
```

## Quick Start: Walking Skeleton (in-process)

The in-process executor lets you drive a fake (or real) Claude Code SDK session
end-to-end without containers, plugin install, IM webhooks, or a database — useful
for local development, contract tests, and smoke-testing transport changes.

```bash
source .venv/bin/activate
python examples/walking_skeleton_demo.py
```

Expected output: a sequence of `tool.result` / `tool.request` / `session.end`
event frames, with a synthetic IM responder auto-approving every `tool.request`.
The demo exits 0 when the session completes cleanly.

References:
- Design — [`docs/superpowers/specs/2026-05-22-sdk-bootstrap-and-runtime-design.md`](./docs/superpowers/specs/2026-05-22-sdk-bootstrap-and-runtime-design.md)
- Implementation plan — [`docs/superpowers/plans/2026-05-22-walking-skeleton-inprocess.md`](./docs/superpowers/plans/2026-05-22-walking-skeleton-inprocess.md)
- Demo source — [`examples/walking_skeleton_demo.py`](./examples/walking_skeleton_demo.py)

## Plan 2: Plugin Assembly + Real SDK Dispatch

Plan 2 extends the walking skeleton in-process backend with two production-shaped
capabilities:

### Plugin installation via `gg-plugins/install.sh`

`InstallShellAssembler.prepare(spec, install_dir)` shells out to the real
gg-plugins installer, then parses
`<install_dir>/.claude/gg/install-state.json` (schema `gg.install.v1`)
into a frozen `InstallReport`. The report is then threaded into the runner
via `make_sdk_runner(install_report=report)` and emitted as the very first
`install.done` event frame.

```python
from pathlib import Path
from gg_relay.session.plugins import InstallShellAssembler
from gg_relay.session.spec import PluginManifest, SessionSpec

assembler = InstallShellAssembler(plugins_home=Path("/path/to/gg-plugins"))
spec = SessionSpec(prompt="...", cwd=Path("/work"),
                   plugins=PluginManifest(profile="minimal"),
                   executor="inprocess")
report = await assembler.prepare(spec, install_dir=Path("/tmp/session-home"))
```

Set `GG_PLUGINS_HOME` to override the installer location for tests:

```bash
export GG_PLUGINS_HOME=/data/workspace/github/gg-plugins
pytest -m "not requires_api_key"  # runs the assembler e2e test against the real installer
```

If the install.sh exits non-zero or the state file is missing,
`PluginInstallError(returncode, stderr, argv)` is raised before
`executor.start()` is even called.

### Real SDK dataclass dispatch + bidirectional FIFO mapping

`make_sdk_runner()` now dispatches on real `claude_code_sdk` dataclasses
(`AssistantMessage`, `UserMessage`, `SystemMessage`, `ResultMessage`,
`StreamEvent`) via a `match` statement. The dict-stub shim is gone.

Because `ToolPermissionContext` carries no `tool_use_id`, the runner pairs
the host's `req_id` (assigned in `can_use_tool`) with the SDK's `tool_use_id`
via a **bidirectional defensive FIFO** over `(tool_name, frozen(input))`:

- `can_use_tool` fires first → push `(req_id, name, fi)` to `pending_perms`
- `AssistantMessage(ToolUseBlock)` fires first → push `(use_id, name, fi)` to `pending_use_blocks`
- Whichever side arrives second pops the matching counterpart and registers
  the `use_id → req_id` mapping

When a `UserMessage(ToolResultBlock(tool_use_id=X))` arrives, the runner
emits a `tool.result` frame with the mapped `req_id` (or empty string if no
mapping was ever registered — defensive, never crashes).

Background and design rationale:
[`docs/sdk-message-ordering-spike.md`](./docs/sdk-message-ordering-spike.md).

### Real API smoke test

```bash
export ANTHROPIC_API_KEY=sk-ant-...
pytest tests/integration/test_real_api_smoke.py
```

Runs one real Anthropic API call (~$0.001). Skipped automatically if the
key is unset; CI's default `pytest -m "not requires_api_key"` runs never
hit the API.

References:
- Plan — [`docs/superpowers/plans/2026-05-22-plan-2-plugin-assembly-and-real-sdk-dispatch.md`](./docs/superpowers/plans/2026-05-22-plan-2-plugin-assembly-and-real-sdk-dispatch.md)
- Spec sync — §4.6 PluginAssembler, §6.5 FIFO mapping in the SDK runtime spec
- Spike — [`docs/sdk-message-ordering-spike.md`](./docs/sdk-message-ordering-spike.md)

## Plan 3: Docker backend + wire transport + host proxy

Plan 3 ships the production-grade execution path: one container per session,
host ↔ container over an `AF_UNIX` socket, with a built-in host-side
forward proxy that allow-lists egress to `api.anthropic.com` only and writes
an audit log per session.

### Docker backend usage

```python
from gg_relay.session.executor.docker import DockerExecutor
from gg_relay.session.runner.bridge import WireBridge
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.spec import (
    PluginManifest, SessionRuntimeContext, SessionSpec,
)

executor = DockerExecutor(
    image="ghcr.io/<org>/gg-relay-runner:latest",
    socket_root=Path("/var/run/gg-relay"),
    proxy_url="http://host.docker.internal:8888",   # see "Host proxy" below
)
spec = SessionSpec(
    prompt="hello", cwd=Path("/workspace"),
    plugins=PluginManifest(profile="minimal"),
    executor="docker",
)
runtime_ctx = SessionRuntimeContext(
    credentials={"ANTHROPIC_API_KEY": api_key},
    trace_id="t-...",
)
handle = await executor.start(spec, runtime_ctx=runtime_ctx)
bridge = WireBridge(transport=handle.transport, coordinator=HITLCoordinator())
await bridge.run()         # blocks until session.end
await executor.stop(handle)
```

`SessionRuntimeContext` carries strictly runtime-only data (credentials,
trace id, callback base URL). It is **never** persisted, **never** rendered
in IM cards, and **never** serialised into `spec_json` — Plan 4 SessionManager
will inject it just before calling `executor.start()`.

### Host proxy

`MinimalProxy` (`src/gg_relay/proxy/server.py`) is a raw `asyncio` HTTP
forward proxy supporting `CONNECT` (HTTPS tunnel) and plain HTTP. Container
egress is locked to `allowed_hosts` (default `frozenset({"api.anthropic.com"})`).
Every connection is annotated with the `X-Relay-Session-Id` header and
appended to a JSONL audit log:

```python
from gg_relay.proxy import AuditLog, MinimalProxy

audit = AuditLog(Path("/var/log/gg-relay/proxy-audit.jsonl"))
proxy = MinimalProxy(audit=audit, port=8888)
await proxy.start()
```

`DockerExecutor(proxy_url="http://host.docker.internal:8888")` then exposes
the proxy to the container via `HTTPS_PROXY` / `HTTP_PROXY` env. Linux Docker
needs `--add-host=host.docker.internal:host-gateway`, which the executor sets
automatically via `HostConfig.ExtraHosts`.

### Demo

```bash
source .venv/bin/activate

# Stub mode — no Docker, no API key required. Drives the full
# WireBridge ↔ WireCoordinatorProxy ↔ UnixSocketTransport stack
# in-process so you can see the protocol end-to-end.
DOCKER_AVAILABLE=false python examples/docker_executor_demo.py

# Real mode — requires Docker + a built runner image + ANTHROPIC_API_KEY.
export ANTHROPIC_API_KEY=sk-ant-...
export GG_RELAY_RUNNER_IMAGE=gg-relay-runner:dev
python examples/docker_executor_demo.py
```

### Runner image

`images/gg-relay-runner/Dockerfile` is a multi-stage build:

1. **plugins-builder** — `node:20-bookworm-slim`, clones `gg-plugins`
   at the requested tag and runs `install.sh --profile full`
2. **runtime** — `python:3.11-slim` + tini + Node 20 + `@anthropic-ai/claude-code`
   pinned to `ARG CLAUDE_CLI_VERSION`, with the pre-installed plugins copied in
   and `gg_relay` editable-installed

Built and pushed to GHCR by `.github/workflows/build-runner-image.yml` on
`workflow_dispatch` or `repository_dispatch[gg_plugins_release]`. See
[`images/gg-relay-runner/README.md`](./images/gg-relay-runner/README.md) for
local build / debugging commands.

### Integration tests

```bash
# Docker + API-key gated (skip cleanly when either is absent):
pytest tests/integration/test_docker_executor.py -m "requires_docker and requires_api_key"

# Proxy curl smoke (requires curl on PATH; auto-skips otherwise):
pytest tests/integration/test_proxy_smoke.py -m requires_curl
```

References:
- Plan — [`docs/superpowers/plans/2026-05-22-plan-3-docker-backend-and-wire-transport.md`](./docs/superpowers/plans/2026-05-22-plan-3-docker-backend-and-wire-transport.md)
- Spec sync — §5.4 DockerExecutor, §5.5 WireBridge/Proxy, §6.6 UnixSocketTransport, §6.7 heartbeat frames, §8.4 host proxy
- Docker spike — [`docs/docker-runner-spike.md`](./docs/docker-runner-spike.md)
- Demo source — [`examples/docker_executor_demo.py`](./examples/docker_executor_demo.py)

## License

MIT
