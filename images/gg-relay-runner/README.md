# gg-relay-runner

Per-session container image for the `gg-relay` docker backend
(Plan 3 §6 Task 6).

## What's inside

| Layer | Provides |
|---|---|
| `python:3.11-slim` | Python runtime |
| `tini` (apt) | PID 1 init — signal forwarding, zombie reaping (D3.19) |
| NodeSource Node 20 | `node` for the claude CLI + gg-plugins JS hooks |
| `@anthropic-ai/claude-code@${CLAUDE_CLI_VERSION}` (npm) | The CLI the SDK shells out to (D3.15) |
| `gg-plugins@${GG_PLUGINS_VERSION}` (git tag, `--profile full`) | Baked at build time so per-session start is fast (D3.3 / D3.14) |
| `gg-relay` (`pip install -e .`) | The Python wire runner that `tini` execs into |

Entry: `tini -- python -m gg_relay.session.runner.wire_runner`.

The image runs as non-root **UID 1000** (`gguser`) so the bind-mounted
unix socket created by the host (`chmod 0o666`) is reachable.

## Build args

| Arg | Default | Notes |
|---|---|---|
| `GG_PLUGINS_VERSION` | required | git tag in `gg-plugins` repo |
| `GG_PLUGINS_REPO` | `https://github.com/gg-org/gg-plugins.git` | override for forks / mirrors |
| `GG_PLUGINS_PROFILE` | `full` | `--profile` passed to install.sh (D3.14) |
| `CLAUDE_CLI_VERSION` | `2.1.133` | npm version pin (D3.15) |

## Build locally

From the **repo root** (the build context):

```bash
docker build \
  --build-arg GG_PLUGINS_VERSION=v0.4.2 \
  --build-arg CLAUDE_CLI_VERSION=2.1.133 \
  -f images/gg-relay-runner/Dockerfile \
  -t gg-relay-runner:dev .
```

Apple Silicon hosts that push to GHCR (which is consumed on `linux/amd64`)
should add `--platform linux/amd64`. CI handles this by always running on
`ubuntu-latest`.

## Run for ad-hoc debugging

The wire runner refuses to start without `GG_RELAY_SPEC_JSON` /
`GG_RELAY_SOCKET` / `ANTHROPIC_API_KEY`, so the simplest smoke test is to
shell in and call the CLI directly:

```bash
docker run --rm -it \
  -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \
  --entrypoint /usr/bin/tini \
  gg-relay-runner:dev -- claude --version
```

## Required mount layout (production)

`DockerExecutor` mounts two paths:

| Host | Container | Mode | Why |
|---|---|---|---|
| `/var/run/gg-relay` | `/var/run/gg-relay` | `rw,z` | NDJSON socket created by the host (`{runtime_id}.sock`); the `:z` SELinux mode is required on RHEL/Fedora hosts (D3.5) |
| `${SessionSpec.cwd}` | `/workspace` | `rw,z` | the user-supplied working directory the SDK reads / writes |

Networking is `bridge` with
`--add-host host.docker.internal:host-gateway` so the bundled minimal proxy
on the host is reachable for the claude CLI's HTTPS egress
(D3.7 + spike addendum).

## Image size

Expect 1.5–2 GiB. The `node:20-bookworm-slim` builder + `python:3.11-slim`
runtime + `claude-code` CLI + Plan 3 D3.14's `--profile full` dominate. The
slimming work is out of scope for Plan 3 (Plan 5+ owns it).

## Troubleshooting

- **`AF_UNIX path too long`** on the host when binding the socket: the host
  path must be ≤ 108 chars. `DockerExecutor.DEFAULT_SOCKET_ROOT` is
  `/var/run/gg-relay` for that reason; don't override it with a deep
  pytest tmpdir.
- **Container exits immediately with `missing required env vars: ...`**:
  the wire runner refuses to start without all of `GG_RELAY_SPEC_JSON`,
  `GG_RELAY_SOCKET`, `ANTHROPIC_API_KEY`. DockerExecutor injects the first
  two automatically; the third comes from `SessionRuntimeContext.credentials`.
- **SELinux on host blocks the bind-mount**: confirm the `:z` mount mode
  is being applied (DockerExecutor does this for both mounts) and that the
  host's `/var/run/gg-relay` is labelled `container_file_t`.
