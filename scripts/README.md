# `scripts/` — operator + load-test helpers

This directory holds short helper scripts that aren't worth shipping as
top-level CLI commands. The bulk of day-to-day operations are exposed
through `gg-relay <subcommand>` (see `gg-relay --help`).

Currently shipped helpers:

| Script                                | Purpose                                                        |
| ------------------------------------- | -------------------------------------------------------------- |
| `check_licenses.py`                   | Plan 7 Task 1 — fail on GPL/AGPL deps, warn on unknown        |
| `check_version_sync.py`               | Plan 7 — guard pyproject ↔ tag ↔ CHANGELOG version parity     |
| `load_test.py`                        | Plan 7 Task 4 (D7.10) — Locust profiles (see below)           |
| `spike_docker_round_trip.sh`          | Plan 3 — Docker executor round-trip spike                     |
| `spike_sdk_*.py`                      | Plan 6 — claude-code-sdk pause / resume / ordering spikes     |

---

## Load testing (`load_test.py`)

Three Locust user profiles live in `scripts/load_test.py`. They are
selectable via `--tags`, so a single file backs three distinct `make`
targets — one per traffic shape. The profile design intentionally
mirrors three production-shaped workloads:

* `rest` — SDK-client traffic (submit + poll)
* `dashboard` — operator UI traffic (HTMX Kanban polling)
* `sse` — **best-effort** SSE stream pressure (see caveat below)

### Install

Locust is **not** part of the default or `[dev]` dependencies — it is a
heavy install pulled in only on demand:

```bash
pip install -e '.[loadtest]'
# or with uv:
uv sync --extra loadtest
```

`[loadtest]` is deliberately excluded from `[all]` and from CI (locked
in by `tests/integration/test_ci_workflow_extras_parity.py`).

### Run

A running `gg-relay serve` on `http://localhost:8080` is the default
target. Override with `LOCUST_HOST=...` for a remote host.

```bash
make load-rest        # uses scripts/load_test.py --tags rest
make load-dashboard   # uses scripts/load_test.py --tags dashboard
make load-sse         # uses scripts/load_test.py --tags sse
```

### Scenario table

| Profile     | Users (`-u`) | Spawn rate (`-r`) | Duration (`-t`) | Wait time    | Target shape                                                                            |
| ----------- | -----------: | ----------------: | --------------- | ------------ | --------------------------------------------------------------------------------------- |
| `rest`      |          100 |              10/s | 5 min           | 1–3 s        | ~40–80 req/s sustained: `POST /api/v1/sessions` + `GET /api/v1/sessions/{sid}` per task |
| `dashboard` |           50 |               5/s | 5 min           | 3–7 s        | ~7–15 req/s sustained: `GET /dashboard/kanban` (HTMX 5 s poll equivalent)               |
| `sse`       |           10 |               1/s | 5 min           | 1–2 s        | 10 concurrent SSE streams holding 5 s each (≈ 10 active streams steady-state)           |

### Environment variables

All optional — sensible defaults wired in for a `docker compose` local stack:

| Variable                      | Default       | Used by                |
| ----------------------------- | ------------- | ---------------------- |
| `RELAY_API_KEY`               | `test-key`    | `RESTUser`, `SSEUser`  |
| `RELAY_DASHBOARD_USER`        | `admin`       | `DashboardUser`        |
| `RELAY_DASHBOARD_PASSWORD`    | `admin`       | `DashboardUser`        |
| `RELAY_LOADTEST_EXECUTOR`     | `inprocess`   | session submit payload |
| `LOCUST_HOST`                 | `http://localhost:8080` | Makefile targets       |

### Fixture session

`@events.test_start` creates **one** shared session against the host
before the swarm starts. The `sse` profile streams from this fixture
(no per-user session creation); the `rest` profile still creates a new
session per task to exercise the submit path. If fixture creation
fails the `sse` profile no-ops cleanly while `rest` / `dashboard`
remain unaffected.

### Caveats

* **SSE profile is best-effort.** Locust has no native SSE statistics.
  We open the stream, sleep 5 seconds, then mark the response a
  success. Use it to apply pressure to the `/events` endpoint — NOT to
  measure end-to-end SSE delivery latency. For real SSE assertions,
  use the dedicated integration tests in `tests/integration/test_sse_*.py`.

* **`executor=inprocess`** is the default for the fixture and `rest`
  submits to keep the load on the API surface rather than the Docker
  runtime. Override with `RELAY_LOADTEST_EXECUTOR=docker` if you want
  to exercise the Docker pool too — and pre-warm it first.

* **Dashboard login uses the configured admin password.** Make sure
  `RELAY_DASHBOARD_PASSWORD` matches the running server's
  `dashboard_admin_password`, otherwise every `DashboardUser` will
  loop on a 401.
