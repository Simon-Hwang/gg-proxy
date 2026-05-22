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

## License

MIT
