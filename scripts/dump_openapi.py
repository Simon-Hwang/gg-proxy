#!/usr/bin/env python3
"""Dump the gg-relay OpenAPI spec to stdout (Plan 7 D7.11).

Pairs with :file:`tests/integration/test_openapi_snapshot.py` and the
``make update-openapi-snapshot`` target. The test asserts that the
dumped spec matches the committed snapshot exactly, so any reviewer
who changes a path, method, schema, or response gets a deterministic
drift signal — and a one-line refresh command.

We sort keys and indent with two spaces so the diff stays human-readable
across regenerations, and we seed the minimum env vars
:func:`Config.validate_required_secrets` expects so the dumper works
with a fresh checkout (no ``.env``, no exported keys).
"""
from __future__ import annotations

import json
import os
import sys


def main() -> int:
    # Seed the minimum environment the lifespan would normally provide.
    # ``Config.validate_required_secrets`` only runs at lifespan startup,
    # not at ``create_app`` time, so the snapshot is independent of the
    # operator's local secrets. We still set RELAY_API_KEYS_RAW so the
    # APIKey middleware lights up with a deterministic single test key
    # (Plan 7 D7.26 — labelled key dict).
    os.environ.setdefault("RELAY_API_KEYS_RAW", "test-key")

    from gg_relay.api.main import create_app
    from gg_relay.config import Config

    cfg = Config()
    app = create_app(cfg)
    spec = app.openapi()
    json.dump(spec, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
