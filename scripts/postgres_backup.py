"""Create, verify, and restore custom-format PostgreSQL archives safely."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence
from urllib.parse import parse_qsl, urlsplit


class PostgresBackupError(RuntimeError):
    """Raised when a PostgreSQL archive operation cannot complete safely."""


def _keyword_contains_password(dsn: str) -> bool:
    """Find a password keyword without treating values as connection keys."""
    index = 0
    while True:
        while index < len(dsn) and dsn[index].isspace():
            index += 1
        if index >= len(dsn):
            return False
        key_start = index
        while index < len(dsn) and not dsn[index].isspace() and dsn[index] != "=":
            index += 1
        key = dsn[key_start:index]
        while index < len(dsn) and dsn[index].isspace():
            index += 1
        if index >= len(dsn) or dsn[index] != "=":
            while index < len(dsn) and not dsn[index].isspace():
                index += 1
            continue
        index += 1
        while index < len(dsn) and dsn[index].isspace():
            index += 1
        if index < len(dsn) and dsn[index] in {"'", '"'}:
            quote = dsn[index]
            index += 1
            while index < len(dsn):
                if dsn[index] == "\\":
                    index += 2
                elif dsn[index] == quote:
                    index += 1
                    break
                else:
                    index += 1
            else:
                raise ValueError("unterminated PostgreSQL connection value")
        else:
            while index < len(dsn) and not dsn[index].isspace():
                index += 1
        if key.casefold() == "password":
            return True


def _require_dsn(dsn: str) -> None:
    """Reject absent or password-bearing connection strings."""
    if not isinstance(dsn, str) or not dsn.strip():
        raise PostgresBackupError("a non-empty PostgreSQL connection string is required")
    try:
        parsed_dsn = urlsplit(dsn)
        is_uri = bool(parsed_dsn.scheme)
        uri_contains_password = is_uri and (
            parsed_dsn.password is not None
            or any(
                key.casefold() == "password"
                for key, _value in parse_qsl(parsed_dsn.query, keep_blank_values=True)
            )
        )
        keyword_contains_password = not is_uri and _keyword_contains_password(dsn)
    except ValueError as error:
        raise PostgresBackupError("invalid PostgreSQL connection string") from error
    if uri_contains_password or keyword_contains_password:
        raise PostgresBackupError(
            "PostgreSQL connection strings must omit passwords; use a secret-managed libpq boundary"
        )


def _require_binary(binary: str, operation: str) -> None:
    """Reject an absent client binary before invoking the subprocess boundary."""
    if not isinstance(binary, str) or not binary.strip():
        raise PostgresBackupError(f"{operation} client binary is required")


def _require_archive(archive: Path) -> None:
    """Require a regular archive file before read or restore operations."""
    if not archive.is_file():
        raise PostgresBackupError("backup archive does not exist")


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run one argv-only client command without exposing captured secrets."""
    try:
        return subprocess.run(
            tuple(command),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
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
        raise PostgresBackupError("backup destination already exists")
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
        try:
            os.link(temporary, destination)
        except FileExistsError as error:
            raise PostgresBackupError(
                "backup destination became occupied during publication"
            ) from error
        except OSError as error:
            raise PostgresBackupError("backup archive publication failed") from error
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError as error:
            raise PostgresBackupError(
                "backup failed and temporary cleanup failed"
            ) from error
        raise
    try:
        temporary.unlink(missing_ok=True)
    except OSError as error:
        raise PostgresBackupError(
            "backup published but temporary cleanup failed"
        ) from error
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
            create_backup(options.dsn, options.output, pg_dump_binary=options.pg_dump)
        elif options.operation == "verify":
            verify_backup(options.archive, pg_restore_binary=options.pg_restore)
        else:
            restore_backup(
                options.dsn,
                options.archive,
                pg_restore_binary=options.pg_restore,
                clean=options.clean,
                confirm_destructive_restore=options.confirm_destructive_restore,
            )
    except PostgresBackupError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"{options.operation} complete")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
