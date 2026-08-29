"""Create and verify a PostgreSQL custom-format backup rehearsal."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Sequence


MANIFEST_VERSION = 1
BACKUP_FORMAT = "custom"
DEFAULT_SOURCE_DSN_ENV = "METERING_BILLING_POSTGRES_DSN"
DEFAULT_TARGET_DSN_ENV = "METERING_BILLING_RESTORE_DSN"
_ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SYSTEM_PATH_ALIASES = frozenset({Path("/tmp"), Path("/var")})
_ROW_COUNT_KEYS = (
    "migration_history_count",
    "tenant_account_count",
    "usage_event_count",
)
_ROW_COUNTS_QUERY = """
SELECT json_build_object(
    'migration_history_count', (SELECT count(*) FROM public.metering_billing_schema_migration),
    'tenant_account_count', (SELECT count(*) FROM billing_core.tenant_account),
    'usage_event_count', (SELECT count(*) FROM billing_core.usage_event)
)::text
""".strip()


class BackupRestoreError(RuntimeError):
    """Raised when backup or restore evidence cannot be completed safely."""


def _dsn_from_environment(environment_name: str, environ: dict[str, str] | None = None) -> str:
    """Return a non-empty DSN from an approved environment-variable name."""
    if _ENVIRONMENT_NAME.fullmatch(environment_name) is None:
        raise BackupRestoreError("DSN environment name must be uppercase snake_case")
    values = os.environ if environ is None else environ
    dsn = values.get(environment_name, "").strip()
    if not dsn:
        raise BackupRestoreError(f"required DSN environment variable is empty: {environment_name}")
    return _validated_dsn(dsn)


def _validated_dsn(dsn: str) -> str:
    """Reject empty DSNs and inline passwords at every public boundary."""
    if not isinstance(dsn, str) or not dsn.strip():
        raise BackupRestoreError("PostgreSQL DSN must be non-empty")
    if re.search(r"(?i)(?:^|\s)password\s*=", dsn) or re.search(
        r"(?i)://[^/]*:[^@]*@", dsn
    ):
        raise BackupRestoreError(
            "DSNs must not contain inline passwords; use libpq secret configuration"
        )
    return dsn.strip()


def _require_safe_path(path: Path, *, must_exist: bool = False) -> None:
    """Reject symlinked paths and missing or non-directory parents."""
    if path.is_symlink():
        raise BackupRestoreError(f"artifact path must not be a symlink: {path}")
    for parent in path.parents:
        if parent.is_symlink() and parent not in _SYSTEM_PATH_ALIASES:
            raise BackupRestoreError(f"artifact parent must not be a symlink: {parent}")
    if not path.parent.is_dir():
        raise BackupRestoreError(f"artifact parent directory is unavailable: {path.parent}")
    if must_exist and (not path.is_file() or path.is_symlink()):
        raise BackupRestoreError(f"backup artifact is unavailable: {path}")


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run one external PostgreSQL command without exposing its output."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as error:
        raise BackupRestoreError(f"required PostgreSQL command is unavailable: {command[0]}") from error
    if result.returncode != 0:
        raise BackupRestoreError(f"PostgreSQL command failed: {command[0]} (exit {result.returncode})")
    return result


def _read_row_counts(dsn: str) -> dict[str, int]:
    """Read stable domain row counts through a no-side-effect psql query."""
    result = _run(
        (
            "psql",
            "--no-psqlrc",
            "--tuples-only",
            "--no-align",
            "--set=ON_ERROR_STOP=1",
            f"--command={_ROW_COUNTS_QUERY}",
            f"--dbname={dsn}",
        )
    )
    try:
        decoded = json.loads(result.stdout.strip())
    except json.JSONDecodeError as error:
        raise BackupRestoreError("psql row-count output is not valid JSON") from error
    if not isinstance(decoded, dict) or set(decoded) != set(_ROW_COUNT_KEYS):
        raise BackupRestoreError("psql row-count output has an unexpected shape")
    counts: dict[str, int] = {}
    for key in _ROW_COUNT_KEYS:
        value = decoded[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise BackupRestoreError("psql row-count output contains an invalid count")
        counts[key] = value
    return counts


def _sha256(path: Path) -> str:
    """Return the SHA-256 digest of one regular backup artifact."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise BackupRestoreError(f"cannot read backup artifact: {path}") from error
    return digest.hexdigest()


def _manifest_path(backup_path: Path, manifest_path: Path | None) -> Path:
    """Return the explicit manifest path or the adjacent default path."""
    return backup_path.with_name(backup_path.name + ".manifest.json") if manifest_path is None else manifest_path


def _manifest(backup_path: Path, digest: str, row_counts: dict[str, int]) -> dict[str, Any]:
    """Build the secret-free manifest written beside a backup artifact."""
    return {
        "manifest_version": MANIFEST_VERSION,
        "backup_format": BACKUP_FORMAT,
        "backup_file": backup_path.name,
        "backup_sha256": digest,
        "row_counts": row_counts,
    }


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    """Write one manifest without replacing an existing evidence file."""
    try:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
    except FileExistsError as error:
        raise BackupRestoreError(f"manifest already exists: {path}") from error
    except OSError as error:
        raise BackupRestoreError(f"cannot write backup manifest: {path}") from error


def _load_manifest(path: Path, backup_path: Path) -> dict[str, Any]:
    """Load and validate a closed manifest for the requested backup."""
    _require_safe_path(path, must_exist=True)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BackupRestoreError(f"backup manifest is unreadable: {path}") from error
    required = {"manifest_version", "backup_format", "backup_file", "backup_sha256", "row_counts"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise BackupRestoreError("backup manifest has an unexpected shape")
    if payload["manifest_version"] != MANIFEST_VERSION or payload["backup_format"] != BACKUP_FORMAT:
        raise BackupRestoreError("backup manifest version or format is unsupported")
    if payload["backup_file"] != backup_path.name:
        raise BackupRestoreError("backup manifest does not describe the requested file")
    digest = payload["backup_sha256"]
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise BackupRestoreError("backup manifest contains an invalid digest")
    row_counts = payload["row_counts"]
    if not isinstance(row_counts, dict) or set(row_counts) != set(_ROW_COUNT_KEYS):
        raise BackupRestoreError("backup manifest contains invalid row counts")
    for value in row_counts.values():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise BackupRestoreError("backup manifest contains an invalid row count")
    return payload


def create_backup(
    source_dsn: str,
    backup_path: Path,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Create a custom-format backup and record source counts atomically."""
    source_dsn = _validated_dsn(source_dsn)
    _require_safe_path(backup_path)
    manifest = _manifest_path(backup_path, manifest_path)
    _require_safe_path(manifest)
    if backup_path.exists():
        raise BackupRestoreError(f"backup artifact already exists: {backup_path}")
    if manifest.exists():
        raise BackupRestoreError(f"manifest already exists: {manifest}")
    row_counts = _read_row_counts(source_dsn)
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{backup_path.name}.", suffix=".tmp", dir=backup_path.parent
        )
        os.close(descriptor)
        _run(
            (
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                f"--file={temporary_name}",
                f"--dbname={source_dsn}",
            )
        )
        try:
            os.link(temporary_name, backup_path)
        except FileExistsError as error:
            raise BackupRestoreError(f"backup artifact already exists: {backup_path}") from error
        digest = _sha256(backup_path)
        payload = _manifest(backup_path, digest, row_counts)
        _write_manifest(manifest, payload)
        return payload
    except OSError as error:
        raise BackupRestoreError(f"cannot finalize backup artifact: {backup_path}") from error
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            except OSError as error:
                raise BackupRestoreError("cannot clean temporary backup artifact") from error


def restore_and_verify(
    target_dsn: str,
    backup_path: Path,
    manifest_path: Path | None = None,
    *,
    target_is_disposable: bool = False,
) -> dict[str, Any]:
    """Restore into an explicitly disposable database and compare row counts."""
    target_dsn = _validated_dsn(target_dsn)
    if not target_is_disposable:
        raise BackupRestoreError("restore requires --target-is-disposable")
    _require_safe_path(backup_path, must_exist=True)
    manifest = _load_manifest(_manifest_path(backup_path, manifest_path), backup_path)
    digest = _sha256(backup_path)
    if digest != manifest["backup_sha256"]:
        raise BackupRestoreError("backup artifact digest does not match its manifest")
    _run(
        (
            "pg_restore",
            "--exit-on-error",
            "--single-transaction",
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "--clean",
            "--if-exists",
            f"--dbname={target_dsn}",
            str(backup_path),
        )
    )
    restored_counts = _read_row_counts(target_dsn)
    if restored_counts != manifest["row_counts"]:
        raise BackupRestoreError("restored PostgreSQL row counts do not match the manifest")
    return {
        "status": "verified",
        "backup_sha256": digest,
        "row_counts": restored_counts,
    }


def main(arguments: Sequence[str] | None = None) -> int:
    """Run one explicit create or restore-and-verify rehearsal."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("create", "restore-verify"), required=True)
    parser.add_argument("--backup-path", type=Path, required=True)
    parser.add_argument("--manifest-path", type=Path)
    parser.add_argument("--source-dsn-env", default=DEFAULT_SOURCE_DSN_ENV)
    parser.add_argument("--target-dsn-env", default=DEFAULT_TARGET_DSN_ENV)
    parser.add_argument("--target-is-disposable", action="store_true")
    options = parser.parse_args(arguments)
    try:
        if options.mode == "create":
            result = create_backup(
                _dsn_from_environment(options.source_dsn_env),
                options.backup_path,
                options.manifest_path,
            )
        else:
            result = restore_and_verify(
                _dsn_from_environment(options.target_dsn_env),
                options.backup_path,
                options.manifest_path,
                target_is_disposable=options.target_is_disposable,
            )
    except BackupRestoreError as error:
        print(f"backup_restore_error: {error}")
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
