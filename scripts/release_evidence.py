"""Create and verify a deterministic source manifest for a release checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


FULL_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SEMVER_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


class ReleaseEvidenceError(RuntimeError):
    """Raised when release-manifest evidence cannot be created or verified."""


def _run_git(root: Path, arguments: tuple[str, ...]) -> str:
    """Run a read-only Git query without returning stderr to an operator."""
    try:
        result = subprocess.run(
            ("git", *arguments),
            cwd=root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError as error:
        raise ReleaseEvidenceError("Git is unavailable for release evidence") from error
    if result.returncode != 0:
        raise ReleaseEvidenceError("Git could not provide release evidence")
    return result.stdout


def _source_commit(root: Path) -> str:
    """Return the exact checked-out commit, rejecting ambiguous repositories."""
    commit = _run_git(root, ("rev-parse", "HEAD")).strip()
    if FULL_COMMIT_PATTERN.fullmatch(commit) is None:
        raise ReleaseEvidenceError("release evidence requires a full source commit")
    return commit


def _tracked_paths(root: Path) -> tuple[str, ...]:
    """Return safe, repository-relative paths from the exact Git index."""
    raw_paths = _run_git(root, ("ls-files", "-z"))
    paths = tuple(path for path in raw_paths.split("\0") if path)
    for path in paths:
        relative_path = Path(path)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ReleaseEvidenceError("Git returned an unsafe release path")
    return tuple(sorted(paths))


def _manifest_relative_path(root: Path, manifest_path: Path) -> str | None:
    """Return a repository-relative manifest path when it is inside the root."""
    try:
        return manifest_path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _require_version(version: object) -> str:
    """Require a semantic release version without accepting mutable labels."""
    if not isinstance(version, str) or SEMVER_PATTERN.fullmatch(version) is None:
        raise ReleaseEvidenceError("release version must be semantic versioning")
    return version


def _sha256(path: Path) -> str:
    """Hash one checked-out artifact's current bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_manifest(
    root: Path, release_version: str, manifest_path: Path
) -> dict[str, Any]:
    """Build a deterministic manifest for tracked files except its own path."""
    version = _require_version(release_version)
    commit = _source_commit(root)
    excluded_path = _manifest_relative_path(root, manifest_path)
    artifacts = [
        {"path": path, "sha256": _sha256(root / path)}
        for path in _tracked_paths(root)
        if path != excluded_path
    ]
    if not artifacts:
        raise ReleaseEvidenceError("release evidence requires tracked artifacts")
    return {
        "release_evidence_contract_version": 1,
        "release_version": version,
        "source_commit": commit,
        "artifacts": artifacts,
    }


def create_manifest(root: Path, release_version: str, manifest_path: Path) -> Path:
    """Write one new manifest without overwriting an existing evidence file."""
    if manifest_path.exists():
        raise ReleaseEvidenceError("release manifest destination already exists")
    manifest = build_manifest(root, release_version, manifest_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def _load_manifest(manifest_path: Path) -> dict[str, Any]:
    """Load and validate the structural shape of one manifest."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseEvidenceError("release manifest is unreadable") from error
    if not isinstance(manifest, dict):
        raise ReleaseEvidenceError("release manifest must be a JSON object")
    if manifest.get("release_evidence_contract_version") != 1:
        raise ReleaseEvidenceError("unsupported release evidence contract version")
    _require_version(manifest.get("release_version"))
    source_commit = manifest.get("source_commit")
    if not isinstance(source_commit, str) or FULL_COMMIT_PATTERN.fullmatch(source_commit) is None:
        raise ReleaseEvidenceError("release manifest source commit is invalid")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ReleaseEvidenceError("release manifest artifacts are required")
    paths: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ReleaseEvidenceError("release manifest artifact is invalid")
        path = artifact.get("path")
        digest = artifact.get("sha256")
        relative_path = Path(path) if isinstance(path, str) else None
        if (
            relative_path is None
            or not path
            or relative_path.is_absolute()
            or ".." in relative_path.parts
            or path in paths
        ):
            raise ReleaseEvidenceError("release manifest artifact path is invalid")
        if not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None:
            raise ReleaseEvidenceError("release manifest artifact hash is invalid")
        paths.add(path)
    return manifest


def verify_manifest(root: Path, manifest_path: Path) -> None:
    """Verify commit identity, tracked-file inventory, and every artifact hash."""
    manifest = _load_manifest(manifest_path)
    errors: list[str] = []
    if manifest["source_commit"] != _source_commit(root):
        errors.append("source commit does not match the checked-out HEAD")
    excluded_path = _manifest_relative_path(root, manifest_path)
    tracked_paths = set(_tracked_paths(root))
    expected = {artifact["path"]: artifact["sha256"] for artifact in manifest["artifacts"]}
    if excluded_path is not None:
        tracked_paths.discard(excluded_path)
    for path in sorted(set(expected) - tracked_paths):
        errors.append(f"missing tracked artifact: {path}")
    for path in sorted(tracked_paths - set(expected)):
        errors.append(f"unexpected tracked artifact: {path}")
    for path, expected_hash in sorted(expected.items()):
        artifact_path = root / path
        if not artifact_path.is_file():
            continue
        if _sha256(artifact_path) != expected_hash:
            errors.append(f"artifact hash mismatch: {path}")
    if errors:
        raise ReleaseEvidenceError("; ".join(errors))


def main(arguments: tuple[str, ...] | None = None) -> int:
    """Run manifest creation or verification from the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    manifest_parser = subparsers.add_parser("manifest", help="create a source manifest")
    manifest_parser.add_argument("--root", type=Path, default=Path.cwd())
    manifest_parser.add_argument("--release-version", required=True)
    manifest_parser.add_argument("--output", type=Path, required=True)
    verify_parser = subparsers.add_parser("verify", help="verify a source manifest")
    verify_parser.add_argument("--root", type=Path, default=Path.cwd())
    verify_parser.add_argument("--manifest", type=Path, required=True)
    options = parser.parse_args(arguments)
    try:
        if options.operation == "manifest":
            create_manifest(options.root, options.release_version, options.output)
            print("release manifest created")
        else:
            verify_manifest(options.root, options.manifest)
            print("release manifest verified")
    except ReleaseEvidenceError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
