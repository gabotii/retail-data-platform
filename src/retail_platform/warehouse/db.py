"""Database engine and a minimal, checksum-verified SQL migration runner."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from importlib import resources

from sqlalchemy import Engine, create_engine, text

logger = logging.getLogger(__name__)

_MIGRATION_NAME = re.compile(r"V(\d{3})__(\w+)\.sql")
_MIGRATION_LOCK = 4242_0001


def make_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True, pool_size=5, max_overflow=5)


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def available_migrations() -> list[Migration]:
    folder = resources.files("retail_platform.warehouse") / "sql"
    found = []
    for entry in folder.iterdir():
        match = _MIGRATION_NAME.fullmatch(entry.name)
        if match:
            found.append(Migration(match.group(1), match.group(2), entry.read_text("utf-8")))
    return sorted(found, key=lambda m: m.version)


class MigrationDriftError(RuntimeError):
    """An already-applied migration file was edited afterwards."""


def migrate(engine: Engine) -> list[str]:
    """Apply pending migrations in order, each in its own transaction. Returns applied versions."""
    applied_now: list[str] = []
    with engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _MIGRATION_LOCK})
        try:
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS public.schema_migrations ("
                    " version text PRIMARY KEY, name text NOT NULL, checksum text NOT NULL,"
                    " applied_at timestamptz NOT NULL DEFAULT now())"
                )
            )
            conn.commit()
            done: dict[str, str] = {
                str(version): str(checksum)
                for version, checksum in conn.execute(
                    text("SELECT version, checksum FROM public.schema_migrations")
                )
            }
            for migration in available_migrations():
                if migration.version in done:
                    if done[migration.version] != migration.checksum:
                        raise MigrationDriftError(
                            f"migration V{migration.version} changed after being applied"
                        )
                    continue
                driver_conn = conn.connection.driver_connection
                assert driver_conn is not None  # noqa: S101 - narrows the type for mypy
                with driver_conn.cursor() as cur:
                    cur.execute(migration.sql)
                conn.execute(
                    text(
                        "INSERT INTO public.schema_migrations (version, name, checksum)"
                        " VALUES (:v, :n, :c)"
                    ),
                    {"v": migration.version, "n": migration.name, "c": migration.checksum},
                )
                conn.commit()
                applied_now.append(migration.version)
                logger.info("migration applied", extra={"version": migration.version})
        finally:
            conn.rollback()
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _MIGRATION_LOCK})
            conn.commit()
    return applied_now
