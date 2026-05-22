"""gg-relay CLI (typer).

Commands:
- ``serve``         — start the FastAPI app via uvicorn
- ``migrate``       — run Alembic ``upgrade head``
- ``status``        — print active sessions (calls /api/v1/sessions)
- ``check-secrets`` — validate required env vars present
- ``prune``         — delete frames older than ``--older-than``
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from gg_relay.config import Config, missing_required
from gg_relay.store import SessionRepository, make_async_engine

logger = logging.getLogger("gg_relay.cli")

app = typer.Typer(
    name="gg-relay",
    help="Python middleware/relay over Claude Code SDK.",
    no_args_is_help=True,
)


# ── helpers ────────────────────────────────────────────────────────────


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")


def _parse_duration(spec: str) -> timedelta:
    """Accept ``30d`` / ``12h`` / ``45m`` / ``60s`` and return a timedelta.

    Helps keep ``prune --older-than`` ergonomic. Errors out clearly when
    the spec is malformed so operators don't accidentally prune the wrong
    horizon.
    """
    m = _DURATION_RE.match(spec)
    if m is None:
        raise typer.BadParameter(
            f"duration must look like 30d / 12h / 45m / 60s; got {spec!r}"
        )
    n = int(m.group(1))
    unit = m.group(2)
    if unit == "d":
        return timedelta(days=n)
    if unit == "h":
        return timedelta(hours=n)
    if unit == "m":
        return timedelta(minutes=n)
    return timedelta(seconds=n)


def _load_config() -> Config:
    return Config()


# ── commands ───────────────────────────────────────────────────────────


@app.command()
def serve(
    host: Annotated[str, typer.Option("--host", "-h")] = "0.0.0.0",
    port: Annotated[int, typer.Option("--port", "-p")] = 8000,
) -> None:
    """Run the FastAPI server (uvicorn)."""
    import uvicorn

    # Imported lazily so ``check-secrets`` / ``migrate`` / ``prune`` don't
    # require the full FastAPI dependency tree to be importable. mypy can't
    # see the module until Task 7 lands it; the runtime check happens here.
    from gg_relay.api.main import create_app

    cfg = _load_config()
    app_obj = create_app(cfg)
    uvicorn.run(app_obj, host=host, port=port)


@app.command()
def migrate() -> None:
    """Run ``alembic upgrade head`` against the configured database."""
    from alembic import command
    from alembic.config import Config as AlembicConfig

    cfg = _load_config()
    # alembic.ini lives at the repo root in dev; production installs will
    # ship a similar file alongside the wheel. The env.py reads
    # ``RELAY_DATABASE_URL`` directly so we set it here for consistency.
    import os

    os.environ["RELAY_DATABASE_URL"] = cfg.database_url
    alembic_cfg = AlembicConfig("alembic.ini")
    command.upgrade(alembic_cfg, "head")
    typer.echo("migrate: upgrade head OK")


@app.command(name="check-secrets")
def check_secrets() -> None:
    """Validate the production-required env vars are set."""
    try:
        cfg = _load_config()
    except ValidationError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(2) from exc
    missing = missing_required(cfg)
    if missing:
        typer.echo(
            "check-secrets: missing required: " + ", ".join(missing),
            err=True,
        )
        raise typer.Exit(1)
    typer.echo("check-secrets: OK")


@app.command()
def status() -> None:
    """Print active sessions by hitting ``/api/v1/sessions``."""
    import httpx

    cfg = _load_config()
    if not cfg.api_keys:
        typer.echo("status: no api keys configured", err=True)
        raise typer.Exit(1)
    base = cfg.public_base_url or "http://127.0.0.1:8000"
    headers = {"X-API-Key": cfg.api_keys[0].get_secret_value()}
    try:
        r = httpx.get(f"{base}/api/v1/sessions", headers=headers, timeout=5.0)
    except httpx.HTTPError as exc:
        typer.echo(f"status: request failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    if r.status_code != 200:
        typer.echo(f"status: HTTP {r.status_code} {r.text[:200]}", err=True)
        raise typer.Exit(1)
    sessions = r.json().get("sessions", [])
    typer.echo(f"status: {len(sessions)} sessions")
    for s in sessions[:20]:
        typer.echo(
            f"  {s['id']}  {s['status']:11s}  "
            f"submitted={s['submitted_at']}  ended={s.get('ended_at')}"
        )


@app.command()
def prune(
    older_than: Annotated[
        str, typer.Option("--older-than", help="30d / 12h / 45m / 60s")
    ] = "30d",
    dry_run: Annotated[
        bool, typer.Option("--dry-run/--no-dry-run")
    ] = False,
) -> None:
    """Delete frames older than the cutoff."""
    delta = _parse_duration(older_than)
    cutoff = datetime.now(UTC) - delta
    cfg = _load_config()

    async def _run() -> int:
        engine = make_async_engine(cfg.database_url)
        store = SessionRepository(engine)
        try:
            if dry_run:
                return 0
            return await store.prune_frames_older_than(cutoff=cutoff)
        finally:
            await engine.dispose()

    deleted = asyncio.run(_run())
    if dry_run:
        typer.echo(
            f"prune: dry-run only; would delete frames with ts < {cutoff.isoformat()}"
        )
    else:
        typer.echo(
            f"prune: deleted {deleted} frame(s) with ts < {cutoff.isoformat()}"
        )


@app.command(name="recover")
def recover() -> None:
    """Run the startup interrupted-scan once and exit.

    Useful after a crash to mark in-flight sessions as ``interrupted``
    without bringing the full ``serve`` lifespan up.
    """
    from gg_relay.session.recovery import recover_on_startup

    cfg = _load_config()

    async def _run() -> tuple[int, tuple[str, ...]]:
        engine = make_async_engine(cfg.database_url)
        store = SessionRepository(engine)
        try:
            report = await recover_on_startup(store)
            return report.interrupted_count, report.interrupted_ids
        finally:
            await engine.dispose()

    n, ids = asyncio.run(_run())
    typer.echo(f"recover: marked {n} session(s) as interrupted")
    for sid in ids[:20]:
        typer.echo(f"  {sid}")


@app.command(name="version")
def version() -> None:
    """Print the package version."""
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as get_version

    try:
        typer.echo(get_version("gg-relay"))
    except PackageNotFoundError:
        typer.echo("0.1.0")


def main(args: list[str] | None = None) -> int:
    """Entry-point for ``python -m gg_relay.cli`` and tests.

    Returns the process exit code so unit tests can assert on it without
    catching SystemExit themselves.
    """
    del args
    try:
        app()
        return 0
    except SystemExit as exc:
        return int(exc.code or 0)


# Force usage of Path imports so mypy doesn't complain about unused.
_unused: tuple[type, ...] = (Path,)  # noqa: PIE794
