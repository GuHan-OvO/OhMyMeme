"""Release-catalog input binding for the six package smoke targets."""

import re

from scripts.package_smoke import ArtifactContract


TARGETS = (
    ("windows-x64", "windows-x86_64"),
    ("linux-appimage-x64", "linux-x86_64-appimage"),
    ("linux-deb-amd64", "linux-x86_64-deb"),
    ("linux-rpm-x64", "linux-x86_64-rpm"),
    ("macos-arm64", "macos-arm64"),
    ("macos-x86_64", "macos-x86_64"),
)
CATALOG_FILENAMES = {
    "windows-x64": "OhMyMeme-{version}-setup.exe",
    "linux-appimage-x64": "OhMyMeme-v{version}-x86_64.AppImage",
    "linux-deb-amd64": "OhMyMeme-v{version}-amd64.deb",
    "linux-rpm-x64": "OhMyMeme-v{version}-x86_64.rpm",
    "macos-arm64": "OhMyMeme-v{version}-arm64.dmg",
    "macos-x86_64": "OhMyMeme-v{version}-x86_64.dmg",
}
SHA256 = re.compile(r"[0-9a-f]{64}")


class PackageInputViolation(RuntimeError):
    """A catalog cannot authenticate a required package lifecycle input."""


def catalog_asset(release, platform, expected_filename):
    """Return the one original release asset that a target may use as input."""
    platforms = release.get("platforms") if isinstance(release, dict) else None
    if not isinstance(platforms, list):
        raise PackageInputViolation("BLOCKED_MISSING_RELEASE_ASSET")
    matches = [asset for asset in platforms if isinstance(asset, dict) and asset.get("platform") == platform]
    if len(matches) != 1:
        raise PackageInputViolation("BLOCKED_MISSING_RELEASE_ASSET")
    asset = matches[0]
    if (
        not isinstance(asset.get("asset_name"), str)
        or not isinstance(asset.get("asset_url"), str)
        or not isinstance(asset.get("sha256"), str)
        or SHA256.fullmatch(asset["sha256"]) is None
        or asset["asset_name"] != expected_filename
        or asset.get("provenance") != "github-release-original"
        or asset.get("original_asset_available") is not True
    ):
        raise PackageInputViolation("BLOCKED_MISSING_RELEASE_ASSET")
    return asset


def target_matrix(report, catalog):
    """Bind every required lifecycle definition to candidate and rollback hashes."""
    rows = report.get("targets") if isinstance(report, dict) else None
    if (
        not isinstance(report, dict)
        or report.get("contract_only") is not True
        or not isinstance(rows, list)
        or [row.get("target") for row in rows] != [target for target, _ in TARGETS]
    ):
        raise PackageInputViolation("BLOCKED_INVALID_CONTRACT_REPORT")
    releases = catalog["releases"]
    current = releases[0]
    previous = releases[1]
    current_version = current.get("version") if isinstance(current, dict) else None
    previous_version = previous.get("version") if isinstance(previous, dict) else None
    if not isinstance(current_version, str) or not isinstance(previous_version, str):
        raise PackageInputViolation("BLOCKED_MISSING_RELEASE_ASSET")
    matrix = []
    for row, (target, platform) in zip(rows, TARGETS, strict=True):
        if row.get("package_version") != current_version or row.get("valid") is not True:
            raise PackageInputViolation("BLOCKED_INVALID_CONTRACT_REPORT")
        contract = ArtifactContract.default(target, current_version)
        candidate = catalog_asset(
            current, platform, CATALOG_FILENAMES[target].format(version=current_version)
        )
        rollback = catalog_asset(
            previous, platform, CATALOG_FILENAMES[target].format(version=previous_version)
        )
        matrix.append(
            {
                "target": target,
                "required": True,
                "filename": row["filename"],
                "architecture": row["architecture"],
                "runner": contract.lifecycle_probes[0].runner,
                "identity": {
                    "app_id": contract.app_id,
                    "bundle_id": contract.bundle_id,
                    "package_name": contract.package_name,
                    "license": contract.license,
                },
                "resources": list(contract.resources),
                "candidate_input": {
                    "version": current_version,
                    "asset_name": candidate["asset_name"],
                    "asset_url": candidate["asset_url"],
                    "sha256": candidate["sha256"],
                },
                "rollback_input": {
                    "version": previous_version,
                    "asset_name": rollback["asset_name"],
                    "asset_url": rollback["asset_url"],
                    "sha256": rollback["sha256"],
                },
                "lifecycle_probes": row["lifecycle_probes"],
            }
        )
    return matrix
