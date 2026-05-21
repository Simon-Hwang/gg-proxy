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

## License

MIT
