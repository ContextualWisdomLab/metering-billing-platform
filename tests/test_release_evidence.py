"""Tests for exact-head release source evidence."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest.mock as mock

from scripts.release_evidence import (
    ReleaseEvidenceError,
    _checked_out_artifact,
    _load_manifest,
    _sha256,
    _source_commit,
    _tracked_paths,
    _require_clean_checkout,
    _worktree_root,
    build_manifest,
    create_manifest,
    main,
    verify_manifest,
)


ROOT = Path(__file__).resolve().parents[1]


class ReleaseEvidenceTests(unittest.TestCase):
    """Keep release evidence deterministic and secret-free."""

    def test_create_and_verify_manifest_from_current_checkout(self) -> None:
        """A manifest records the current tracked inventory and verifies it."""
        with TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "source-manifest.json"
            self.assertEqual(
                create_manifest(ROOT, "0.3.0-rc.1", manifest_path), manifest_path
            )
            verify_manifest(ROOT, manifest_path, "0.3.0-rc.1")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["release_version"], "0.3.0-rc.1")
            self.assertTrue(manifest["artifacts"])

    def test_manifest_rejects_existing_destination_and_invalid_version(self) -> None:
        """Evidence creation never overwrites and requires SemVer input."""
        with TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "source-manifest.json"
            manifest_path.write_text("existing", encoding="utf-8")
            with self.assertRaisesRegex(ReleaseEvidenceError, "already exists"):
                create_manifest(ROOT, "0.3.0", manifest_path)
            manifest_path.unlink()
            for invalid_version in ("release-latest", "1.0.0-01"):
                with self.assertRaisesRegex(ReleaseEvidenceError, "semantic"):
                    build_manifest(ROOT, invalid_version, manifest_path)

    def test_git_identity_and_inventory_reject_ambiguous_values(self) -> None:
        """Evidence refuses abbreviated commits and unsafe Git paths."""
        with mock.patch(
            "scripts.release_evidence._run_git", return_value="not-a-sha\n"
        ):
            with self.assertRaisesRegex(ReleaseEvidenceError, "full source commit"):
                _source_commit(ROOT)
        for unsafe_path in ("/absolute", "../parent"):
            with mock.patch(
                "scripts.release_evidence._run_git", return_value=f"{unsafe_path}\0"
            ):
                with self.assertRaisesRegex(ReleaseEvidenceError, "unsafe release path"):
                    _tracked_paths(ROOT)

    def test_manifest_rejects_dirty_or_partial_checkout(self) -> None:
        """Evidence binds to a clean repository root and stable HEAD."""
        with self.assertRaisesRegex(ReleaseEvidenceError, "worktree root"):
            build_manifest(ROOT / "scripts", "0.3.0", ROOT / "manifest.json")
        with mock.patch("scripts.release_evidence._worktree_root", return_value=ROOT), mock.patch(
            "scripts.release_evidence._source_commit", return_value="a" * 40
        ), mock.patch(
            "scripts.release_evidence._run_git", return_value=" M tracked.py\n"
        ):
            with self.assertRaisesRegex(ReleaseEvidenceError, "clean checkout"):
                build_manifest(ROOT, "0.3.0", ROOT / "manifest.json")
        with mock.patch(
            "scripts.release_evidence._source_commit", return_value="a" * 40
        ), mock.patch(
            "scripts.release_evidence._run_git", side_effect=("", "h tracked.py\0")
        ):
            with self.assertRaisesRegex(ReleaseEvidenceError, "hidden index"):
                _require_clean_checkout(ROOT, "a" * 40)
        with mock.patch("scripts.release_evidence._worktree_root", return_value=ROOT), mock.patch(
            "scripts.release_evidence._source_commit", side_effect=("a" * 40, "b" * 40)
        ):
            with self.assertRaisesRegex(ReleaseEvidenceError, "HEAD changed"):
                build_manifest(ROOT, "0.3.0", ROOT / "manifest.json")
        with mock.patch(
            "scripts.release_evidence._run_git", return_value=""
        ):
            with self.assertRaisesRegex(ReleaseEvidenceError, "Git worktree root"):
                _worktree_root(ROOT)

    def test_manifest_rejects_symlinks_and_unreadable_bytes(self) -> None:
        """Evidence never follows a symlink or leaks a filesystem error."""
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory) / "checkout"
            directory.mkdir()
            outside = Path(temporary_directory) / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            (directory / "link.txt").symlink_to(outside)
            with self.assertRaisesRegex(ReleaseEvidenceError, "symlinked"):
                _checked_out_artifact(directory, "link.txt")
            with self.assertRaisesRegex(ReleaseEvidenceError, "unreadable"):
                _sha256(directory / "missing.txt")

    def test_manifest_creation_is_exclusive_and_sanitizes_write_errors(self) -> None:
        """A racing writer or filesystem failure cannot overwrite evidence."""
        manifest = {
            "release_evidence_contract_version": 1,
            "release_version": "0.3.0",
            "source_commit": "a" * 40,
            "artifacts": [{"path": "README.md", "sha256": "0" * 64}],
        }
        with TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "manifest.json"
            with mock.patch(
                "scripts.release_evidence.build_manifest", return_value=manifest
            ), mock.patch.object(Path, "open", side_effect=FileExistsError):
                with self.assertRaisesRegex(ReleaseEvidenceError, "already exists"):
                    create_manifest(ROOT, "0.3.0", manifest_path)
            with mock.patch(
                "scripts.release_evidence.build_manifest", return_value=manifest
            ), mock.patch.object(Path, "open", side_effect=OSError("secret path")):
                with self.assertRaisesRegex(ReleaseEvidenceError, "could not be written"):
                    create_manifest(ROOT, "0.3.0", manifest_path)

    def test_manifest_requires_a_tracked_artifact_and_rejects_tracked_output(self) -> None:
        """Empty indexes fail and a tracked output path is rejected."""
        with mock.patch(
            "scripts.release_evidence._source_commit", return_value="a" * 40
        ), mock.patch("scripts.release_evidence._tracked_paths", return_value=()):
            with self.assertRaisesRegex(ReleaseEvidenceError, "tracked artifacts"):
                build_manifest(ROOT, "0.3.0", ROOT / "manifest.json")

        with self.assertRaisesRegex(ReleaseEvidenceError, "must not be tracked"):
            build_manifest(ROOT, "0.3.0", ROOT / "scripts/release_evidence.py")

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manifest_path = root / "manifest.json"
            expected = {
                "release_version": "0.3.0",
                "source_commit": "a" * 40,
                "artifacts": [{"path": "README.md", "sha256": "0" * 64}],
            }
            with mock.patch(
                "scripts.release_evidence._load_manifest", return_value=expected
            ), mock.patch(
                "scripts.release_evidence._source_commit", return_value="a" * 40
            ), mock.patch(
                "scripts.release_evidence._tracked_paths",
                return_value=("tracked.txt",),
            ), mock.patch(
                "scripts.release_evidence._worktree_root", return_value=root
            ), mock.patch("scripts.release_evidence._require_clean_checkout"):
                with self.assertRaises(ReleaseEvidenceError) as error:
                    verify_manifest(root, manifest_path, "0.3.0")
                self.assertIn("missing tracked artifact: README.md", str(error.exception))
                self.assertIn("missing artifact bytes: README.md", str(error.exception))

        with mock.patch(
            "scripts.release_evidence._load_manifest",
            return_value={
                "release_version": "0.3.0",
                "source_commit": "a" * 40,
                "artifacts": [{"path": "README.md", "sha256": "0" * 64}],
            },
        ), mock.patch(
            "scripts.release_evidence._worktree_root", return_value=ROOT
        ), mock.patch(
            "scripts.release_evidence._require_clean_checkout"
        ), mock.patch(
            "scripts.release_evidence._manifest_relative_path",
            return_value="README.md",
        ), mock.patch(
            "scripts.release_evidence._tracked_paths", return_value=("README.md",)
        ):
            with self.assertRaisesRegex(ReleaseEvidenceError, "must not be tracked"):
                verify_manifest(ROOT, ROOT / "manifest.json", "0.3.0")

    def test_manifest_detects_commit_inventory_and_hash_changes(self) -> None:
        """Changed release identity, inventory, and bytes all fail closed."""
        with TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "source-manifest.json"
            create_manifest(ROOT, "0.3.0", manifest_path)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            tampered = dict(manifest)
            tampered["source_commit"] = "0" * 40
            manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(ReleaseEvidenceError, "source commit"):
                verify_manifest(ROOT, manifest_path, "0.3.0")

            tampered = dict(manifest)
            tampered["artifacts"] = manifest["artifacts"][1:]
            manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(ReleaseEvidenceError, "unexpected tracked"):
                verify_manifest(ROOT, manifest_path, "0.3.0")

            tampered = dict(manifest)
            tampered["artifacts"] = [
                dict(manifest["artifacts"][0], sha256="0" * 64)
            ] + manifest["artifacts"][1:]
            manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(ReleaseEvidenceError, "hash mismatch"):
                verify_manifest(ROOT, manifest_path, "0.3.0")

            tampered = dict(manifest, release_version="0.3.1")
            manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(ReleaseEvidenceError, "release version"):
                verify_manifest(ROOT, manifest_path, "0.3.0")

    def test_manifest_structure_errors_fail_closed(self) -> None:
        """Malformed JSON and artifact metadata cannot become release evidence."""
        with TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "manifest.json"
            for value, message in (
                ("[1]", "JSON object"),
                ('{"release_evidence_contract_version": 2}', "contract version"),
                (
                    json.dumps(
                        {
                            "release_evidence_contract_version": 1,
                            "release_version": "0.3.0",
                            "source_commit": "0" * 40,
                            "artifacts": [],
                        }
                    ),
                    "artifacts are required",
                ),
                (
                    json.dumps(
                        {
                            "release_evidence_contract_version": 1,
                            "release_version": "0.3.0",
                            "source_commit": "bad",
                            "artifacts": [{"path": "README.md", "sha256": "0" * 64}],
                        }
                    ),
                    "source commit is invalid",
                ),
                (
                    json.dumps(
                        {
                            "release_evidence_contract_version": 1,
                            "release_version": "0.3.0",
                            "source_commit": "0" * 40,
                            "artifacts": [{"path": "README.md", "sha256": "0" * 64}],
                            "extra": True,
                        }
                    ),
                    "manifest fields are invalid",
                ),
            ):
                manifest_path.write_text(value, encoding="utf-8")
                with self.assertRaisesRegex(ReleaseEvidenceError, message):
                    _load_manifest(manifest_path)
            manifest_path.write_text("not json", encoding="utf-8")
            with self.assertRaisesRegex(ReleaseEvidenceError, "unreadable"):
                _load_manifest(manifest_path)

    def test_manifest_rejects_bad_artifact_entries(self) -> None:
        """Absolute, parent, duplicate, and malformed hash paths are rejected."""
        with TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "manifest.json"
            base = {
                "release_evidence_contract_version": 1,
                "release_version": "0.3.0",
                "source_commit": "0" * 40,
            }
            entries = (
                ([{"path": "/absolute", "sha256": "0" * 64}], "path is invalid"),
                ([{"path": "../parent", "sha256": "0" * 64}], "path is invalid"),
                ([{"path": "", "sha256": "0" * 64}], "path is invalid"),
                ([{"path": 1, "sha256": "0" * 64}], "path is invalid"),
                (
                    [{"path": "README.md", "sha256": "0" * 64, "extra": True}],
                    "artifact fields are invalid",
                ),
                (
                    [
                        {"path": "README.md", "sha256": "0" * 64},
                        {"path": "README.md", "sha256": "0" * 64},
                    ],
                    "path is invalid",
                ),
                ([{"path": "README.md", "sha256": "bad"}], "hash is invalid"),
                (["README.md"], "artifact is invalid"),
            )
            for artifacts, message in entries:
                manifest_path.write_text(
                    json.dumps({**base, "artifacts": artifacts}), encoding="utf-8"
                )
                with self.assertRaisesRegex(ReleaseEvidenceError, message):
                    _load_manifest(manifest_path)

    def test_git_failures_are_sanitized(self) -> None:
        """Git failures expose no command output or repository path."""
        result = subprocess.CompletedProcess(("git",), 1, "secret output", "secret error")
        with mock.patch("scripts.release_evidence.subprocess.run", return_value=result):
            with self.assertRaisesRegex(ReleaseEvidenceError, "could not provide"):
                build_manifest(ROOT, "0.3.0", ROOT / "manifest.json")
        with mock.patch(
            "scripts.release_evidence.subprocess.run", side_effect=OSError("secret")
        ):
            with self.assertRaisesRegex(ReleaseEvidenceError, "unavailable"):
                build_manifest(ROOT, "0.3.0", ROOT / "manifest.json")

    def test_cli_dispatches_create_verify_and_errors(self) -> None:
        """The command line exposes only status messages and sanitized errors."""
        with TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "source-manifest.json"
            self.assertEqual(
                main(
                    (
                        "manifest",
                        "--root",
                        str(ROOT),
                        "--release-version",
                        "0.3.0",
                        "--output",
                        str(manifest_path),
                    )
                ),
                0,
            )
            self.assertEqual(
                main(
                    (
                        "verify",
                        "--root",
                        str(ROOT),
                        "--manifest",
                        str(manifest_path),
                        "--release-version",
                        "0.3.0",
                    )
                ),
                0,
            )
            self.assertEqual(
                main(
                    (
                        "verify",
                        "--root",
                        str(ROOT),
                        "--manifest",
                        str(manifest_path) + ".missing",
                        "--release-version",
                        "0.3.0",
                    )
                ),
                2,
            )


if __name__ == "__main__":
    unittest.main()
