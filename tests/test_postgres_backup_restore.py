"""Tests for the explicit PostgreSQL backup/restore rehearsal boundary."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import backup_restore_postgres as backup


class PostgresBackupRestoreTests(unittest.TestCase):
    """Exercise safe artifact, command, manifest, and verification branches."""

    class SnapshotStream:
        """Small text stream double for the interactive psql snapshot."""

        def __init__(self, output: str = "") -> None:
            self.output = output
            self.writes: list[str] = []
            self.closed = False

        def write(self, value: str) -> int:
            self.writes.append(value)
            return len(value)

        def flush(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

        def readline(self) -> str:
            return self.output

    class SnapshotProcess:
        """Small process double that keeps the snapshot transaction open."""

        def __init__(self, output: str, poll_result: int | None = None) -> None:
            self.stdin = PostgresBackupRestoreTests.SnapshotStream()
            self.stdout = PostgresBackupRestoreTests.SnapshotStream(output)
            self.poll_result = poll_result
            self.wait_result = 0

        def poll(self) -> int | None:
            return self.poll_result

        def wait(self) -> int:
            return self.wait_result

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.counts = {
            "migration_history_count": 4,
            "tenant_account_count": 2,
            "usage_event_count": 9,
        }
        self.popen_commands: list[tuple[str, ...]] = []
        self.snapshot_processes: list[PostgresBackupRestoreTests.SnapshotProcess] = []
        self.run_commands: list[tuple[str, ...]] = []
        self.popen_patcher = patch.object(backup.subprocess, "Popen", side_effect=self.fake_popen)
        self.popen_patcher.start()
        self.addCleanup(self.popen_patcher.stop)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def completed(self, stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
        """Return one fake completed subprocess result."""
        return subprocess.CompletedProcess([], returncode, stdout, "secret-free stderr")

    def fake_runner(self, command: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
        """Emulate psql, pg_dump, and pg_restore without a live database."""
        self.run_commands.append(command)
        if command[0] == "psql":
            return self.completed(json.dumps(self.counts))
        if command[0] == "pg_dump":
            output = next(item.removeprefix("--file=") for item in command if item.startswith("--file="))
            Path(output).write_bytes(b"custom-format-backup")
        return self.completed()

    def fake_popen(self, command: tuple[str, ...], **_: object) -> SnapshotProcess:
        """Emulate the long-lived psql snapshot transaction."""
        self.popen_commands.append(command)
        process = self.SnapshotProcess(f"snapshot-token\t{json.dumps(self.counts)}\n")
        self.snapshot_processes.append(process)
        return process

    def create_artifact(self, stem: str = "billing") -> tuple[Path, Path]:
        """Create a valid test artifact and return its paths."""
        backup_path = self.root / f"{stem}.dump"
        manifest_path = self.root / f"{stem}.manifest.json"
        with patch.object(backup.subprocess, "run", side_effect=self.fake_runner):
            backup.create_backup("dbname=source", backup_path, manifest_path)
        return backup_path, manifest_path

    def test_dsn_boundaries(self) -> None:
        """DSN names and inline credentials are rejected."""
        with self.assertRaisesRegex(backup.BackupRestoreError, "uppercase"):
            backup._dsn_from_environment("lower_case", {"lower_case": "dbname=x"})
        with self.assertRaisesRegex(backup.BackupRestoreError, "empty"):
            backup._dsn_from_environment("DB_NAME", {})
        with self.assertRaisesRegex(backup.BackupRestoreError, "inline passwords"):
            backup._dsn_from_environment("DB_NAME", {"DB_NAME": "dbname=x password=secret"})
        with self.assertRaisesRegex(backup.BackupRestoreError, "inline passwords"):
            backup._dsn_from_environment("DB_NAME", {"DB_NAME": "postgresql://u:p@host/db"})
        with self.assertRaisesRegex(backup.BackupRestoreError, "inline passwords"):
            backup._dsn_from_environment("DB_NAME", {"DB_NAME": "postgresql://u@host/db?password=secret"})
        with self.assertRaisesRegex(backup.BackupRestoreError, "malformed"):
            backup._validated_dsn("postgresql://u@[broken/db")
        self.assertEqual("dbname=x", backup._dsn_from_environment("DB_NAME", {"DB_NAME": " dbname=x "}))
        with self.assertRaisesRegex(backup.BackupRestoreError, "non-empty"):
            backup._validated_dsn("")
        with self.assertRaisesRegex(backup.BackupRestoreError, "non-empty"):
            backup._validated_dsn(None)  # type: ignore[arg-type]

    def test_safe_paths_and_manifest_path(self) -> None:
        """Artifact paths reject missing parents and symlink components."""
        with self.assertRaisesRegex(backup.BackupRestoreError, "parent directory"):
            backup._require_safe_path(self.root / "missing" / "file")
        with self.assertRaisesRegex(backup.BackupRestoreError, "unavailable"):
            backup._require_safe_path(self.root / "missing", must_exist=True)
        target = self.root / "target"
        target.write_text("x", encoding="utf-8")
        link = self.root / "link"
        link.symlink_to(target)
        with self.assertRaisesRegex(backup.BackupRestoreError, "symlink"):
            backup._require_safe_path(link)
        parent_link = self.root / "parent-link"
        parent_link.symlink_to(self.root / "does-not-exist", target_is_directory=True)
        with self.assertRaisesRegex(backup.BackupRestoreError, "symlink"):
            backup._require_safe_path(parent_link / "artifact")
        self.assertEqual(self.root / "billing.dump.manifest.json", backup._manifest_path(self.root / "billing.dump", None))
        self.assertEqual(target, backup._manifest_path(self.root / "billing.dump", target))

    def test_command_and_row_count_failures(self) -> None:
        """External command and count output failures fail closed without stderr."""
        with patch.object(backup.subprocess, "run", side_effect=OSError("secret")):
            with self.assertRaisesRegex(backup.BackupRestoreError, "unavailable"):
                backup._run(("psql",))
        with patch.object(backup.subprocess, "run", return_value=self.completed(returncode=1)):
            with self.assertRaisesRegex(backup.BackupRestoreError, "exit 1"):
                backup._run(("psql",))
        for output, message in (
            ("not-json", "valid JSON"),
            ("{}", "unexpected shape"),
            (json.dumps({**self.counts, "tenant_account_count": True}), "invalid count"),
        ):
            with patch.object(backup.subprocess, "run", return_value=self.completed(output)):
                with self.assertRaisesRegex(backup.BackupRestoreError, message):
                    backup._read_row_counts("dbname=test")
        with patch.object(backup.subprocess, "run", return_value=self.completed(json.dumps(self.counts))):
            self.assertEqual(self.counts, backup._read_row_counts("dbname=test"))

    def test_snapshot_session_failures_close_without_leaking_output(self) -> None:
        """Snapshot startup and cleanup failures are reported without command output."""
        with patch.object(backup.subprocess, "Popen", side_effect=OSError("secret")):
            with self.assertRaisesRegex(backup.BackupRestoreError, "unavailable"):
                backup._open_snapshot_session("dbname=test")
        with patch.object(backup.subprocess, "Popen", side_effect=backup.BackupRestoreError("bad")):
            with self.assertRaisesRegex(backup.BackupRestoreError, "bad"):
                backup._open_snapshot_session("dbname=test")

        missing_stdin = self.SnapshotProcess("snapshot-token\t{}\n")
        missing_stdin.stdin = None
        with patch.object(backup.subprocess, "Popen", return_value=missing_stdin):
            with self.assertRaisesRegex(backup.BackupRestoreError, "streams"):
                backup._open_snapshot_session("dbname=test")

        invalid_counts = {**self.counts, "usage_event_count": True}
        for output, message in (("not-snapshot", "invalid"), (f"snapshot-token\t{json.dumps(invalid_counts)}\n", "invalid count")):
            process = self.SnapshotProcess(output)
            with patch.object(backup.subprocess, "Popen", return_value=process):
                with self.assertRaisesRegex(backup.BackupRestoreError, message):
                    backup._open_snapshot_session("dbname=test")

        broken_stream = self.SnapshotProcess("")
        broken_stream.stdin.write = lambda _: (_ for _ in ()).throw(OSError("write"))
        with patch.object(backup.subprocess, "Popen", return_value=broken_stream):
            with self.assertRaisesRegex(backup.BackupRestoreError, "unavailable"):
                backup._open_snapshot_session("dbname=test")

        exited = self.SnapshotProcess("snapshot-token\t{}\n", poll_result=1)
        with patch.object(backup.subprocess, "Popen", return_value=exited):
            with self.assertRaisesRegex(backup.BackupRestoreError, "exited"):
                backup._open_snapshot_session("dbname=test")

        process = self.SnapshotProcess("")
        process.stdin.write = lambda _: (_ for _ in ()).throw(OSError("write"))
        process.wait = lambda: (_ for _ in ()).throw(OSError("wait"))
        backup._close_snapshot_session(process)

        process = self.SnapshotProcess("")
        process.stdin = None
        backup._close_snapshot_session(process)

    def test_create_success_and_existing_targets(self) -> None:
        """Create records counts and never replaces an existing artifact."""
        backup_path, manifest_path = self.create_artifact("first")
        self.assertTrue(backup_path.is_file())
        self.assertIn("--snapshot=snapshot-token", self.run_commands[-1])
        self.assertIn(
            "BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY;",
            self.snapshot_processes[-1].stdin.writes[0],
        )
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(self.counts, payload["row_counts"])
        backup_path.write_bytes(b"tampered")
        with self.assertRaisesRegex(backup.BackupRestoreError, "existing backup artifact digest"):
            backup.create_backup("dbname=source", backup_path, manifest_path)
        with patch.object(backup.subprocess, "run", side_effect=self.fake_runner):
            with self.assertRaisesRegex(backup.BackupRestoreError, "artifact already exists"):
                backup.create_backup("dbname=source", backup_path, self.root / "other.json")
            with self.assertRaisesRegex(backup.BackupRestoreError, "manifest already exists"):
                backup.create_backup("dbname=source", self.root / "other.dump", manifest_path)

        fifo_path = self.root / "fifo.dump"
        fifo_manifest = self.root / "fifo.dump.manifest.json"
        os.mkfifo(fifo_path)
        fifo_manifest.write_text(
            json.dumps(backup._manifest(fifo_path, "0" * 64, self.counts)),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(backup.BackupRestoreError, "unavailable"):
            backup.create_backup("dbname=source", fifo_path, fifo_manifest)

    def test_create_failures_clean_up_temp_file(self) -> None:
        """Database and finalization failures do not leave owned temporary files."""
        backup_path = self.root / "billing.dump"
        with patch.object(backup.subprocess, "run", return_value=self.completed(returncode=1)):
            with self.assertRaisesRegex(backup.BackupRestoreError, "exit 1"):
                backup.create_backup("dbname=source", backup_path)
        with patch.object(backup.subprocess, "run", return_value=self.completed(returncode=1)), patch.object(
            backup.os, "unlink", side_effect=PermissionError
        ):
            with self.assertRaisesRegex(backup.BackupRestoreError, "clean temporary"):
                backup.create_backup("dbname=source", backup_path)
        with patch.object(backup.subprocess, "run", side_effect=self.fake_runner), patch.object(
            backup.os, "link", side_effect=FileExistsError
        ):
            with self.assertRaisesRegex(backup.BackupRestoreError, "artifact already exists"):
                backup.create_backup("dbname=source", backup_path)
        with patch.object(backup.subprocess, "run", side_effect=self.fake_runner), patch.object(
            backup.os, "link", side_effect=PermissionError
        ):
            with self.assertRaisesRegex(backup.BackupRestoreError, "finalize"):
                backup.create_backup("dbname=source", backup_path)
        with patch.object(backup.subprocess, "run", side_effect=self.fake_runner), patch.object(
            backup, "_write_manifest", side_effect=backup.BackupRestoreError("manifest failure")
        ):
            with self.assertRaisesRegex(backup.BackupRestoreError, "manifest failure"):
                backup.create_backup("dbname=source", backup_path)
        self.assertFalse(backup_path.exists())
        with patch.object(backup.subprocess, "run", side_effect=self.fake_runner):
            backup.create_backup("dbname=source", backup_path)
        with patch.object(backup.subprocess, "run", side_effect=self.fake_runner), patch.object(
            backup.os, "unlink", side_effect=FileNotFoundError
        ):
            backup.create_backup("dbname=source", self.root / "missing-temp.dump")
        with patch.object(backup.subprocess, "run", side_effect=self.fake_runner), patch.object(
            backup.os, "unlink", side_effect=PermissionError
        ):
            result = backup.create_backup("dbname=source", self.root / "unclean.dump")
        self.assertEqual(self.counts, result["row_counts"])
        self.assertTrue((self.root / "unclean.dump").is_file())
        self.assertTrue((self.root / "unclean.dump.manifest.json").is_file())
        with patch.object(backup.subprocess, "run", side_effect=self.fake_runner):
            retry = backup.create_backup("dbname=source", self.root / "unclean.dump")
            self.assertEqual(result, retry)
            backup.restore_and_verify(
                "dbname=target",
                self.root / "unclean.dump",
                target_is_disposable=True,
            )

    def test_manifest_validation_and_write_failures(self) -> None:
        """Manifest files are closed, digest-checked, and exclusive."""
        backup_path, manifest_path = self.create_artifact("first")
        valid = json.loads(manifest_path.read_text(encoding="utf-8"))
        invalid_payloads = (
            {"manifest_version": 1},
            {**valid, "manifest_version": 2},
            {**valid, "backup_format": "plain"},
            {**valid, "backup_file": "other.dump"},
            {**valid, "backup_sha256": "bad"},
            {**valid, "row_counts": {"wrong": 1}},
            {**valid, "row_counts": {**self.counts, "usage_event_count": True}},
        )
        for payload in invalid_payloads:
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(backup.BackupRestoreError):
                backup._load_manifest(manifest_path, backup_path)
        manifest_path.write_text("not-json", encoding="utf-8")
        with self.assertRaisesRegex(backup.BackupRestoreError, "unreadable"):
            backup._load_manifest(manifest_path, backup_path)
        with patch.object(backup, "_manifest", return_value=valid):
            with patch.object(backup.Path, "open", side_effect=FileExistsError):
                with self.assertRaisesRegex(backup.BackupRestoreError, "already exists"):
                    backup._write_manifest(self.root / "new.json", valid)
        with patch.object(backup.Path, "open", side_effect=PermissionError):
            with self.assertRaisesRegex(backup.BackupRestoreError, "cannot write"):
                backup._write_manifest(self.root / "new.json", valid)
        with patch.object(backup.json, "dump", side_effect=TypeError("not serializable")):
            with self.assertRaisesRegex(backup.BackupRestoreError, "cannot write"):
                backup._write_manifest(self.root / "partial.json", valid)
        self.assertFalse((self.root / "partial.json").exists())
        with patch.object(backup.json, "dump", side_effect=TypeError("not serializable")), patch.object(
            backup.Path, "unlink", side_effect=PermissionError
        ):
            with self.assertRaisesRegex(backup.BackupRestoreError, "cannot clean"):
                backup._write_manifest(self.root / "unclean.json", valid)
        with patch.object(backup.json, "dump", side_effect=TypeError("not serializable")), patch.object(
            backup.Path, "unlink", side_effect=FileNotFoundError
        ):
            with self.assertRaisesRegex(backup.BackupRestoreError, "cannot write"):
                backup._write_manifest(self.root / "missing.json", valid)
        with self.assertRaisesRegex(backup.BackupRestoreError, "cannot read"):
            backup._sha256(self.root / "missing.dump")

    def test_owned_backup_rollback_is_inode_scoped(self) -> None:
        """Rollback removes only the hard link created by this invocation."""
        temporary = self.root / "temporary.dump"
        owned = self.root / "owned.dump"
        other = self.root / "other.dump"
        temporary.write_bytes(b"backup")
        os.link(temporary, owned)
        other.write_bytes(b"different")
        backup._remove_owned_backup(owned, str(temporary))
        self.assertFalse(owned.exists())
        backup._remove_owned_backup(owned, str(temporary))
        backup._remove_owned_backup(other, str(temporary))
        self.assertTrue(other.exists())

        link = self.root / "link.dump"
        link.symlink_to(temporary)
        backup._remove_owned_backup(link, str(temporary))
        self.assertTrue(link.is_symlink())
        with patch.object(backup.os, "stat", side_effect=PermissionError):
            with self.assertRaisesRegex(backup.BackupRestoreError, "inspect"):
                backup._remove_owned_backup(other, str(temporary))
        os.link(temporary, owned)
        with patch.object(backup.os, "unlink", side_effect=FileNotFoundError):
            backup._remove_owned_backup(owned, str(temporary))
        with patch.object(backup.os, "unlink", side_effect=PermissionError):
            with self.assertRaisesRegex(backup.BackupRestoreError, "roll back"):
                backup._remove_owned_backup(owned, str(temporary))

    def test_restore_requires_manifest_digest_and_matching_counts(self) -> None:
        """Restore is explicit, verifies bytes first, and compares stable counts."""
        backup_path, manifest_path = self.create_artifact("second")
        with self.assertRaisesRegex(backup.BackupRestoreError, "disposable"):
            backup.restore_and_verify("dbname=target", backup_path, manifest_path)
        with patch.object(backup.subprocess, "run", side_effect=self.fake_runner):
            result = backup.restore_and_verify(
                "dbname=target", backup_path, manifest_path, target_is_disposable=True
            )
        self.assertEqual("verified", result["status"])
        backup_path.write_bytes(b"tampered")
        with self.assertRaisesRegex(backup.BackupRestoreError, "digest"):
            backup.restore_and_verify(
                "dbname=target", backup_path, manifest_path, target_is_disposable=True
            )

        backup_path, manifest_path = self.create_artifact()
        mismatched = json.loads(manifest_path.read_text(encoding="utf-8"))
        mismatched["row_counts"]["usage_event_count"] = 10
        manifest_path.write_text(json.dumps(mismatched), encoding="utf-8")
        with patch.object(backup.subprocess, "run", side_effect=self.fake_runner):
            with self.assertRaisesRegex(backup.BackupRestoreError, "do not match"):
                backup.restore_and_verify(
                    "dbname=target", backup_path, manifest_path, target_is_disposable=True
                )

    def test_main_success_and_error(self) -> None:
        """CLI dispatches to the selected mode and returns a stable error code."""
        with patch.dict(os.environ, {"SOURCE_DSN": "dbname=source"}), patch.object(
            backup, "create_backup", return_value={"status": "created"}
        ) as create:
            self.assertEqual(
                0,
                backup.main(
                    ["--mode", "create", "--backup-path", str(self.root / "x.dump"), "--source-dsn-env", "SOURCE_DSN"]
                ),
            )
            create.assert_called_once()
        with patch.dict(os.environ, {"TARGET_DSN": "dbname=target"}), patch.object(
            backup, "restore_and_verify", return_value={"status": "verified"}
        ) as restore:
            self.assertEqual(
                0,
                backup.main(
                    [
                        "--mode",
                        "restore-verify",
                        "--backup-path",
                        str(self.root / "x.dump"),
                        "--target-dsn-env",
                        "TARGET_DSN",
                        "--target-is-disposable",
                    ]
                ),
            )
            restore.assert_called_once()
        with patch.object(backup, "create_backup", side_effect=backup.BackupRestoreError("nope")):
            with patch.dict(os.environ, {"SOURCE_DSN": "dbname=source"}):
                self.assertEqual(
                    2,
                    backup.main(
                        ["--mode", "create", "--backup-path", str(self.root / "x.dump"), "--source-dsn-env", "SOURCE_DSN"]
                    ),
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
