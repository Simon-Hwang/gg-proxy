"""Alembic env.py — async SQLAlchemy migration runner.

Follows the canonical async template from SQLAlchemy 2.0 docs. The DB URL is
read from the ``RELAY_DATABASE_URL`` env (falling back to
``sqlalchemy.url`` in ``alembic.ini``, which we leave blank in source control
so secrets never check in).
"""
from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from gg_relay.store.schema import metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

env_url = os.environ.get("RELAY_DATABASE_URL")
if env_url:
    config.set_main_option("sqlalchemy.url", env_url)

target_metadata = metadata


def _run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section, {})
    if not section.get("sqlalchemy.url"):
        section["sqlalchemy.url"] = "sqlite+aiosqlite:///./relay.db"
    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_run_migrations)
    await connectable.dispose()


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url") or (
        "sqlite+aiosqlite:///./relay.db"
    )
    context.configure(
        url=url, target_metadata=target_metadata, literal_binds=True
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
