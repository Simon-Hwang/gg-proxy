# CLAUDE.md

This file provides guidance to Claude Code when working in this repository.

## Project Overview

`gg-relay` is a Python middleware service that relays tasks to Claude Code via the official `claude-code-sdk` Python package. It adds observability (OpenTelemetry), IM integration (Feishu/DingTalk/Slack), and a visual dashboard on top.

## Architecture

- **`src/gg_relay/core/`** — Event bus, state machine, domain models (zero external deps)
- **`src/gg_relay/session/`** — SessionManager, SDK client wrapper, crash recovery
- **`src/gg_relay/store/`** — SQLAlchemy Core async + Alembic migrations
- **`src/gg_relay/tracing/`** — OTel subscriber, TracerProvider bootstrap
- **`src/gg_relay/im/`** — IMBackend Protocol, webhook router, IM backends
- **`src/gg_relay/api/`** — FastAPI app, middleware, routers
- **`src/gg_relay/dashboard/`** — Jinja2 + HTMX templates

## Key Commands

```bash
gg-relay serve          # Start the FastAPI server
gg-relay migrate        # Run Alembic migrations
gg-relay check-secrets  # Validate required secrets are present
gg-relay status         # Show active sessions
```

## Design Principles

1. **EventBus is the only fan-out mechanism** — no direct coupling between producers and consumers
2. **All plugin interfaces use `typing.Protocol`** — structural typing, no import coupling
3. **Security is P0** — API key auth, webhook verification, log redaction from day one
4. **Immutability** — frozen dataclasses with immutable containers throughout
5. **`ClaudeSDKClient` exclusively** — never `query()` shorthand

## Integration with gg-plugins

This repo is a sibling to `gg-plugins`. Integration via:
- HTTP API contract (`/api/v1/sessions`, etc.)
- Task-trace JSONL (`~/.claude/metrics/gg-task-trace.jsonl`)
- `RELAY_TRACE_ID` env var for OTel correlation
