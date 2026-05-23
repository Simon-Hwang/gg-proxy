# OpenTelemetry & Tracing

> Canonical spec: [`superpowers/specs/2026-05-22-sdk-bootstrap-and-runtime-design.md`](superpowers/specs/2026-05-22-sdk-bootstrap-and-runtime-design.md) §"Observability"
> Plan-7 changes: [`superpowers/plans/2026-05-23-plan-7-foundation-polish.md`](superpowers/plans/2026-05-23-plan-7-foundation-polish.md) D7.9 / D7.11 / D7.23

`gg-relay` ships an OTLP exporter wired through the `EventBus`. Spans
are emitted by an out-of-band subscriber (`OtelSubscriber`) so the
session control path never blocks on tracing I/O.

## Quick start (dev)

```bash
cd /path/to/gg-relay
docker compose -f deploy/docker-compose.dev.yml up --build
# Jaeger UI:  http://localhost:16686
# Relay API:  http://localhost:8000
```

The dev compose file ships a `jaegertracing/all-in-one:1.57` sibling
service (collector + query + UI in one container). The relay container
auto-discovers it via `RELAY_OTEL_ENDPOINT=http://jaeger:4317` and
exports spans over OTLP gRPC.

> Note: the all-in-one image is `linux/amd64` only as of 1.57. On
> arm64 Macs Docker Desktop will emulate via Rosetta; `platform` is
> pinned in the compose file so the pull doesn't fail.

## OTel env vars

| Variable | Priority | Default | Notes |
|---|---|---|---|
| `RELAY_OTEL_ENDPOINT` | 1 (canonical) | unset | Plan 7 D7.23 — `RELAY_`-prefixed wins |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 2 (fallback) | unset | Upstream OTel convention |
| `RELAY_OTEL_EXPORTER` | — | `grpc` | One of `grpc` / `http` / `console` |
| `RELAY_OTEL_EXPERIMENTAL_GENAI` | — | `false` | Opt-in for `gen_ai.*` semconv attrs |

When `RELAY_OTEL_ENDPOINT` is unset the `OtelSubscriber` is **not**
wired — the relay still works, you just don't get spans. Set the env
var to enable.

## Span hierarchy (D7.9 / D7.21)

```
relay.session                       (root, per session lifecycle)
├── relay.session.run               (per pause/resume cycle)
│   └── relay.tool_call             (per tool invocation)
└── relay.session.finalize          (on terminal state)
```

Each span carries `session.id` as the canonical OTel semconv attribute
and double-writes the legacy `gg_relay.session_id` attribute for
backwards compatibility with dashboards built against Plan 6.

## Span attributes

| Span | Attr (canonical) | Attr (legacy compat) | Description |
|---|---|---|---|
| `relay.session` | `session.id` | `gg_relay.session_id` | OTel semconv |
| `relay.session` | `gen_ai.system` (opt-in) | — | `claude-code` |
| `relay.session.run` | `session.id` | `gg_relay.session_id` | Inherits from parent |
| `relay.session.run` | `relay.run.reason` | — | `initial` / `resume` |
| `relay.tool_call` | `gen_ai.tool.name` (opt-in) | `gg_relay.tool` | Tool identifier |
| `relay.tool_call` | `relay.hitl.required` | — | bool, whether HITL gated this call |
| `relay.session.finalize` | `relay.session.end_reason` | — | `completed` / `cancelled` / `paused_timeout` / `interrupted` |

### Double-write transition

Plan 7 emits **both** the canonical (`session.id`, `gen_ai.tool.name`)
and legacy (`gg_relay.*`) attributes on every span. The legacy keys
will be cut to single-write in **0.8**; check existing Jaeger/Tempo
queries and dashboards now.

## Experimental gen_ai opt-in

Anthropic / OpenAI / Google have all moved toward the OTel `gen_ai.*`
experimental semconv. We default it **off** so deployments on stable
OTel collectors don't fail attribute validation:

```bash
export RELAY_OTEL_EXPERIMENTAL_GENAI=true
```

When enabled the subscriber tags `relay.session` with
`gen_ai.system=claude-code` and `relay.tool_call` with
`gen_ai.tool.name=<tool>`. Legacy `gg_relay.tool` is still emitted.

## Metrics

`/metrics` exposes the Prometheus scrape endpoint with counters /
gauges for bus drops (`gg_relay_bus_drops_total`), durable-store
failures (`gg_relay_bus_durable_drops_total`), session lifecycle
events, and rate-limit decisions. The metrics router is intentionally
**outside** the OpenAPI schema (it's a Prometheus text response, not
JSON) — see `docs/api.md`.

## Trace correlation with gg-plugins

Plan 7 D7.19 / Task 14 sets `RELAY_TRACE_ID` in the SDK runner's env
so the sibling `gg-plugins` task-trace JSONL can be joined against the
OTel span ID. See `docs/superpowers/specs/2026-05-22-sdk-bootstrap-and-runtime-design.md`
§"Trace correlation" for the join key conventions.

## Cross-references

- Architecture overview: [`architecture.md`](architecture.md)
- API contract: [`api.md`](api.md)
- Cluster (Plan 8+) tracing fan-out: [`cluster.md`](cluster.md)
