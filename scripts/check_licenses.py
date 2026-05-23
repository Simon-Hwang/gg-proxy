#!/usr/bin/env python3
"""License gate for tagged releases (Plan 7 Task 3 / D7.1).

Reads ``pip-licenses --format=json`` output from stdin and:
  - exits **1** if any direct dependency reports a copyleft license
    (GPL / AGPL / LGPL family) — incompatible with gg-relay's MIT
    distribution,
  - exits **0** but writes a stderr warning for ``UNKNOWN`` /
    ``UNKNOWN`` results so unclassified packages can be triaged
    without blocking a release,
  - prints a one-line summary plus a per-package table to stdout for
    the GitHub Actions log.

Usage (inside ``release.yml``)::

    pip-licenses --format=json --packages $(...) \\
        | python scripts/check_licenses.py
"""

from __future__ import annotations

import json
import sys
from typing import Final

# pip-licenses normalises common SPDX-ish aliases ("GNU General Public
# License v3 (GPLv3)", "LGPLv2+", ...). A simple substring match against
# the upper-cased name is enough to catch every copyleft variant we care
# about without false positives on "ZPL" or "PIL".
_COPYLEFT_TOKENS: Final = ("AGPL", "LGPL", "GPL")
_UNKNOWN_TOKENS: Final = ("UNKNOWN",)


def _is_copyleft(license_name: str) -> bool:
    upper = license_name.upper()
    return any(tok in upper for tok in _COPYLEFT_TOKENS)


def _is_unknown(license_name: str) -> bool:
    stripped = license_name.strip()
    return not stripped or stripped.upper() in _UNKNOWN_TOKENS


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        sys.stderr.write("check_licenses: empty stdin (did pip-licenses run?)\n")
        return 1
    try:
        records = json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"check_licenses: invalid JSON on stdin: {exc}\n")
        return 1

    if not isinstance(records, list):
        sys.stderr.write(
            f"check_licenses: expected JSON array from pip-licenses, got "
            f"{type(records).__name__}\n"
        )
        return 1

    failures: list[tuple[str, str]] = []
    warnings: list[tuple[str, str]] = []
    ok: list[tuple[str, str]] = []

    for rec in records:
        name = str(rec.get("Name", "<unknown>"))
        lic = str(rec.get("License", ""))
        if _is_copyleft(lic):
            failures.append((name, lic))
        elif _is_unknown(lic):
            warnings.append((name, lic or "<empty>"))
        else:
            ok.append((name, lic))

    print(
        f"check_licenses: {len(ok)} OK, {len(warnings)} unknown, "
        f"{len(failures)} blocked"
    )
    for name, lic in ok:
        print(f"  OK   {name}: {lic}")
    for name, lic in warnings:
        sys.stderr.write(
            f"  WARN {name}: license={lic!r} — please verify manually\n"
        )
    for name, lic in failures:
        sys.stderr.write(
            f"  FAIL {name}: license={lic!r} (copyleft, incompatible with MIT)\n"
        )

    if failures:
        sys.stderr.write(
            f"check_licenses: refusing release; "
            f"{len(failures)} copyleft dep(s) detected\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
