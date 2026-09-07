"""Fail-closed verification for Todo 1 external evidence."""

import hashlib
import re
import subprocess
import tarfile
from io import BytesIO
from pathlib import Path

from baseline_contracts import DIMENSIONS, GENERATOR_VERSION, MAPPINGS, METADATA_FIELDS, is_reparse_or_symlink, sha256_path, source_anchors


def baseline_commit(repo_root):
    """Return the immutable commit that a freeze is permitted to represent."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True,
        check=False,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def clean_head_root(repo_root, commit, temp_root):
    """Materialize committed sources in task-owned storage when the tree is dirty."""
    result = subprocess.run(["git", "-C", str(repo_root), "archive", "--format=tar", commit], capture_output=True, check=False)
    if result.returncode:
        return None
    root = temp_root / "clean-head"
    with tarfile.open(fileobj=BytesIO(result.stdout)) as archive:
        members = archive.getmembers()
        if any(member.name.startswith("/") or ".." in Path(member.name).parts for member in members):
            return None
        archive.extractall(root, members, filter="data")
    return root


def valid_metadata(document, plan, commit):
    """Require exact common evidence metadata rather than truthy placeholders."""
    expected_hash = hashlib.sha256(plan.read_bytes()).hexdigest()
    return (
        all(field in document for field in METADATA_FIELDS)
        and document["schema_version"] == 1
        and document["plan_sha256"] == expected_hash
        and document["baseline_commit"] == commit
        and re.fullmatch(r"[0-9a-f]{40}", commit or "") is not None
        and document["generator_version"] == GENERATOR_VERSION
        and document["verdict"] == "pass"
        and isinstance(document["evidence_files"], list)
    )


def valid_mappings(inventory, repo_root):
    """Authenticate every legacy mapping by exact identity, kind, hash and anchors."""
    entries = inventory.get("legacy_mappings")
    if not isinstance(entries, list) or len(entries) != len(MAPPINGS):
        return False
    expected_entries = {legacy: (paths, kinds) for legacy, paths, kinds in MAPPINGS}
    if {entry.get("legacy_path") for entry in entries} != set(expected_entries):
        return False
    for entry in entries:
        expected = expected_entries[entry["legacy_path"]]
        current_paths, kinds = expected
        if entry.get("current_path") != list(current_paths) or entry.get("source_or_host_or_generated") != list(kinds):
            return False
        if not entry.get("source_line_anchors") or not isinstance(entry["source_line_anchors"], list):
            return False
        hashes = entry.get("existence_sha256")
        if not isinstance(hashes, list) or len(hashes) != len(current_paths) or any(re.fullmatch(r"[0-9a-f]{64}", value or "") is None or value == "0" * 64 for value in hashes):
            return False
        source_hashes = []
        source_anchors = []
        for path in current_paths:
            source = repo_root / path
            if not source.exists() or source.is_symlink():
                return False
            source_hashes.append(sha256_path(source))
            source_anchors.extend(source_anchors_for(source))
        if hashes != source_hashes or entry["source_line_anchors"] != sorted(set(source_anchors)):
            return False
    return True


def source_anchors_for(path):
    """Keep clean-source anchor calculation separate from evidence fields."""
    return source_anchors(path)


def valid_lan_contract(inventory, repo_root):
    """Bind LAN v1 claims to the immutable protocol source contract."""
    source = (repo_root / "src/ohmymeme/services/lan/protocol.py").read_text(encoding="utf-8")
    expected = ["json.dumps ensure_ascii=False", "big-endian length", "PBKDF2-SHA256 100000", "AES-GCM", "Chinese errors"]
    return inventory.get("public_surfaces", {}).get("lan", {}).get("v1") == expected and all(token in source for token in ("ensure_ascii=False", "struct.pack(\">I\"", "100000", "AESGCM", "帧过大"))


def valid_companions(inventory, evidence_root, plan, commit, loader, canonical_bytes):
    """Verify applicability and task reports share metadata and cite concrete proof."""
    try:
        applicability = loader((evidence_root / "task-applicability.json").read_bytes())
        task = loader((evidence_root / "task-1.json").read_bytes())
    except (OSError, ValueError):
        return False
    if not valid_metadata(applicability, plan, commit) or not valid_metadata(task, plan, commit) or task.get("title") != "冻结公开契约、产品行为与旧架构 inventory":
        return False
    tasks = applicability.get("tasks", {})
    if set(tasks) != {str(number) for number in range(1, 33)}:
        return False
    targets = set()
    artifacts = {"inventory.json": inventory, "task-applicability.json": applicability, "task-1.json": task}
    for item in tasks.values():
        dimensions = item.get("dimensions", {})
        if set(dimensions) != set(DIMENSIONS):
            return False
        for value in dimensions.values():
            status = value.get("status")
            if status == "required":
                reference = value.get("evidence_file", {})
                if reference.get("scope") != "evidence_root" or reference.get("path") not in artifacts or not isinstance(reference.get("pointer"), str):
                    return False
                target = (reference["path"], reference["pointer"])
                if target in targets or not _resolve_pointer(artifacts[reference["path"]], reference["pointer"]):
                    return False
                targets.add(target)
            elif status != "not_applicable" or not value.get("reason"):
                return False
    return task.get("todo") == 1 and task.get("inventory_sha256") == hashlib.sha256(canonical_bytes(inventory)).hexdigest() and task.get("applicability_sha256") == hashlib.sha256(canonical_bytes(applicability)).hexdigest()


def _resolve_pointer(document, pointer):
    """Resolve one RFC 6901 pointer and reject missing or wide targets."""
    if not pointer.startswith("/") or "*" in pointer:
        return False
    value = document
    try:
        for token in pointer[1:].split("/"):
            key = token.replace("~1", "/").replace("~0", "~")
            value = value[key] if isinstance(value, dict) else value[int(key)]
    except (IndexError, KeyError, TypeError, ValueError):
        return False
    return isinstance(value, dict) and value == {"recorded": True}
