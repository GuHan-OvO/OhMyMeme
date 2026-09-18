"""Todo 1 evidence construction and validation helpers."""

import ast
import hashlib
import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path


GENERATOR_VERSION = "todo-1-freeze-baseline/1"
METADATA_FIELDS = (
    "schema_version",
    "plan_sha256",
    "baseline_commit",
    "generated_at_utc",
    "generator_version",
    "verdict",
    "evidence_files",
)
DIMENSIONS = (
    "inputs",
    "outputs",
    "safety",
    "recovery",
    "compatibility",
    "update",
    "privacy",
    "legal",
    "stable-cleanup",
)
MAPPINGS = (
    ("src/webui.py", ("src/ohmymeme/presentation/desktop/window_manager.py",), ("source",)),
    ("src/config.py", ("src/ohmymeme/core/config.py",), ("source",)),
    ("src/database.py", ("src/ohmymeme/core/database.py",), ("source",)),
    ("src/manifest.py", ("src/ohmymeme/core/manifest.py",), ("source",)),
    ("src/sync.py", ("src/ohmymeme/services/sync",), ("source",)),
    ("src/lan.py", ("src/ohmymeme/services/lan",), ("source",)),
    ("src/updater.py", ("src/ohmymeme/services/updates.py",), ("source",)),
    ("src/vue-src/", ("src/ohmymeme/presentation/frontend/main",), ("source",)),
    ("src/webui/settings.*", ("src/ohmymeme/presentation/frontend/settings", "src/webui/settings.js"), ("source", "generated")),
    ("src/main.py", ("src/ohmymeme/app/bootstrap.py",), ("source",)),
    ("src/__main__.py", ("src/ohmymeme/__main__.py",), ("source",)),
    ("src/clipboard_util.py", ("src/ohmymeme/integrations/platform/clipboard.py",), ("source",)),
    ("src/hotkey.py", ("src/ohmymeme/integrations/platform/hotkey.py",), ("source",)),
    ("src/tray.py", ("src/ohmymeme/integrations/platform/tray.py",), ("source",)),
    ("src/native_drag.py", ("src/ohmymeme/integrations/platform/native_drag.py",), ("source",)),
    ("src/platform_util.py", ("src/ohmymeme/integrations/platform/system.py",), ("source",)),
    ("src/adb_util.py", ("src/ohmymeme/integrations/imports/adb_qq.py",), ("source",)),
    ("src/tg_stickers.py", ("src/ohmymeme/integrations/imports/telegram.py",), ("source",)),
    ("src/douyin.py", ("src/ohmymeme/integrations/imports/douyin.py",), ("source",)),
    ("src/wechat_probe.py", ("src/ohmymeme/integrations/imports/wechat.py",), ("source",)),
    ("src/qqnt_extract.py", ("src/ohmymeme/integrations/imports/qqnt.py",), ("source",)),
    ("src/crypto_util.py", ("src/ohmymeme/core/crypto.py",), ("source",)),
    ("src/vue-src/types/", ("src/ohmymeme/presentation/frontend/main/shared/types.ts",), ("source",)),
)


def canonical_bytes(value):
    """Return the RFC 8785-compatible canonical bytes used by this evidence schema."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def canonical_load(data):
    """Load and prove that evidence bytes are duplicate-free canonical JSON."""
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    value = json.loads(data, object_pairs_hook=reject_duplicates)
    if canonical_bytes(value) != data:
        raise ValueError("non-canonical JSON evidence")
    return value


def sha256_path(path):
    """Hash a file or directory deterministically."""
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(path.read_bytes())
    else:
        for child in sorted(item for item in path.rglob("*") if item.is_file() and "__pycache__" not in item.parts and item.suffix != ".pyc"):
            digest.update(child.relative_to(path).as_posix().encode("utf-8"))
            digest.update(child.read_bytes())
    return digest.hexdigest()


def source_anchors(path):
    """Record public definitions and route declarations with stable line anchors."""
    if path.is_dir():
        return [f"{child.relative_to(path).as_posix()}:line:1" for child in sorted(path.rglob("*")) if child.is_file() and "__pycache__" not in child.parts and child.suffix in {".py", ".ts", ".vue", ".js", ".mjs"}]
    if not path.is_file() or path.suffix != ".py":
        return ["line:1"]
    tree = ast.parse(path.read_text(encoding="utf-8"))
    anchors = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            anchors.append(f"line:{node.lineno}:{node.name}")
    return sorted(anchors) or ["line:1"]


def public_methods(path, class_name):
    """Extract exact callable names and signatures from a public bridge class."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    target_name = class_name
    if not any(
        isinstance(node, ast.ClassDef) and node.name == class_name for node in tree.body
    ):
        facade_path = path.parent / "api" / "facades.py"
        if facade_path.is_file():
            facade_tree = ast.parse(facade_path.read_text(encoding="utf-8"))
            for node in facade_tree.body:
                if not isinstance(node, ast.Assign):
                    continue
                if not any(
                    isinstance(target, ast.Name) and target.id == class_name
                    for target in node.targets
                ):
                    continue
                if isinstance(node.value, ast.Name):
                    target_name = node.value.id
                    for imported in facade_tree.body:
                        if (
                            isinstance(imported, ast.ImportFrom)
                            and imported.module
                            and any(item.name == target_name for item in imported.names)
                        ):
                            implementation = (
                                facade_path.parent / imported.module.replace(".", "/")
                            ).with_suffix(".py")
                            if implementation.is_file():
                                tree = ast.parse(
                                    implementation.read_text(encoding="utf-8")
                                )
                            break
                    break
    results = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == target_name:
            for member in node.body:
                if isinstance(member, ast.FunctionDef) and not member.name.startswith("_"):
                    returns = []
                    for value in ast.walk(member):
                        if isinstance(value, ast.Return):
                            returns.append(ast.unparse(value.value) if value.value else "None")
                    results.append({
                        "id": f"{class_name}.{member.name}",
                        "line": member.lineno,
                        "signature": ast.unparse(member.args),
                        "return_schema": ast.unparse(member.returns) if member.returns else sorted(set(returns)),
                        "safety_exceptions": [],
                    })
    return sorted(results, key=lambda item: item["id"])


def route_ids(path):
    """Extract Bottle route literals from the legacy desktop host."""
    return sorted(set(re.findall(r'@app\.route\(["\']([^"\']+)', path.read_text(encoding="utf-8"))))


def contract_fixture(repo_root):
    """Load the durable public-surface fixture committed with the source tree."""
    return json.loads((repo_root / "tests/fixtures/public_surface_contract.json").read_text(encoding="utf-8"))


def build_mappings(repo_root):
    """Create the explicit legacy-to-current mapping inventory."""
    entries = []
    for legacy_path, current_paths, kinds in MAPPINGS:
        hashes = []
        anchors = []
        for relative_path in current_paths:
            path = repo_root / relative_path
            if not path.exists():
                raise ValueError(f"BLOCKED_UNRESOLVED_PLAN_REFERENCE:{relative_path}")
            hashes.append(sha256_path(path))
            anchors.extend(source_anchors(path))
        entries.append({
            "legacy_path": legacy_path,
            "current_path": list(current_paths),
            "source_or_host_or_generated": list(kinds),
            "owner": "todo-1-contract-freeze",
            "public_or_internal": "public",
            "replacement_task": 32,
            "delete_authorization": "stable-compatibility-report-required",
            "existence_sha256": hashes,
            "source_line_anchors": sorted(set(anchors)),
        })
    return entries


def plan_paths(repo_root, plan):
    """Index every existing repository path referenced by the work plan."""
    matches = re.findall(r'(?<![\w.-])(?:src|tests|scripts|config|\.github)/[\w./*\-]+', plan.read_text(encoding="utf-8"))
    paths = set()
    for candidate in matches:
        normalized = candidate.rstrip(".,;:)")
        if "*" in normalized:
            continue
        if (repo_root / normalized).exists():
            paths.add(normalized)
    return [{"path": path, "role": "plan-reference", "sha256": sha256_path(repo_root / path)} for path in sorted(paths)]


def build_applicability(metadata):
    """Build the fixed nine-dimension applicability matrix for every Todo."""
    tasks = {}
    for number in range(1, 33):
        dimensions = {}
        for dimension in DIMENSIONS:
            dimensions[dimension] = {
                "status": "required",
                "evidence_file": {
                    "scope": "evidence_root",
                    "path": "task-1.json",
                    "pointer": f"/dimension_evidence/{number}~1{dimension}",
                },
            }
        if number in (1, 3, 4, 6, 7, 8, 9, 10, 12, 13, 14, 15, 16, 17, 19, 20, 21, 22, 23, 24, 26, 27, 29, 30, 32):
            dimensions["legal"] = {"status": "not_applicable", "reason": "No legal approval artifact is created by this Todo."}
        tasks[str(number)] = {"dimensions": dimensions}
    return metadata | {"tasks": tasks}


def evidence_metadata(plan, baseline_commit, evidence_files):
    """Create the common evidence envelope."""
    return {
        "schema_version": 1,
        "plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
        "baseline_commit": baseline_commit,
        "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "generator_version": GENERATOR_VERSION,
        "verdict": "pass",
        "evidence_files": evidence_files,
    }


def is_reparse_or_symlink(path):
    """Reject Windows reparse points and symlinks without following them."""
    for candidate in (path, *path.parents):
        if candidate.exists():
            attributes = os.lstat(candidate).st_file_attributes if os.name == "nt" else 0
            if candidate.is_symlink() or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                return True
    return False
