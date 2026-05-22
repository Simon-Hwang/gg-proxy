# Deployment Guide

This guide covers a single-host production deployment of `gg-relay` using
Docker Compose plus Postgres, an OTel collector, and (optionally) Nginx
as the TLS terminator.

## 1. Prerequisites

- Docker ≥ 24 + Docker Compose v2
- Postgres 15+ (managed or container) — SQLite is dev-only
- A reachable OTel collector endpoint (optional but recommended)
- A Feishu app + webhook secret (optional, only if IM HITL is desired)
- A `gg-plugins` checkout under `/opt/gg-plugins` (or mount your own
  layout — `install.sh` is the only required entrypoint)

## 2. Required environment variables

Set the following before `gg-relay serve`. The full list lives in
`Config` (`src/gg_relay/config.py`).

| Variable | Required for prod? | Purpose |
|---|---|---|
| `RELAY_API_KEYS_RAW` | yes | comma-separated X-API-Key values |
| `RELAY_PUBLIC_BASE_URL` | yes | base URL surfaced to IM cards |
| `RELAY_DASHBOARD_ADMIN_PASSWORD` | yes | admin login |
| `RELAY_DASHBOARD_SESSION_SECRET` | yes | itsdangerous session signer (≥ 32 random bytes) |
| `RELAY_DATABASE_URL` | recommended | e.g. `postgresql+asyncpg://user:pw@db/relay` |
| `RELAY_GG_PLUGINS_HOME` | recommended | path to gg-plugins checkout |
| `RELAY_INSTALL_DIR_ROOT` | recommended | where the assembler materialises plugin trees |
| `RELAY_DOCKER_IMAGE` | recommended | runner image tag |
| `RELAY_DOCKER_SOCKET_ROOT` | recommended | per-session Unix-socket dir |
| `RELAY_OUTBOUND_PROXY_URL` | optional | when set, runners route HTTPS through it (else built-in MinimalProxy) |
| `RELAY_OTEL_ENDPOINT` | optional | OTLP gRPC URL |
| `RELAY_OTEL_EXPORTER` | optional | `grpc` (default), `http`, `console` |
| `RELAY_FEISHU_APP_ID` | optional | Feishu app credentials (all four required when enabled) |
| `RELAY_FEISHU_APP_SECRET` | optional | |
| `RELAY_FEISHU_WEBHOOK_SECRET` | optional | |
| `RELAY_FEISHU_TARGET_CHAT_ID` | optional | |
| `RELAY_REDACTION_PATTERNS_RAW` | optional | extra regex patterns (CSV) added to defaults |
| `RELAY_REDACTION_KEYS_RAW` | optional | extra dict-key names (CSV) treated as sensitive |
| `RELAY_DEFAULT_TIMEOUT_S` | optional | per-session timeout (default 1800) |
| `RELAY_MAX_CONCURRENT` | optional | concurrent running sessions cap (default 10) |
| `RELAY_GRACE_PERIOD_S` | optional | shutdown grace seconds (default 30) |

Validate with::

    gg-relay check-secrets

The command exits non-zero if any `REQUIRED_FOR_PROD` field is unset.

## 3. Example docker-compose.yml

```yaml
version: "3.9"

services:
  db:
    image: postgres:16
    environment:
      POSTGRES_USER: relay
      POSTGRES_PASSWORD: change-me
      POSTGRES_DB: relay
    volumes:
      - pgdata:/var/lib/postgresql/data

  otel-collector:
    image: otel/opentelemetry-collector:0.105.0
    command: ["--config=/etc/otelcol/config.yaml"]
    volumes:
      - ./otelcol.yaml:/etc/otelcol/config.yaml:ro

  relay:
    image: ghcr.io/your-org/gg-relay:latest
    depends_on: [db, otel-collector]
    environment:
      RELAY_API_KEYS_RAW: "k-prod-1,k-prod-2"
      RELAY_PUBLIC_BASE_URL: "https://relay.example.com"
      RELAY_DATABASE_URL: "postgresql+asyncpg://relay:change-me@db/relay"
      RELAY_GG_PLUGINS_HOME: "/opt/gg-plugins"
      RELAY_DOCKER_IMAGE: "ghcr.io/your-org/gg-relay-runner:v0.1"
      RELAY_DASHBOARD_ADMIN_PASSWORD: "${ADMIN_PW}"
      RELAY_DASHBOARD_SESSION_SECRET: "${SESSION_SECRET}"
      RELAY_OTEL_ENDPOINT: "http://otel-collector:4317"
    ports: ["8000:8000"]
    volumes:
      - /opt/gg-plugins:/opt/gg-plugins:ro
      - /var/run/docker.sock:/var/run/docker.sock:ro   # if you use DockerExecutor
    command: ["sh", "-c", "gg-relay migrate && gg-relay serve --host 0.0.0.0"]

  nginx:
    image: nginx:1.27
    depends_on: [relay]
    ports: ["443:443"]
    volumes:
      - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro
      - ./tls:/etc/tls:ro

volumes:
  pgdata:
```

## 4. Feishu setup

1. Create a *Custom App* in https://open.feishu.cn .
2. In **Credentials**, copy `App ID` + `App Secret` → set
   `RELAY_FEISHU_APP_ID` + `RELAY_FEISHU_APP_SECRET`.
3. In **Event Subscriptions**, add the request URL
   `https://relay.example.com/im/feishu/callback`. Feishu sends a
   one-shot URL-verification challenge which the router handles
   automatically.
4. Generate a webhook signing secret in the same panel → set
   `RELAY_FEISHU_WEBHOOK_SECRET`.
5. Find the target chat's `chat_id` (open-api `chats/v1/get` or via the
   admin console) → set `RELAY_FEISHU_TARGET_CHAT_ID`.

The card buttons round-trip a JSON value carrying
`{session_id, req_id, decision}`; the webhook router maps it to
`HITLCoordinator.resolve()`.

## 5. Proxy modes

- **Built-in MinimalProxy** — when `RELAY_OUTBOUND_PROXY_URL` is unset
  the lifespan starts an in-process proxy with an allow-list of
  Anthropic + GitHub hosts. Audit log at
  `RELAY_PROXY_AUDIT_LOG` (default `/var/log/gg-relay/proxy-audit.jsonl`).
- **External proxy** (Squid, OpenResty, etc.) — set
  `RELAY_OUTBOUND_PROXY_URL=http://squid:3128`. Recommended for
  multi-tenant or compliance-sensitive deployments where audit owners
  differ from the relay operator.

## 6. TLS

Use Nginx (or any HTTP/2-capable reverse proxy) to terminate TLS. The
relay listens on plain HTTP because the lifespan needs to own its own
graceful shutdown; the reverse proxy supplies HSTS, OCSP stapling, etc.

## 7. Backups

- Postgres: nightly `pg_dump`. The schema is owned by Alembic; restore
  with `gg-relay migrate` after restoring the dump.
- Audit log: ship to your SIEM (the file is append-only JSONL).
