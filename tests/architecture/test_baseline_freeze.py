import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "freeze_baseline.py"
RESOLVER = ROOT / "scripts" / "resolve_plan_refs.py"
PLAN = ROOT / ".omo" / "plans" / "full-project-modular-refactor.md"


def _freeze(tmp_path, repository=ROOT):
    evidence_root = tmp_path / "evidence"
    temp_root = tmp_path / "task-temp"
    environment = dict(os.environ)
    environment["OMO_EVIDENCE_ROOT"] = str(evidence_root)
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(repository),
            "--plan",
            str(PLAN),
            "--evidence-root",
            str(evidence_root),
            "--temp-root",
            str(temp_root),
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_freeze_baseline_emits_canonical_inventory_and_task_records(tmp_path):
    # Given: an external, user-owned evidence directory
    result = _freeze(tmp_path)

    # When: the baseline freeze runs against the current worktree
    assert result.returncode == 0, result.stderr

    # Then: required evidence is present, JCS canonical, and describes contracts
    evidence_root = tmp_path / "evidence"
    for filename in ("inventory.json", "task-applicability.json", "task-1.json"):
        payload = (evidence_root / filename).read_bytes()
        assert json.loads(payload)
    inventory = json.loads((evidence_root / "inventory.json").read_text(encoding="utf-8"))
    assert inventory["schema_version"] == 1
    assert inventory["verdict"] == "pass"
    assert inventory["plan_sha256"]
    assert inventory["baseline_commit"]
    assert {"copy_meme", "/api/upload/"} <= set(inventory["required_contracts"])
    assert all(
        {"legacy_path", "current_path", "source_or_host_or_generated", "owner",
         "public_or_internal", "replacement_task", "delete_authorization",
         "existence_sha256", "source_line_anchors"} <= set(entry)
        for entry in inventory["legacy_mappings"]
    )
    applicability = json.loads(
        (evidence_root / "task-applicability.json").read_text(encoding="utf-8")
    )
    assert set(applicability["tasks"]) == {str(number) for number in range(1, 33)}
    assert all(len(task["dimensions"]) == 9 for task in applicability["tasks"].values())


def test_freeze_baseline_rejects_missing_copy_meme_and_changed_upload_route(tmp_path):
    # Given: an isolated source copy with a public contract mutation
    repository = tmp_path / "repository"
    source_root = ROOT / "src"
    target_root = repository / "src"
    target_root.parent.mkdir()
    import shutil

    shutil.copytree(source_root, target_root)
    fixture_root = repository / "tests" / "fixtures"
    fixture_root.mkdir(parents=True)
    shutil.copy2(ROOT / "tests" / "fixtures" / "public_surface_contract.json", fixture_root)
    manager = target_root / "ohmymeme" / "presentation" / "desktop" / "window_manager.py"
    source = manager.read_text(encoding="utf-8")
    manager.write_text(
        source.replace("def copy_meme(", "def copy_meme_removed(", 1).replace(
            '"/api/upload/"', '"/api/upload-changed/"', 1
        ),
        encoding="utf-8",
    )

    # When: a mutated public surface is frozen
    result = _freeze(tmp_path, repository)

    # Then: the freeze fails rather than recording a misleading baseline
    assert result.returncode != 0
    assert "BLOCKED_PUBLIC_CONTRACT_DRIFT" in result.stderr


def test_freeze_baseline_rejects_a_worktree_or_existing_evidence_root(tmp_path):
    # Given: evidence locations that would either pollute the worktree or overwrite proof
    worktree_result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(ROOT),
            "--plan",
            str(PLAN),
            "--evidence-root",
            str(ROOT / ".omo" / "evidence"),
            "--temp-root",
            str(tmp_path / "task-temp"),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    existing_root = tmp_path / "existing"
    existing_root.mkdir()
    (existing_root / "inventory.json").write_text("{}", encoding="utf-8")

    # When: the generator receives unsafe evidence roots
    existing_result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(ROOT),
            "--plan",
            str(PLAN),
            "--evidence-root",
            str(existing_root),
            "--temp-root",
            str(tmp_path / "another-task-temp"),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    # Then: neither unsafe root is accepted
    assert "BLOCKED_INVALID_EVIDENCE_ROOT" in worktree_result.stderr
    assert "BLOCKED_EVIDENCE_EXISTS" in existing_result.stderr


def test_resolver_rejects_a_wrong_mapping(tmp_path):
    # Given: a generated inventory whose legacy mapping was corrupted
    result = _freeze(tmp_path)
    assert result.returncode == 0, result.stderr
    inventory_path = tmp_path / "evidence" / "inventory.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    inventory["legacy_mappings"][0]["current_path"] = ["src/missing.py"]
    inventory_path.write_text(
        json.dumps(inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    # When: the plan-reference resolver consumes the inventory
    verification = subprocess.run(
        [
            sys.executable,
            str(RESOLVER),
            "--repo-root",
            str(ROOT),
            "--plan",
            str(PLAN),
            "--inventory",
            str(inventory_path),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    # Then: a wrong mapping cannot pass as an inventory freeze
    assert verification.returncode != 0
    assert "BLOCKED_INVALID_MAPPING" in verification.stderr


def test_freeze_baseline_rejects_an_overlong_evidence_path_without_traceback(tmp_path):
    # Given: an evidence-root input that cannot be created by the host filesystem
    evidence_root = tmp_path / ("x" * 300)

    # When: the gate receives the malformed long path
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--evidence-root",
            str(evidence_root),
            "--temp-root",
            str(tmp_path / "task-temp"),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    # Then: it reports the gate error without a misleading successful exit
    assert result.returncode != 0
    assert result.stderr.strip() == "BLOCKED_INVALID_EVIDENCE_ROOT"


def test_resolver_rejects_an_existing_but_wrong_mapping_and_zero_baseline(tmp_path):
    # Given: canonical evidence with a path that exists but violates identity
    result = _freeze(tmp_path)
    assert result.returncode == 0, result.stderr
    inventory_path = tmp_path / "evidence" / "inventory.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    inventory["legacy_mappings"][0]["current_path"] = ["mise.toml"]
    inventory["baseline_commit"] = "0" * 40
    inventory_path.write_text(
        json.dumps(inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    # When: the resolver validates the forged evidence
    verification = subprocess.run(
        [sys.executable, str(RESOLVER), "--plan", str(PLAN), "--inventory", str(inventory_path)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    # Then: existence alone cannot authenticate a mapping or baseline
    assert verification.returncode != 0
    assert "BLOCKED_INVALID_EVIDENCE" in verification.stderr


def test_inventory_contains_concrete_contracts_and_dimension_evidence(tmp_path):
    # Given: a freshly frozen baseline
    result = _freeze(tmp_path)
    assert result.returncode == 0, result.stderr

    # When: evidence is read through its public schema
    evidence_root = tmp_path / "evidence"
    inventory = json.loads((evidence_root / "inventory.json").read_text(encoding="utf-8"))
    applicability = json.loads((evidence_root / "task-applicability.json").read_text(encoding="utf-8"))

    # Then: every required surface and applicability dimension has concrete proof
    assert {"cli", "bridge", "http", "config", "database", "manifest", "sync", "lan", "platform"} <= set(inventory["public_surfaces"])
    assert all(
        dimension["evidence_file"]
        for task in applicability["tasks"].values()
        for dimension in task["dimensions"].values()
        if dimension["status"] == "required"
    )


def test_resolver_rejects_forged_contract_reports_anchors_and_pointers(tmp_path):
    # Given: canonical sibling evidence with one integrity mutation at a time
    result = _freeze(tmp_path)
    assert result.returncode == 0, result.stderr
    root = tmp_path / "evidence"
    original = {name: (root / name).read_text(encoding="utf-8") for name in ("inventory.json", "task-applicability.json", "task-1.json")}
    mutations = (
        ("inventory.json", ("legacy_mappings", 0, "existence_sha256", 0), "0" * 64),
        ("inventory.json", ("legacy_mappings", 0, "source_line_anchors"), []),
        ("inventory.json", ("public_surfaces", "lan", "v1", 0), "forged"),
        ("task-1.json", ("title",), "forged"),
        ("task-applicability.json", ("tasks", "1", "dimensions", "inputs", "evidence_file", "pointer"), "/missing"),
    )
    for filename, path, replacement in mutations:
        payload = json.loads((root / filename).read_text(encoding="utf-8"))
        target = payload
        for segment in path[:-1]:
            target = target[segment]
        target[path[-1]] = replacement
        (root / filename).write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        verification = subprocess.run([sys.executable, str(RESOLVER), "--plan", str(PLAN), "--inventory", str(root / "inventory.json")], cwd=ROOT, text=True, capture_output=True, check=False)
        assert verification.returncode != 0
        for name, content in original.items():
            (root / name).write_text(content, encoding="utf-8")


def test_resolver_rejects_composite_clean_head_hash_and_lan_forgery(tmp_path):
    # Given: a forged inventory and task report made internally self-consistent
    result = _freeze(tmp_path)
    assert result.returncode == 0, result.stderr
    root = tmp_path / "evidence"
    inventory = json.loads((root / "inventory.json").read_text(encoding="utf-8"))
    task = json.loads((root / "task-1.json").read_text(encoding="utf-8"))
    inventory["legacy_mappings"][0]["existence_sha256"][0] = "a" * 64
    inventory["public_surfaces"]["lan"]["v1"][0] = "forged JSON encoding"
    canonical = json.dumps(inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    import hashlib

    task["inventory_sha256"] = hashlib.sha256(canonical).hexdigest()
    (root / "inventory.json").write_bytes(canonical)
    (root / "task-1.json").write_text(json.dumps(task, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")

    # When: every self-reported companion hash has been recomputed
    verification = subprocess.run([sys.executable, str(RESOLVER), "--plan", str(PLAN), "--inventory", str(root / "inventory.json")], cwd=ROOT, text=True, capture_output=True, check=False)

    # Then: authoritative clean-HEAD source facts still reject the forged evidence
    assert verification.returncode != 0
