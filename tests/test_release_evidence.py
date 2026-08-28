"""Tests for deterministic release SBOM evidence."""

from __future__ import annotations

import json
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest import mock

from scripts.build_spdx_sbom import build_sbom, main, write_sbom


RELEASE_SHA = "0123456789abcdef0123456789abcdef01234567"


class ReleaseEvidenceTests(unittest.TestCase):
    """Keep release evidence content-bound and reproducible."""

    def test_build_and_write_spdx_graph(self) -> None:
        """Artifacts appear with SHA-256 hashes and immutable release metadata."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "dist"
            root.joinpath("python").mkdir(parents=True)
            root.joinpath("python", "package.whl").write_bytes(b"python")
            root.joinpath("rust").mkdir()
            root.joinpath("rust", "package.crate").write_bytes(b"rust")
            output = root / "evidence" / "release.spdx.json"

            first = build_sbom(root, output, RELEASE_SHA, "2026-08-29T09:00:00+09:00")
            second = build_sbom(root, output, RELEASE_SHA, "2026-08-29T00:00:00Z")
            self.assertEqual(first, second)
            self.assertEqual(first["@context"], "https://spdx.org/rdf/3.0.1/spdx-context.jsonld")
            graph = first["@graph"]
            self.assertIsInstance(graph, list)
            packages = [item for item in graph if item.get("type") == "software_Package"]
            self.assertEqual([item["name"] for item in packages], ["python/package.whl", "rust/package.crate"])
            self.assertEqual(
                packages[0]["verifiedUsing"][0]["hashValue"],
                sha256(b"python").hexdigest(),
            )

            write_sbom(root, output, RELEASE_SHA, "2026-08-29T00:00:00Z")
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), first)

    def test_rejects_invalid_release_inputs_and_empty_roots(self) -> None:
        """Evidence refuses ambiguous identity, time, and artifact inputs."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "dist"
            root.mkdir()
            output = root / "release.spdx.json"
            with self.assertRaisesRegex(ValueError, "lowercase 40-character"):
                build_sbom(root, output, "A" * 40, "2026-08-29T00:00:00Z")
            with self.assertRaisesRegex(ValueError, "ISO 8601"):
                build_sbom(root, output, RELEASE_SHA, "not-a-time")
            with self.assertRaisesRegex(ValueError, "include a timezone"):
                build_sbom(root, output, RELEASE_SHA, "2026-08-29T00:00:00")
            with self.assertRaisesRegex(ValueError, "no files"):
                build_sbom(root, output, RELEASE_SHA, "2026-08-29T00:00:00Z")
            with self.assertRaisesRegex(ValueError, "not a directory"):
                build_sbom(root / "missing", output, RELEASE_SHA, "2026-08-29T00:00:00Z")

    def test_command_line_entrypoint(self) -> None:
        """The release workflow can invoke the script as a normal command."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "dist"
            root.mkdir()
            root.joinpath("artifact.whl").write_bytes(b"artifact")
            output = root / "release.spdx.json"
            with mock.patch(
                "sys.argv",
                [
                    "build_spdx_sbom.py",
                    "--artifact-root",
                    str(root),
                    "--output",
                    str(output),
                    "--release-sha",
                    RELEASE_SHA,
                    "--created-at",
                    "2026-08-29T00:00:00Z",
                ],
            ):
                self.assertEqual(main(), 0)
            self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
