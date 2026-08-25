"""Apply the checked-in PostgreSQL migrations under one session lock."""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path
from typing import Any, Sequence


MIGRATION_HISTORY_TABLE = "public.metering_billing_schema_migration"
MIGRATION_LOCK_KEY = 581_642_019_203
MIGRATION_NAME_PATTERN = re.compile(r"^[0-9]{4}_[a-z0-9_]+\.sql$")


class MigrationPlanError(ValueError):
    """Raised when a migration file cannot be applied transactionally."""


class MigrationDriftError(RuntimeError):
    """Raised when an already-applied migration's bytes have changed."""


def _migration_files(migration_directory: Path) -> tuple[Path, ...]:
    """Return the ordered, convention-compliant SQL migration files."""
    paths = tuple(sorted(migration_directory.glob("*.sql")))
    if not paths:
        raise MigrationPlanError(f"no SQL migrations found in {migration_directory}")
    invalid = tuple(path.name for path in paths if not MIGRATION_NAME_PATTERN.fullmatch(path.name))
    if invalid:
        raise MigrationPlanError(f"invalid migration filenames: {', '.join(invalid)}")
    return paths


def _transaction_body(sql_text: str, migration_name: str) -> str:
    """Remove the required outer transaction wrapper for the runner transaction."""
    stripped = sql_text.strip()
    if not stripped.startswith("BEGIN;") or not stripped.endswith("COMMIT;"):
        raise MigrationPlanError(
            f"migration must use a BEGIN/COMMIT wrapper: {migration_name}"
        )
    body = stripped[len("BEGIN;") : -len("COMMIT;")].strip()
    if not body:
        raise MigrationPlanError(f"migration has no statements: {migration_name}")
    return body


def _checksum(sql_text: str) -> str:
    """Return the stable SHA-256 checksum recorded for one migration file."""
    return hashlib.sha256(sql_text.encode("utf-8")).hexdigest()


def apply_migrations(connection: Any, migration_directory: Path) -> tuple[str, ...]:
    """Apply unapplied migrations and return the names applied in this call.

    The history row and each migration statement are committed together. A
    transaction-scoped advisory lock serializes runners without leaving a
    session lock behind after a failed migration.
    """
    paths = _migration_files(migration_directory)
    applied_now: list[str] = []
    with connection.transaction():
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS public.metering_billing_schema_migration (
                migration_name text PRIMARY KEY,
                checksum_sha256 text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
            )
            """
        )
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_KEY,))
        applied = dict(
            connection.execute(
                "SELECT migration_name, checksum_sha256 FROM public.metering_billing_schema_migration"
            ).fetchall()
        )
        for path in paths:
            sql_text = path.read_text(encoding="utf-8")
            checksum = _checksum(sql_text)
            previous_checksum = applied.get(path.name)
            if previous_checksum is not None:
                if previous_checksum != checksum:
                    raise MigrationDriftError(
                        f"migration checksum changed after apply: {path.name}"
                    )
                continue
            connection.execute(_transaction_body(sql_text, path.name))
            connection.execute(
                """
                INSERT INTO public.metering_billing_schema_migration
                    (migration_name, checksum_sha256)
                VALUES (%s, %s)
                """,
                (path.name, checksum),
            )
            applied_now.append(path.name)
    return tuple(applied_now)


def main(arguments: Sequence[str] | None = None) -> int:
    """Apply migrations from the command line using a psycopg connection."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True, help="psycopg connection string")
    parser.add_argument(
        "--migrations",
        type=Path,
        default=Path("database/migrations"),
        help="directory containing ordered SQL migrations",
    )
    options = parser.parse_args(arguments)
    try:
        import psycopg
    except ImportError as error:  # pragma: no cover - packaging smoke covers this boundary
        raise RuntimeError("PostgreSQL support requires psycopg[binary]") from error
    with psycopg.connect(options.dsn) as connection:
        applied = apply_migrations(connection, options.migrations)
    print(f"applied {len(applied)} PostgreSQL migrations")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
