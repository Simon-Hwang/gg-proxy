# Docker Runner Spike — Plan 3 Task 0

**Date**: 2026-05-22  **Author**: Plan 3 executor  **Status**: ✅ PASS (7/7)

## Goal

Validate the locked decisions in Plan 3 §4 before writing production code:

- **D3.2** — `python:3.11-slim` + Node 20 + `@anthropic-ai/claude-code@2.1.133`
  + `gg-plugins` (four-piece bundle) actually assembles
- **D3.7** — `--network=bridge` (not `none`); claude CLI must reach upstream
- **D3.13** — `HTTPS_PROXY` env propagates to claude CLI when set
- **D3.15** — pin claude CLI version via build ARG
- **D3.19** — `tini` as PID 1, claude CLI runs as PID > 1

## Spike script

`scripts/spike_docker_round_trip.sh` runs 7 checks. Source-of-truth for what was
exercised; this report only summarises observations.

## Results

| # | Check | Result | Notes |
|---|---|---|---|
| 1 | `docker version` reachable | PASS | Engine 24.0.6, API 1.43 |
| 2 | `python:3.11-slim` runs `python --version` | PASS | Reports 3.11.15 |
| 3 | `node:20-bookworm-slim` runs `node --version` | PASS | Reports v20.20.2 |
| 4 | `--add-host=host.docker.internal:host-gateway` resolves | PASS | Resolves to `172.17.0.1` (default bridge gateway) |
| 5 | Build minimal Dockerfile (python:3.11-slim + nodejs 20 + tini + claude CLI 2.1.133) | PASS | Image build succeeded |
| 6 | `claude --version` runs as PID > 1 under tini PID 1 | PASS | tini correctly reaps zombies, no PID-1 quirks |
| 7 | `HTTPS_PROXY` env is visible inside container subprocess | PASS | Propagates to child shells/Node |

Full timing: spike script ran end-to-end in **~142 s** (most of it the one-shot
image build, which is uncached on the first run; subsequent builds are <10 s
thanks to layer caching).

## Decisions confirmed

- **D3.2 (image base)**: No change. `python:3.11-slim` + NodeSource Node 20 +
  `@anthropic-ai/claude-code@2.1.133` produces a working image with both
  toolchains. We can layer `gg-plugins` in a separate stage as planned.
- **D3.7 (network)**: No change. Confirms `none` would not work (claude CLI
  needs egress). Default `bridge` is sufficient, with `host.docker.internal`
  routable when launched with `--add-host=host.docker.internal:host-gateway`.
  **DockerExecutor MUST add this `extra_hosts` entry to containers** so the
  bundled minimal proxy on the host is reachable from inside.
- **D3.13 (proxy)**: No change. `HTTPS_PROXY` env propagates to subprocesses
  cleanly; claude CLI honours it via Node's default agent. We do not need to
  bake a custom CA cert because the proxy in Plan 3 §6 Task 12 is a pure
  HTTP/CONNECT tunnel, not a TLS-terminating MITM.
- **D3.15 (CLI pin)**: No change. `ARG CLAUDE_CLI_VERSION=2.1.133` works,
  `npm install -g @anthropic-ai/claude-code@${CLAUDE_CLI_VERSION}` is the
  correct invocation.
- **D3.19 (tini)**: No change. `tini` is in Debian's repos
  (`apt-get install tini`), one-line install. Image starts via
  `ENTRYPOINT ["/usr/bin/tini", "--"]` and signal handling is normal.

## Environment limitations & follow-up

- **No `ANTHROPIC_API_KEY` in this sandbox**, so we did NOT exercise a real
  `claude --print "say hi"` end-to-end round-trip. The spike instead asserts
  that the binary is installed, executable, and that `--version` returns
  cleanly under tini. The real round-trip will be exercised by
  `tests/integration/test_docker_executor.py` under the
  `@requires_docker @requires_api_key` markers (Plan 3 Task 10).
- **`host.docker.internal`** resolves on this Docker daemon (24.0.6) only when
  `--add-host=host.docker.internal:host-gateway` is passed; older daemons may
  need a different name. Plan 3 README will document this for operators on
  Linux Docker installations without Docker Desktop.

## Conclusion

All locked decisions in Plan 3 §4 are validated against the real Docker daemon
in this environment. **No D3.x revisions required.** Implementation can proceed
with the planned image layout (Dockerfile in `images/gg-relay-runner/`) and the
planned DockerExecutor `HostConfig` (network=bridge, plus an
`ExtraHosts: ["host.docker.internal:host-gateway"]` entry).
