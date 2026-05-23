# gg-relay Makefile — convenience targets that aren't worth a
# dedicated CLI subcommand. Plan 7 Task 4 (D7.10) introduces the three
# Locust load-test profiles; see ``scripts/README.md`` for scenario
# details and the per-profile target QPS table.

LOCUST_HOST ?= http://localhost:8080

.PHONY: load-rest load-dashboard load-sse

load-rest:
	locust -f scripts/load_test.py --tags rest -u 100 -r 10 -t 5m --headless --host=$(LOCUST_HOST)

load-dashboard:
	locust -f scripts/load_test.py --tags dashboard -u 50 -r 5 -t 5m --headless --host=$(LOCUST_HOST)

load-sse:
	locust -f scripts/load_test.py --tags sse -u 10 -r 1 -t 5m --headless --host=$(LOCUST_HOST)
