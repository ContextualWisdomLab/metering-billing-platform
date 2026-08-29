"""Create, verify, and restore custom-format PostgreSQL archives safely."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence


class PostgresBackupError(RuntimeError):
    """Raised when a PostgreSQL archive operation cannot complete safely."""


def _require_dsn(dsn: str) -> None:
    """Reject an absent connection string without echoing its value."""
    if not isinstance(dsn, str) or not dsn.strip():
        raise PostgresBackupError("a non-empty PostgreSQL connection string is required")


def _require_binary(binary: str, operation: str) -> None:
    """Reject an absent client binary before invoking the subprocess boundary."""
    if not isinstance(binary, str) or not binary.strip():
        raise PostgresBackupError(f"{operation} client binary is required")


def _require_archive(archive: Path) -> None:
    """Require a regular archive file before read or restore operations."""
    if not archive.is_file():
        raise PostgresBackupError(f"backup archive does not exist: {archive}")


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run one argv-only client command without exposing captured secrets."""
    try:
        return subprocess.run(
            tuple(command),
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise PostgresBackupError(f"PostgreSQL client is unavailable: {command[0]}") from error


def _require_success(result: subprocess.CompletedProcess[str], operation: str) -> None:
    """Convert a client failure into a secret-free operator error."""
    if result.returncode != 0:
        raise PostgresBackupError(
            f"{operation} failed with exit status {result.returncode}"
        )


def create_backup(
    dsn: str,
    output_path: Path,
    *,
    pg_dump_binary: str = "pg_dump",
) -> Path:
    """Create one non-overwriting custom archive and atomically publish it."""
    _require_dsn(dsn)
    _require_binary(pg_dump_binary, "pg_dump")
    destination = Path(output_path)
    if destination.exists():
        raise PostgresBackupError(f"backup destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".partial", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        result = _run(
            (
                pg_dump_binary,
                "--format=custom",
                "--no-owner",
                "--file",
                str(temporary),
                dsn,
            )
        )
        _require_success(result, "pg_dump")
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise PostgresBackupError("pg_dump produced an empty archive")
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def verify_backup(
    archive_path: Path,
    *,
    pg_restore_binary: str = "pg_restore",
) -> Path:
    """Validate a custom archive by listing its catalog without connecting."""
    _require_binary(pg_restore_binary, "pg_restore")
    archive = Path(archive_path)
    _require_archive(archive)
    result = _run((pg_restore_binary, "--list", str(archive)))
    _require_success(result, "pg_restore archive verification")
    return archive


def restore_backup(
    dsn: str,
    archive_path: Path,
    *,
    pg_restore_binary: str = "pg_restore",
    clean: bool = False,
    confirm_destructive_restore: bool = False,
) -> Path:
    """Restore a verified archive, requiring explicit confirmation for cleanup."""
    _require_dsn(dsn)
    _require_binary(pg_restore_binary, "pg_restore")
    archive = verify_backup(archive_path, pg_restore_binary=pg_restore_binary)
    if clean and not confirm_destructive_restore:
        raise PostgresBackupError("--clean requires --confirm-destructive-restore")
    command = [
        pg_restore_binary,
        "--exit-on-error",
        "--single-transaction",
        "--no-owner",
        "--dbname",
        dsn,
    ]
    if clean:
        command.extend(("--clean", "--if-exists"))
    command.append(str(archive))
    _require_success(_run(command), "pg_restore")
    return archive


def main(arguments: Sequence[str] | None = None) -> int:
    """Run the archive operation selected by the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)

    backup_parser = subparsers.add_parser("backup", help="create a custom archive")
    backup_parser.add_argument("--dsn", required=True)
    backup_parser.add_argument("--output", type=Path, required=True)
    backup_parser.add_argument("--pg-dump", default="pg_dump")

    verify_parser = subparsers.add_parser("verify", help="verify an archive catalog")
    verify_parser.add_argument("--archive", type=Path, required=True)
    verify_parser.add_argument("--pg-restore", default="pg_restore")

    restore_parser = subparsers.add_parser("restore", help="restore a custom archive")
    restore_parser.add_argument("--dsn", required=True)
    restore_parser.add_argument("--archive", type=Path, required=True)
    restore_parser.add_argument("--pg-restore", default="pg_restore")
    restore_parser.add_argument("--clean", action="store_true")
    restore_parser.add_argument("--confirm-destructive-restore", action="store_true")

    options = parser.parse_args(arguments)
    try:
        if options.operation == "backup":
            archive = create_backup(options.dsn, options.output, pg_dump_binary=options.pg_dump)
        elif options.operation == "verify":
            archive = verify_backup(options.archive, pg_restore_binary=options.pg_restore)
        else:
            archive = restore_backup(
                options.dsn,
                options.archive,
                pg_restore_binary=options.pg_restore,
                clean=options.clean,
                confirm_destructive_restore=options.confirm_destructive_restore,
            )
    except PostgresBackupError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"{options.operation} complete: {archive}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
