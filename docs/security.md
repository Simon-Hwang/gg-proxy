# Security Operations Guide

This document collects the security-relevant invariants of `gg-relay` and
the operational practices that uphold them.

## 1. P0 invariants

1. **Credentials never persist.** `SessionSubmitRequest.credentials` is
   absorbed into `SessionRuntimeContext` and consumed by the executor
   only; nothing serialises it back to the store or response surface.
2. **Frames are redacted before persistence.** Every frame goes through
   `RedactionEngine.redact_frame()` before reaching the `frames` table or
   the EventBus's downstream subscribers.
3. **The API has a single auth surface.** `X-API-Key` against an
   immutable key-set fixed at process start; the dashboard runs its own
   cookie session.
4. **The Feishu webhook is signed.** Every callback verifies
   HMAC-SHA256 over `timestamp + "\n" + secret`; unsigned or wrong
   signatures return 401 before the payload is touched.
5. **The container runner has no network by default.** Only HTTPS to
   allow-listed hosts via the MinimalProxy; everything else is dropped.

## 2. API key management

API keys are loaded from `RELAY_API_KEYS_RAW` (comma-separated). The
middleware accepts ANY of them, so the rotation flow is:

1. Add a new key to the env (`k-new`), reload the relay process.
2. Update clients to start sending `k-new`.
3. Drain traffic from the old key (audit log + access log greps).
4. Remove the old key from the env, reload again.

There is intentionally no per-key scope or rate-limit in v1; that lives
in Plan 5+ (D4.23). Treat keys as bearer credentials: never echo them in
logs, never check them into git, prefer environment files mounted
read-only.

## 3. Redaction patterns

`RedactionEngine` applies, in order:

- Sensitive-key match (case-insensitive `lower()` check against a
  frozen set including `api_key`, `token`, `secret`, `password`,
  `credentials`, `ANTHROPIC_API_KEY`, and any extras from
  `RELAY_REDACTION_KEYS_RAW`).
- Regex patterns (default: generic `key=value` formats, Anthropic
  `sk-ant-*`, bearer headers, AWS `AKIA*`); operators add deployment-
  specific patterns via `RELAY_REDACTION_PATTERNS_RAW`.

Custom patterns are validated by `re.compile()` at process start; an
invalid regex prevents startup (fail-fast).

## 4. Webhook signature hardening

`verify_feishu_signature` uses `hmac.compare_digest()` to avoid timing
oracles. Operators MUST set `RELAY_FEISHU_WEBHOOK_SECRET` even when
running in test environments; an empty secret bypasses verification
(intended only for unit tests).

For incident response: a leaked `webhook_secret` invalidates only that
backend, not the API surface — rotate it via the Feishu admin console
and restart the relay.

## 5. Filesystem permissions

- `RELAY_DOCKER_SOCKET_ROOT` (default `/var/run/gg-relay`) should be
  `0700` and owned by the relay user. Each session creates a sub-dir
  for its Unix socket; the cleanup happens in `executor.stop()`.
- `RELAY_PROXY_AUDIT_LOG` (default
  `/var/log/gg-relay/proxy-audit.jsonl`) — `0600`, ship to your SIEM.
- `RELAY_INSTALL_DIR_ROOT` (default `/var/lib/gg-relay/installs`) —
  contains the per-session plugin material; `0700`.
- The `gg-plugins` mount is read-only (`ro` in the compose example).

SELinux note: on Fedora/RHEL hosts the bind-mount of
`/var/run/docker.sock` needs the `:Z` flag (or a custom policy module)
to label the socket for the container's context.

## 6. Audit + observability

- All requests pass through `StructuredLoggingMiddleware`: method, path,
  status, duration, request-id. Pair with a structured log shipper.
- Session lifecycle is published on the `session_state` EventBus topic;
  attach additional subscribers for alerting on stuck `running` ages.
- OTel spans expose `gg_relay.session_id` / `gg_relay.req_id` /
  `gg_relay.tool` as attributes — sufficient to pivot a Grafana / Tempo
  trace to a `frames` row.

## 7. Docker socket exposure (Plan 5 D5.12)

The `DockerExecutor` (Plan 3+) needs to talk to a Docker daemon to spawn
per-session runner containers. **How that daemon is exposed to the
gg-relay service container is the single highest-impact security
decision in your deployment**, because the Docker API is a root-
equivalent control plane.

### Threat model

A process with read/write access to `/var/run/docker.sock` (or a TCP
endpoint with the equivalent privileges) can:

* run an arbitrary image bind-mounted to `/host`, granting full root
  filesystem access to the host;
* read or modify any container the daemon manages, including peers
  running with secrets in their environment;
* exfiltrate Docker secrets, certificates, and the daemon's TLS keys.

Treat any container that holds the socket as **functionally privileged**,
regardless of whether it sets `privileged: true`.

### What gg-relay does

| Posture                                                            | Where        |
| ------------------------------------------------------------------ | ------------ |
| Bind-mounts `/var/run/docker.sock` into the service container      | `docker-compose.dev.yml` — **dev only** |
| Runs as non-root (`ggrelay` uid 1000), gains daemon access via `group_add: docker` | both dev & prod images |
| **Does not** bind-mount the socket in production                    | `docker-compose.prod.yml` (D5.6=A) |
| Production uses sysadmin-controlled per-session rootless docker exposed on `/var/run/gg-relay` | see `docs/deployment.md` |
| Loads images only by digest in production (CI publishes `@sha256:…`) | `RELAY_DOCKER_IMAGE` |

### Recommended production deployment

1. **Run dockerd rootless** under a dedicated `dockerd-ggrelay` user.
   The systemd unit listens on a Unix socket under `/var/run/gg-relay/`
   with mode `0660` and group `gg-relay`.
2. **Mount only that directory** into the service container; never
   `/var/run/docker.sock` itself.
3. **Pin runner images by digest.** A digest-pinned `RELAY_DOCKER_IMAGE`
   lets you trust a CI-built tag even if the registry is later
   compromised.
4. **Restrict outbound network from the runner image.** The
   MinimalProxy already enforces an allow-list; runners run with
   `--network=gg-relay-egress` so direct internet egress fails closed.
5. **Audit the proxy log.** `RELAY_PROXY_AUDIT_LOG` records every
   resolved hostname and verdict; ship to your SIEM and alert on `deny`
   spikes.

### What you must NOT do in production

* `privileged: true` on the service container.
* Bind-mount `/var/run/docker.sock` read-only thinking that helps. Read-
  only access to the socket still permits container creation; the
  daemon's API is the wrong granularity for filesystem ACLs.
* Expose dockerd on TCP without mutual TLS. Docker over TCP without
  TLS is unauthenticated; a single port-forward turns the host into a
  rootkit dropbox.
* Run the service container as root with the socket mounted. Even
  inside a sandbox, that grants ring-0 equivalence to anyone who can
  reach the FastAPI port.

### Incident response

If you suspect socket exposure has been abused:

1. Stop the service container; preserve the runner containers for
   forensics (`docker ps -a`, `docker logs`).
2. Rotate every secret in `RELAY_FEISHU_*`, the dashboard cookie key,
   and any API key set issued during the window.
3. Audit `RELAY_PROXY_AUDIT_LOG` for unusual destinations.
4. Inspect `frames` table for rows whose `cwd` or tool names look out-
   of-policy.

## 8. Crash recovery posture

`recover_on_startup` is intentionally conservative (D4.6): any session
left in `running` when the process restarts is marked `interrupted`
with `end_reason=startup_recovery`. We do not auto-resume because:

- Runner containers may not be salvageable after a host reboot.
- Re-running a side-effecting tool call is generally worse than failing
  loudly and letting a human re-submit.

Override in your application layer by polling for `status=interrupted`
and re-submitting with adjusted spec if the workload allows it.
