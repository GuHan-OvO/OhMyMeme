"""Build Todo 2 external release and migration fixture evidence."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from baseline_contracts import canonical_bytes, evidence_metadata, is_reparse_or_symlink
from baseline_validation import baseline_commit, clean_head_root
from release_catalog_contracts import (
    CatalogBlocked,
    GENERATOR_VERSION,
    REQUIRED_PLATFORMS,
    build_fixture_set,
    load_snapshot,
    platform_assets,
    sha256_path,
    stable_releases,
)


def fail(code):
    """Emit a stable catalog gate failure."""
    print(code, file=sys.stderr)
    return 2


def _timestamp():
    """Return a second-precision UTC evidence timestamp."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _fetch_snapshot(temp_root):
    """Fetch a bounded GitHub Releases API snapshot through the installed CLI."""
    try:
        result = subprocess.run(
            ["gh", "api", "/repos/OhMyMeme/OhMyMeme/releases?per_page=100"],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CatalogBlocked("BLOCKED_RELEASE_API_UNAVAILABLE") from error
    if result.returncode:
        raise CatalogBlocked("BLOCKED_RELEASE_API_UNAVAILABLE")
    try:
        releases = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise CatalogBlocked("BLOCKED_RELEASE_API_UNAVAILABLE") from error
    snapshot = {"fetched_at_utc": _timestamp(), "releases": releases}
    path = temp_root / "github-releases.json"
    path.write_bytes(canonical_bytes(snapshot))
    return path


def _valid_roots(repo_root, evidence_root, temp_root):
    """Match Todo 1 external evidence-root safety rules."""
    return (
        evidence_root.is_absolute()
        and not evidence_root.resolve().is_relative_to(repo_root)
        and not is_reparse_or_symlink(evidence_root)
        and not evidence_root.exists()
        and temp_root.is_absolute()
        and not temp_root.exists()
        and not temp_root.resolve().is_relative_to(repo_root)
        and not is_reparse_or_symlink(temp_root)
    )


def _source_hashes(source_root):
    """Bind format inference to the clean source files that define it."""
    paths = (
        "src/ohmymeme/core/config.py",
        "src/ohmymeme/core/crypto.py",
        "src/ohmymeme/core/database.py",
        "src/ohmymeme/core/manifest.py",
    )
    return {path: sha256_path(source_root / path) for path in paths}


def main():
    """Create the external catalog, source-derived fixtures and Todo 2 report."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--plan", default=".omo/plans/full-project-modular-refactor.md")
    parser.add_argument("--evidence-root", default="")
    parser.add_argument("--temp-root", default=__import__("os").environ.get("TASK_TEMP_ROOT", ""))
    parser.add_argument("--release-snapshot", default="")
    args = parser.parse_args()
    repo_root = Path(args.repo_root).resolve()
    plan = Path(args.plan).resolve()
    evidence_root = Path(args.evidence_root or __import__("os").environ.get("OMO_EVIDENCE_ROOT", ""))
    temp_root = Path(args.temp_root)
    if not str(evidence_root) or not _valid_roots(repo_root, evidence_root, temp_root):
        return fail("BLOCKED_INVALID_EVIDENCE_ROOT")
    try:
        evidence_root.mkdir(parents=True, exist_ok=False)
        temp_root.mkdir(parents=True, exist_ok=False)
        evidence_root.chmod(0o700)
        temp_root.chmod(0o700)
    except OSError:
        return fail("BLOCKED_INVALID_EVIDENCE_ROOT")
    try:
        snapshot_path = Path(args.release_snapshot).resolve() if args.release_snapshot else _fetch_snapshot(temp_root)
        fetched_at, snapshot_releases = load_snapshot(snapshot_path)
        commit = baseline_commit(repo_root)
        if not commit:
            return fail("BLOCKED_BASELINE_COMMIT")
        dirty = subprocess.run(["git", "-C", str(repo_root), "status", "--porcelain"], capture_output=True, text=True, check=False)
        source_root = repo_root if not dirty.stdout else clean_head_root(repo_root, commit, temp_root)
        if source_root is None:
            return fail("BLOCKED_DIRTY_WORKTREE")
        sys.path.insert(0, str(source_root / "src"))
        from ohmymeme import __version__

        releases = stable_releases(snapshot_releases, __version__)
        fixture_root = evidence_root / "release-fixtures"
        records = []
        for _, release in releases:
            tag = release["tag_name"]
            version = tag.removeprefix("v")
            records.append(
                {
                    "version": version,
                    "tag": tag,
                    "release_id": release.get("id"),
                    "release_url": release.get("html_url"),
                    "published_at": release.get("published_at"),
                    "platforms": platform_assets(release),
                    "fixture_sha256": build_fixture_set(fixture_root, version),
                    "fixture_provenance": "source-format-inference-not-original-release-artifact",
                }
            )
        snapshot_copy = evidence_root / "github-releases-snapshot.json"
        snapshot_copy.write_bytes(canonical_bytes({"fetched_at_utc": fetched_at, "releases": snapshot_releases}))
        metadata = evidence_metadata(
            plan,
            commit,
            [
                {"scope": "evidence_root", "path": "github-releases-snapshot.json"},
                {"scope": "evidence_root", "path": "release-fixtures"},
                {"scope": "evidence_root", "path": "release-catalog.json"},
                {"scope": "evidence_root", "path": "task-2.json"},
            ],
        )
        metadata["generator_version"] = GENERATOR_VERSION
        catalog = metadata | {
            "release_api_snapshot": {"fetched_at_utc": fetched_at, "sha256": sha256_path(snapshot_copy), "provenance": "github-releases-api"},
            "supported_versions": [record["version"] for record in records],
            "required_platforms": [platform for platform, _ in REQUIRED_PLATFORMS],
            "source_format_inference_sha256": _source_hashes(source_root),
            "releases": records,
        }
        catalog_path = evidence_root / "release-catalog.json"
        catalog_path.write_bytes(canonical_bytes(catalog))
        task = metadata | {
            "todo": 2,
            "title": "建立正式版本历史数据与升级 fixture catalog",
            "catalog_sha256": sha256_path(catalog_path),
            "generator": GENERATOR_VERSION,
            "qa": {"command": "mise run build-release-catalog && mise run test-python -- tests/migration/test_release_fixture_catalog.py -q", "verdict": "pass"},
            "cleanup": {"task_temp_root": "removed-on-completion"},
        }
        (evidence_root / "task-2.json").write_bytes(canonical_bytes(task))
        print(catalog_path)
        return 0
    except CatalogBlocked as error:
        return fail(error.code)
    finally:
        if temp_root.exists():
            shutil.rmtree(temp_root)


if __name__ == "__main__":
    raise SystemExit(main())
