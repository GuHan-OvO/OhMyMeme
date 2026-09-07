import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def pytest_addoption(parser):
    parser.addoption("--inventory", action="store", default="")


def pytest_sessionstart(session):
    inventory = session.config.getoption("--inventory")
    if not inventory:
        return
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "resolve_plan_refs.py"),
            "--repo-root",
            str(ROOT),
            "--plan",
            str(ROOT / ".omo" / "plans" / "full-project-modular-refactor.md"),
            "--inventory",
            inventory,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        pytest.exit(result.stderr.strip(), returncode=result.returncode)
