"""Validate Todo 1's inventory against every current plan reference."""

import argparse
import json
import sys
import tempfile
from pathlib import Path

from baseline_contracts import canonical_bytes, canonical_load, plan_paths
from baseline_validation import baseline_commit, clean_head_root, valid_companions, valid_lan_contract, valid_metadata, valid_mappings


def fail(code):
    """Emit a stable gate error."""
    print(code, file=sys.stderr)
    return 2


def main():
    """Resolve plan paths only through the immutable inventory fields."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--inventory", required=True)
    args = parser.parse_args()
    repo_root = Path(args.repo_root).resolve()
    plan = Path(args.plan).resolve()
    try:
        inventory = canonical_load(Path(args.inventory).read_bytes())
    except (OSError, ValueError, json.JSONDecodeError):
        return fail("BLOCKED_INVALID_EVIDENCE")
    commit = baseline_commit(repo_root)
    dirty = __import__("subprocess").run(["git", "-C", str(repo_root), "status", "--porcelain"], capture_output=True, text=True, check=False)
    with tempfile.TemporaryDirectory(prefix="ohmymeme-resolve-") as temporary:
        source_root = repo_root if not dirty.stdout else clean_head_root(repo_root, commit, Path(temporary))
        valid_source = source_root is not None and valid_mappings(inventory, source_root) and valid_lan_contract(inventory, source_root)
    if not valid_metadata(inventory, plan, commit):
        return fail("BLOCKED_INVALID_EVIDENCE")
    if not valid_source:
        return fail("BLOCKED_INVALID_MAPPING")
    if not valid_companions(inventory, Path(args.inventory).resolve().parent, plan, commit, canonical_load, canonical_bytes):
        return fail("BLOCKED_INVALID_COMPANION_EVIDENCE")
    indexed = {item["path"] for item in inventory.get("path_index", [])}
    for entry in inventory.get("legacy_mappings", []):
        for path in entry.get("current_path", []):
            if not (repo_root / path).exists():
                return fail("BLOCKED_UNRESOLVED_PLAN_REFERENCE")
            indexed.add(path)
    required = {item["path"] for item in plan_paths(repo_root, plan)}
    if not required <= indexed:
        return fail("BLOCKED_UNRESOLVED_PLAN_REFERENCE")
    print("PLAN_REFERENCES_RESOLVED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
