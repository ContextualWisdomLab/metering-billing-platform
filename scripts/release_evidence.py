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
    r"(?:-(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)(?:"
    r"\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
MANIFEST_FIELDS = frozenset(
    {"release_evidence_contract_version", "release_version", "source_commit", "artifacts"}
)
ARTIFACT_FIELDS = frozenset({"path", "sha256"})


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


def _worktree_root(root: Path) -> Path:
    """Require the configured root to be the complete Git worktree root."""
    configured_root = root.resolve()
    git_root_text = _run_git(root, ("rev-parse", "--show-toplevel")).strip()
    if not git_root_text:
        raise ReleaseEvidenceError("release evidence requires a Git worktree root")
    git_root = Path(git_root_text).resolve()
    if git_root != configured_root:
        raise ReleaseEvidenceError("release evidence root must be the Git worktree root")
    return configured_root


def _tracked_paths(root: Path) -> tuple[str, ...]:
    """Return safe, repository-relative paths from the exact Git index."""
    raw_paths = _run_git(root, ("ls-files", "-z"))
    paths = tuple(path for path in raw_paths.split("\0") if path)
    for path in paths:
        relative_path = Path(path)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ReleaseEvidenceError("Git returned an unsafe release path")
    return tuple(sorted(paths))


def _require_clean_checkout(root: Path, expected_commit: str) -> None:
    """Reject index, worktree, untracked, or HEAD drift during collection."""
    if _source_commit(root) != expected_commit:
        raise ReleaseEvidenceError(
            "source commit/checked-out HEAD changed during release evidence collection"
        )
    if _run_git(root, ("status", "--porcelain=v1", "--untracked-files=all")).strip():
        raise ReleaseEvidenceError("release evidence requires a clean checkout")
    index_entries = _run_git(root, ("ls-files", "-v", "-z")).split("\0")
    if any(entry and not entry.startswith("H ") for entry in index_entries):
        raise ReleaseEvidenceError("release evidence rejects hidden index changes")


def _manifest_relative_path(root: Path, manifest_path: Path) -> str | None:
    """Return a repository-relative manifest path when it is inside the root."""
    try:
        return manifest_path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _checked_out_artifact(root: Path, relative_path: str) -> Path:
    """Return a tracked artifact only when no path component is a symlink."""
    artifact_path = root
    for component in Path(relative_path).parts:
        artifact_path /= component
        if artifact_path.is_symlink():
            raise ReleaseEvidenceError("release evidence rejects symlinked artifacts")
    return artifact_path


def _require_version(version: object) -> str:
    """Require a semantic release version without accepting mutable labels."""
    if not isinstance(version, str) or SEMVER_PATTERN.fullmatch(version) is None:
        raise ReleaseEvidenceError("release version must be semantic versioning")
    return version


def _sha256(path: Path) -> str:
    """Hash one checked-out artifact's current bytes."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise ReleaseEvidenceError("release artifact bytes are unreadable") from error


def build_manifest(
    root: Path, release_version: str, manifest_path: Path
) -> dict[str, Any]:
    """Build a deterministic manifest for tracked files except its own path."""
    root = _worktree_root(root)
    version = _require_version(release_version)
    commit = _source_commit(root)
    _require_clean_checkout(root, commit)
    excluded_path = _manifest_relative_path(root, manifest_path)
    artifacts = [
        {"path": path, "sha256": _sha256(_checked_out_artifact(root, path))}
        for path in _tracked_paths(root)
        if path != excluded_path
    ]
    if not artifacts:
        raise ReleaseEvidenceError("release evidence requires tracked artifacts")
    _require_clean_checkout(root, commit)
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
    try:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with manifest_path.open("x", encoding="utf-8") as output:
            output.write(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    except FileExistsError as error:
        raise ReleaseEvidenceError("release manifest destination already exists") from error
    except OSError as error:
        raise ReleaseEvidenceError("release manifest could not be written") from error
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
    if set(manifest) != MANIFEST_FIELDS:
        raise ReleaseEvidenceError("release manifest fields are invalid")
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
        if set(artifact) != ARTIFACT_FIELDS:
            raise ReleaseEvidenceError("release manifest artifact fields are invalid")
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


def verify_manifest(root: Path, manifest_path: Path, expected_version: str) -> None:
    """Verify commit identity, tracked-file inventory, and every artifact hash."""
    expected_version = _require_version(expected_version)
    manifest = _load_manifest(manifest_path)
    root = _worktree_root(root)
    _require_clean_checkout(root, manifest["source_commit"])
    errors: list[str] = []
    if manifest["release_version"] != expected_version:
        errors.append("release version does not match the requested version")
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
        artifact_path = _checked_out_artifact(root, path)
        if not artifact_path.is_file():
            errors.append(f"missing artifact bytes: {path}")
            continue
        if _sha256(artifact_path) != expected_hash:
            errors.append(f"artifact hash mismatch: {path}")
    _require_clean_checkout(root, manifest["source_commit"])
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
    verify_parser.add_argument("--release-version", required=True)
    options = parser.parse_args(arguments)
    try:
        if options.operation == "manifest":
            create_manifest(options.root, options.release_version, options.output)
            print("release manifest created")
        else:
            verify_manifest(options.root, options.manifest, options.release_version)
            print("release manifest verified")
    except ReleaseEvidenceError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
