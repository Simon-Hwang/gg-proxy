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

## License

MIT
