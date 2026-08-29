"""Tests for the secret-safe PostgreSQL archive helper."""

from __future__ import annotations

import io
import subprocess
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.postgres_backup import (
    PostgresBackupError,
    create_backup,
    main,
    restore_backup,
    verify_backup,
)


class PostgresBackupTests(unittest.TestCase):
    """Keep archive operations deterministic without requiring a live database."""

    @staticmethod
    def _result(returncode: int = 0) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(("postgres-client",), returncode, "", "")

    def test_create_backup_publishes_nonempty_archive_atomically(self) -> None:
        """A successful dump uses custom format and does not overwrite output."""
        with TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "nested" / "billing.dump"

            def dump(command: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
                archive = Path(command[command.index("--file") + 1])
                archive.write_bytes(b"custom archive")
                return self._result()

            with unittest.mock.patch("scripts.postgres_backup.subprocess.run", side_effect=dump) as runner:
                self.assertEqual(
                    create_backup("postgresql://secret.example/db", output, pg_dump_binary="dump"),
                    output,
                )
            self.assertEqual(output.read_bytes(), b"custom archive")
            command = runner.call_args.args[0]
            self.assertEqual(
                command[:4], ("dump", "--format=custom", "--no-owner", "--file")
            )
            self.assertEqual(command[-1], "postgresql://secret.example/db")
            self.assertEqual(tuple(output.parent.glob("*.partial")), ())

    def test_create_backup_rejects_existing_destination_and_empty_dump(self) -> None:
        """The helper never replaces an archive and rejects an empty dump."""
        with TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "billing.dump"
            output.write_bytes(b"existing")
            with self.assertRaisesRegex(PostgresBackupError, "already exists"):
                create_backup("dsn", output)

            output.unlink()
            with unittest.mock.patch(
                "scripts.postgres_backup.subprocess.run", return_value=self._result()
            ):
                with self.assertRaisesRegex(PostgresBackupError, "empty archive"):
                    create_backup("dsn", output)
            self.assertFalse(output.exists())
            self.assertEqual(tuple(output.parent.glob("*.partial")), ())

    def test_create_backup_cleans_failed_or_missing_partial_archive(self) -> None:
        """Failed clients and clients that remove their output leave no partial file."""
        with TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "billing.dump"
            with unittest.mock.patch(
                "scripts.postgres_backup.subprocess.run", return_value=self._result(7)
            ):
                with self.assertRaisesRegex(PostgresBackupError, "exit status 7"):
                    create_backup("dsn", output)
            self.assertEqual(tuple(output.parent.glob("*.partial")), ())

            def remove_output(command: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
                Path(command[command.index("--file") + 1]).unlink()
                return self._result()

            with unittest.mock.patch("scripts.postgres_backup.subprocess.run", side_effect=remove_output):
                with self.assertRaisesRegex(PostgresBackupError, "empty archive"):
                    create_backup("dsn", output)

            def create_race(command: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
                archive = Path(command[command.index("--file") + 1])
                archive.write_bytes(b"new archive")
                output.write_bytes(b"existing archive")
                return self._result()

            with unittest.mock.patch("scripts.postgres_backup.subprocess.run", side_effect=create_race):
                with self.assertRaisesRegex(PostgresBackupError, "became occupied"):
                    create_backup("dsn", output)
            self.assertEqual(output.read_bytes(), b"existing archive")

            with unittest.mock.patch(
                "scripts.postgres_backup.subprocess.run",
                side_effect=lambda command, **_: (
                    Path(command[command.index("--file") + 1]).write_bytes(b"archive"),
                    self._result(),
                )[1],
            ), unittest.mock.patch("scripts.postgres_backup.os.link", side_effect=OSError):
                output.unlink()
                with self.assertRaisesRegex(PostgresBackupError, "publication failed"):
                    create_backup("dsn", output)

            with unittest.mock.patch(
                "scripts.postgres_backup.subprocess.run",
                side_effect=lambda command, **_: (
                    Path(command[command.index("--file") + 1]).write_bytes(b"archive"),
                    self._result(),
                )[1],
            ), unittest.mock.patch("pathlib.Path.unlink", side_effect=OSError):
                with self.assertRaisesRegex(PostgresBackupError, "published but"):
                    create_backup("dsn", output)
            output.unlink()

            with unittest.mock.patch(
                "scripts.postgres_backup.subprocess.run", return_value=self._result(7)
            ), unittest.mock.patch("pathlib.Path.unlink", side_effect=OSError):
                with self.assertRaisesRegex(PostgresBackupError, "failed and"):
                    create_backup("dsn", output)

    def test_archive_validation_rejects_missing_invalid_and_unavailable_clients(self) -> None:
        """Archive checks fail closed without returning subprocess output."""
        with TemporaryDirectory() as temporary_directory:
            archive = Path(temporary_directory) / "billing.dump"
            with self.assertRaisesRegex(PostgresBackupError, "does not exist"):
                verify_backup(archive)
            archive.write_bytes(b"archive")
            with unittest.mock.patch(
                "scripts.postgres_backup.subprocess.run", return_value=self._result(3)
            ):
                with self.assertRaisesRegex(PostgresBackupError, "verification.*exit status 3"):
                    verify_backup(archive)
            with unittest.mock.patch(
                "scripts.postgres_backup.subprocess.run", side_effect=FileNotFoundError
            ):
                with self.assertRaisesRegex(PostgresBackupError, "client is unavailable"):
                    verify_backup(archive)
            with self.assertRaisesRegex(PostgresBackupError, "client binary is required"):
                verify_backup(archive, pg_restore_binary=" ")

    def test_restore_verifies_then_restores_without_clean_by_default(self) -> None:
        """A normal restore is single-transaction and does not drop existing objects."""
        with TemporaryDirectory() as temporary_directory:
            archive = Path(temporary_directory) / "billing.dump"
            archive.write_bytes(b"archive")
            with unittest.mock.patch(
                "scripts.postgres_backup.subprocess.run", return_value=self._result()
            ) as runner:
                self.assertEqual(
                    restore_backup("postgresql://secret.example/db", archive, pg_restore_binary="restore"),
                    archive,
                )
            verify_command, restore_command = (
                call.args[0] for call in runner.call_args_list
            )
            self.assertEqual(verify_command, ("restore", "--list", str(archive)))
            self.assertEqual(
                restore_command,
                (
                    "restore",
                    "--exit-on-error",
                    "--single-transaction",
                    "--no-owner",
                    "--dbname",
                    "postgresql://secret.example/db",
                    str(archive),
                ),
            )

    def test_restore_clean_requires_confirmation_and_can_fail(self) -> None:
        """Cleanup is explicit and restore client failures remain secret-free."""
        with TemporaryDirectory() as temporary_directory:
            archive = Path(temporary_directory) / "billing.dump"
            archive.write_bytes(b"archive")
            with unittest.mock.patch(
                "scripts.postgres_backup.subprocess.run", return_value=self._result()
            ):
                with self.assertRaisesRegex(PostgresBackupError, "requires"):
                    restore_backup("dsn", archive, clean=True)
            with unittest.mock.patch(
                "scripts.postgres_backup.subprocess.run", side_effect=[self._result(), self._result(9)]
            ):
                with self.assertRaisesRegex(PostgresBackupError, "exit status 9"):
                    restore_backup("postgresql://secret.example/db", archive, clean=True, confirm_destructive_restore=True)
            with unittest.mock.patch(
                "scripts.postgres_backup.subprocess.run", return_value=self._result()
            ) as runner:
                restore_backup("dsn", archive, clean=True, confirm_destructive_restore=True)
            self.assertIn("--clean", runner.call_args_list[1].args[0])
            self.assertIn("--if-exists", runner.call_args_list[1].args[0])

    def test_invalid_connection_and_binary_inputs_fail_before_subprocess(self) -> None:
        """Trust-boundary arguments are rejected before client invocation."""
        with TemporaryDirectory() as temporary_directory:
            archive = Path(temporary_directory) / "billing.dump"
            archive.write_bytes(b"archive")
            with self.assertRaisesRegex(PostgresBackupError, "connection string"):
                create_backup(" ", archive)
            with self.assertRaisesRegex(PostgresBackupError, "must omit passwords"):
                create_backup("postgresql://user:secret@host/database", archive)
            with self.assertRaisesRegex(PostgresBackupError, "must omit passwords"):
                create_backup("host=database password=secret", archive)
            with self.assertRaisesRegex(PostgresBackupError, "must omit passwords"):
                create_backup("host=database password = secret", archive)
            with self.assertRaisesRegex(PostgresBackupError, "invalid PostgreSQL"):
                create_backup("postgresql://[", archive)
            allowed_output = Path(temporary_directory) / "allowed.dump"

            def dump_without_password_false_positive(
                command: tuple[str, ...], **_: object
            ) -> subprocess.CompletedProcess[str]:
                Path(command[command.index("--file") + 1]).write_bytes(b"archive")
                return self._result()

            with unittest.mock.patch(
                "scripts.postgres_backup.subprocess.run",
                side_effect=dump_without_password_false_positive,
            ):
                self.assertEqual(
                    create_backup(
                        "host=database application_name='password=rotation'",
                        allowed_output,
                    ),
                    allowed_output,
                )
            with self.assertRaisesRegex(PostgresBackupError, "client binary"):
                create_backup("dsn", archive, pg_dump_binary="")
            with self.assertRaisesRegex(PostgresBackupError, "connection string"):
                restore_backup("", archive)

    def test_main_dispatches_all_operations_and_returns_secret_free_error(self) -> None:
        """The CLI prints only operation status and sanitized failures."""
        with TemporaryDirectory() as temporary_directory:
            archive = Path(temporary_directory) / "billing.dump"
            with unittest.mock.patch("scripts.postgres_backup.create_backup", return_value=archive):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main(["backup", "--dsn", "dsn", "--output", str(archive)]), 0)
                self.assertIn("backup complete", output.getvalue())
            with unittest.mock.patch("scripts.postgres_backup.verify_backup", return_value=archive):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main(["verify", "--archive", str(archive)]), 0)
                self.assertIn("verify complete", output.getvalue())
            with unittest.mock.patch("scripts.postgres_backup.restore_backup", return_value=archive):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main(["restore", "--dsn", "dsn", "--archive", str(archive)]), 0)
                self.assertIn("restore complete", output.getvalue())
            with unittest.mock.patch(
                "scripts.postgres_backup.create_backup",
                side_effect=PostgresBackupError("client failed"),
            ):
                error = io.StringIO()
                with redirect_stderr(error):
                    self.assertEqual(main(["backup", "--dsn", "top-secret", "--output", str(archive)]), 2)
                self.assertEqual(error.getvalue(), "error: client failed\n")


if __name__ == "__main__":
    unittest.main()
