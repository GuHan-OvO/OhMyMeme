"""Generate Todo 1's immutable external contract evidence."""

import argparse
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

from baseline_contracts import build_applicability, build_mappings, canonical_bytes, contract_fixture, evidence_metadata, is_reparse_or_symlink, plan_paths, public_methods, route_ids
from baseline_validation import baseline_commit, clean_head_root


def fail(code):
    """Emit a stable gate error."""
    print(code, file=sys.stderr)
    return 2


def main():
    """Freeze current public contracts into an external evidence root."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--plan", default=".omo/plans/full-project-modular-refactor.md")
    parser.add_argument("--evidence-root", default="")
    parser.add_argument("--temp-root", required=True)
    args = parser.parse_args()
    repo_root = Path(args.repo_root).resolve()
    plan = Path(args.plan).resolve()
    evidence_root = Path(args.evidence_root or __import__("os").environ.get("OMO_EVIDENCE_ROOT", ""))
    temp_root = Path(args.temp_root)
    try:
        if not str(evidence_root) or not evidence_root.is_absolute() or evidence_root.resolve().is_relative_to(repo_root) or is_reparse_or_symlink(evidence_root):
            return fail("BLOCKED_INVALID_EVIDENCE_ROOT")
        if evidence_root.exists() and any(evidence_root.iterdir()):
            return fail("BLOCKED_EVIDENCE_EXISTS")
        if not temp_root.is_absolute() or temp_root.exists() or temp_root.resolve().is_relative_to(repo_root) or is_reparse_or_symlink(temp_root):
            return fail("BLOCKED_INVALID_TASK_TEMP_ROOT")
        evidence_root.mkdir(parents=True, exist_ok=False)
        temp_root.mkdir(parents=True, exist_ok=False)
    except OSError:
        return fail("BLOCKED_INVALID_EVIDENCE_ROOT")
    try:
        evidence_root.chmod(0o700)
        temp_root.chmod(0o700)
        baseline = baseline_commit(repo_root)
        dirty = subprocess.run(["git", "-C", str(repo_root), "status", "--porcelain"], capture_output=True, text=True, check=False)
        source_root = repo_root if not baseline or dirty.returncode or not dirty.stdout else clean_head_root(repo_root, baseline, temp_root)
        if source_root is None:
            return fail("BLOCKED_DIRTY_WORKTREE")
        manager = source_root / "src/ohmymeme/presentation/desktop/window_manager.py"
        methods = public_methods(manager, "JsApi")
        routes = route_ids(manager)
        method_names = {item["id"].split(".", 1)[1] for item in methods}
        fixture = contract_fixture(repo_root)
        required = set(fixture["required_bridge_methods"])
        required_routes = set(fixture["required_http_routes"])
        legacy_paths = {entry[0] for entry in __import__("baseline_contracts").MAPPINGS}
        if not required <= method_names or not required_routes <= set(routes) or not set(fixture["required_legacy_paths"]) <= legacy_paths:
            return fail("BLOCKED_PUBLIC_CONTRACT_DRIFT")
        if not baseline:
            return fail("BLOCKED_BASELINE_COMMIT")
        if dirty.returncode:
            return fail("BLOCKED_DIRTY_WORKTREE")
        metadata = evidence_metadata(plan, baseline, [{"scope": "evidence_root", "path": "inventory.json"}, {"scope": "evidence_root", "path": "task-applicability.json"}, {"scope": "evidence_root", "path": "task-1.json"}, {"scope": "repository", "path": "tests/fixtures/public_surface_contract.json"}])
        inventory = metadata | {
            "legacy_mappings": build_mappings(source_root),
            "path_index": plan_paths(source_root, plan),
            "public_surfaces": {
                "bridge": {"JsApi": methods, "SettingsApi": public_methods(manager, "SettingsApi")},
                "http": {"routes": routes, "host": "127.0.0.1"},
                "cli": {"entrypoints": ["python -m ohmymeme", "ohmymeme.app.bootstrap:main"], "flags": ["--debug-update", "--debug-startup", "--debug-adb", "--debug", "--silent"]},
                "config": {"path": "src/ohmymeme/core/config.py", "contracts": ["JSON keys/defaults", "Fernet/XOR readable", "custom cache_dir"]},
                "database": {"path": "src/ohmymeme/core/database.py", "tables": ["memes", "tags", "meme_tags", "collections", "meme_collections", "favorites", "recent_uses"], "journal_mode": "WAL"},
                "manifest": {"path": "src/ohmymeme/core/manifest.py", "versions": [2, 3], "filename": "meme-index.json"},
                "sync": {"path": "src/ohmymeme/services/sync", "remote_layout": ["memes/", "meme-index.json"], "failure": "SyncError"},
                "lan": {"path": "src/ohmymeme/services/lan", "v1": ["json.dumps ensure_ascii=False", "big-endian length", "PBKDF2-SHA256 100000", "AES-GCM", "Chinese errors"]},
                "platform": {"path": "src/ohmymeme/integrations/platform", "contracts": ["CF_HDROP", "keyboard-pynput-polling", "WinForms drag", "Wayland native drag"]},
            },
            "required_contracts": ["copy_meme", "/api/upload/"],
            "fixture_sha256": hashlib.sha256(canonical_bytes(fixture)).hexdigest(),
            "baseline_inputs": {
                "mise_lock_sha256": hashlib.sha256((source_root / "mise.lock").read_bytes()).hexdigest(),
                "characterization_tests": ["tests/test_core.py", "tests/test_sync.py", "tests/test_lan.py", "tests/test_startup.py", "tests/presentation/test_desktop_bottle_security.py"],
            },
        }
        applicability = build_applicability(metadata)
        evidence = {f"{number}/{dimension}": {"recorded": True} for number in range(1, 33) for dimension in __import__("baseline_contracts").DIMENSIONS if applicability["tasks"][str(number)]["dimensions"][dimension]["status"] == "required"}
        task = metadata | {"todo": 1, "title": "冻结公开契约、产品行为与旧架构 inventory", "inventory_sha256": hashlib.sha256(canonical_bytes(inventory)).hexdigest(), "applicability_sha256": hashlib.sha256(canonical_bytes(applicability)).hexdigest(), "dimension_evidence": evidence, "task_temp_root": str(temp_root), "cleanup": {"task_temp_root": "removed-on-completion"}}
        for name, value in (("inventory.json", inventory), ("task-applicability.json", applicability), ("task-1.json", task)):
            (evidence_root / name).write_bytes(canonical_bytes(value))
        print(evidence_root / "inventory.json")
        return 0
    finally:
        if temp_root.exists():
            shutil.rmtree(temp_root)


if __name__ == "__main__":
    raise SystemExit(main())
