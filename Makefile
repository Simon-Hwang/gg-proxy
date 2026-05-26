# gg-relay Makefile — convenience targets that aren't worth a
# dedicated CLI subcommand. Plan 7 Task 4 (D7.10) introduces the three
# Locust load-test profiles; see ``scripts/README.md`` for scenario
# details and the per-profile target QPS table.

LOCUST_HOST ?= http://localhost:8080

.PHONY: load-rest load-dashboard load-sse update-openapi-snapshot actor-label-audit

load-rest:
	locust -f scripts/load_test.py --tags rest -u 100 -r 10 -t 5m --headless --host=$(LOCUST_HOST)

load-dashboard:
	locust -f scripts/load_test.py --tags dashboard -u 50 -r 5 -t 5m --headless --host=$(LOCUST_HOST)

load-sse:
	locust -f scripts/load_test.py --tags sse -u 10 -r 1 -t 5m --headless --host=$(LOCUST_HOST)

# Plan 7 D7.11 — regenerate the committed OpenAPI snapshot.
# Run after any handler / schema / router change; the matching
# integration test (tests/integration/test_openapi_snapshot.py)
# fails on drift and prints this exact command.
update-openapi-snapshot:
	uv run python scripts/dump_openapi.py > docs/openapi.snapshot.json

# Plan v3 §B.6.bis — audit every `manager.submit(...)` / `manager.retry(...)`
# call site against the actor_label-forwarding invariant. Lists every
# call site so a reviewer can confirm none accidentally regressed to
# the v2-Santa-FAIL behavior (omitting actor_label on a path that
# carries per-user credentials). Wired into CI via `pre-commit` and
# checked by hand during code review.
#
# A new call site MUST either:
#   * pass `actor_label=<authenticated identity>` for credential
#     scoping, OR
#   * be in a path where credentials are intentionally absent
#     (background watchdog / system retry) — in which case the
#     reviewer leaves a comment explaining why actor_label is None.
#
# The grep covers both bare `self.submit(` and `manager.submit(` /
# `manager.retry(` patterns so a future refactor that swaps the
# manager variable name still surfaces.
actor-label-audit:
	@echo "── manager.submit / manager.retry call sites (Plan v3 §B.6.bis) ──"
	@rg -n '\b(self|manager)\.(submit|retry)\(' \
		src/gg_relay/api/routers/ \
		src/gg_relay/session/manager.py \
		|| true
	@echo "── confirm each carries actor_label=… (or document why not) ──"
