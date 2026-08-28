"""Build a deterministic SPDX 3.0.1 JSON-LD document for release artifacts."""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote


SPDX_CONTEXT = "https://spdx.org/rdf/3.0.1/spdx-context.jsonld"
RELEASE_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def build_sbom(
    artifact_root: Path,
    output_path: Path,
    release_sha: str,
    created_at: str,
) -> dict[str, object]:
    """Return a reproducible SPDX graph for every file below *artifact_root*."""
    release_sha = _validate_release_sha(release_sha)
    created = _canonical_created_at(created_at)
    root = artifact_root.resolve()
    output = output_path.resolve()
    if not root.is_dir():
        raise ValueError(f"artifact root is not a directory: {artifact_root}")
    artifact_paths = tuple(
        path
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.resolve() != output
    )
    if not artifact_paths:
        raise ValueError(f"artifact root has no files: {artifact_root}")

    document_id = f"https://github.com/ContextualWisdomLab/metering-billing-platform/sbom/{release_sha}"
    creation_info_id = f"{document_id}#creation-info"
    sbom_id = f"{document_id}#producer-artifacts"
    package_ids: list[str] = []
    packages: list[dict[str, object]] = []
    for artifact_path in artifact_paths:
        relative_path = artifact_path.relative_to(root).as_posix()
        digest = sha256(artifact_path.read_bytes()).hexdigest()
        package_id = f"{document_id}#artifact/{quote(relative_path, safe='')}"
        package_ids.append(package_id)
        packages.append(
            {
                "type": "software_Package",
                "spdxId": package_id,
                "name": relative_path,
                "creationInfo": creation_info_id,
                "verifiedUsing": [
                    {"type": "Hash", "algorithm": "sha256", "hashValue": digest}
                ],
                "software_sourceInfo": f"release commit {release_sha}",
            }
        )

    creation_info = {
        "type": "CreationInfo",
        "created": created,
        "createdBy": ["https://github.com/ContextualWisdomLab"],
        "specVersion": "3.0.1",
    }
    sbom = {
        "type": "software_Sbom",
        "spdxId": sbom_id,
        "name": "CWL producer SDK release artifacts",
        "creationInfo": creation_info_id,
        "element": package_ids,
        "rootElement": package_ids,
        "profileConformance": ["core", "software"],
        "software_sbomType": ["build"],
    }
    document = {
        "type": "SpdxDocument",
        "spdxId": document_id,
        "name": "Metering Billing Platform producer SDK release",
        "creationInfo": creation_info_id,
        "dataLicense": "https://spdx.org/licenses/CC0-1.0",
        "element": [sbom_id, *package_ids],
        "rootElement": [sbom_id],
        "profileConformance": ["core", "software"],
    }
    return {"@context": SPDX_CONTEXT, "@graph": [document, sbom, creation_info, *packages]}


def write_sbom(
    artifact_root: Path,
    output_path: Path,
    release_sha: str,
    created_at: str,
) -> None:
    """Write the deterministic SPDX document without timestamps or host paths."""
    document = build_sbom(artifact_root, output_path, release_sha, created_at)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _validate_release_sha(release_sha: str) -> str:
    """Require the full immutable Git commit identity used by the evidence."""
    if not RELEASE_SHA_PATTERN.fullmatch(release_sha):
        raise ValueError("release SHA must be a lowercase 40-character Git commit SHA")
    return release_sha


def _canonical_created_at(created_at: str) -> str:
    """Normalize a timezone-aware timestamp to second-precision UTC."""
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("created-at must be an ISO 8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("created-at must include a timezone")
    return parsed.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_arguments() -> argparse.Namespace:
    """Parse the release evidence command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--created-at", required=True)
    return parser.parse_args()


def main() -> int:
    """Build one SPDX file and report its path."""
    arguments = _parse_arguments()
    write_sbom(
        arguments.artifact_root,
        arguments.output,
        arguments.release_sha,
        arguments.created_at,
    )
    print(arguments.output)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
