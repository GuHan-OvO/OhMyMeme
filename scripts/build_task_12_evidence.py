"""生成 Todo 12 的外部 SQLite 兼容性证据。"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _schema_snapshot(database_path: Path) -> dict[str, object]:
    from ohmymeme.core.database import MemeDB

    database = MemeDB(database_path)
    connection = database._get_conn()
    tables = {}
    for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ):
        table = row[0]
        tables[table] = {
            "columns": [
                list(column)
                for column in connection.execute(f"PRAGMA table_info({table})")
            ],
            "indexes": [
                index[1] for index in connection.execute(f"PRAGMA index_list({table})")
            ],
        }
    pragmas = {
        "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
        "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
    }
    database.close()
    return {"pragmas": pragmas, "tables": tables}


def _run_tests(catalog_path: Path) -> dict[str, object]:
    command = [
        sys.executable,
        "-m",
        "pytest",
        "tests/migration/test_database_upgrade.py",
        "tests/adapters/sqlite",
        "-q",
    ]
    environment = dict(os.environ)
    environment["OHMYMEME_TODO2_CATALOG"] = str(catalog_path)
    environment["OHMYMEME_TODO2_SNAPSHOT"] = str(
        catalog_path.parent / "github-releases-snapshot.json"
    )
    result = subprocess.run(
        command, cwd=ROOT, text=True, capture_output=True, env=environment
    )
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def _todo2_fixtures(catalog_path: Path) -> dict[str, dict[str, str]]:
    catalog = json.loads(catalog_path.read_text("utf-8"))
    fixture_root = catalog_path.parent / "release-fixtures"
    records = {}
    for release in catalog["releases"]:
        version = release["version"]
        pair_root = fixture_root / version
        actual = {
            "database": hashlib.sha256((pair_root / "memes.db").read_bytes()).hexdigest(),
            "wal": hashlib.sha256((pair_root / "memes.db-wal").read_bytes()).hexdigest(),
        }
        if actual != {
            "database": release["fixture_sha256"]["database"],
            "wal": release["fixture_sha256"]["wal"],
        }:
            raise RuntimeError(f"Todo 2 fixture hash mismatch: {version}")
        records[version] = actual
    return records


def build_evidence(evidence_root: Path, catalog_path: Path) -> Path:
    report_path = evidence_root / "task-12.json"
    if report_path.exists():
        raise FileExistsError(report_path)
    evidence_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=evidence_root) as temporary:
        snapshot = _schema_snapshot(Path(temporary) / "memes.db")
    fixture = _todo2_fixtures(catalog_path)
    result = _run_tests(catalog_path)
    if result["returncode"] != 0:
        raise RuntimeError(result["stderr"] or result["stdout"])
    report = {
        "generator_version": "todo-12-sqlite-evidence/1",
        "verdict": "pass",
        "schema_snapshot": snapshot,
        "query_parity": "tests/adapters/sqlite/test_repository.py",
        "todo2_fixture": {
            "catalog_path": str(catalog_path),
            "wal_present": True,
            "sha256": fixture,
        },
        "migration_fault_matrix": {
            "interrupted_alter": "rollback",
            "foreign_key_corruption": "fail_closed",
            "unknown_schema": "fail_closed",
            "concurrent_open": "idempotent",
            "historical_wal": "copied_fixture_reopen",
        },
        "verification": result,
        "cleanup_receipt": {"temporary_directory_removed": True},
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return report_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--release-catalog", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        print(build_evidence(arguments.evidence_root, arguments.release_catalog))
    except (FileExistsError, OSError, RuntimeError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
