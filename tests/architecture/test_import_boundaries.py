"""Todo 8 application boundary contracts."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "src" / "ohmymeme"
PORTS = PACKAGE / "app" / "ports.py"
LIFECYCLE = PACKAGE / "services" / "lifecycle.py"
SERVICE_PORTS = PACKAGE / "services" / "ports.py"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    return {module for module in modules if module}


def test_lifecycle_services_depend_only_on_inner_domain_and_ports() -> None:
    # Given: the new lifecycle service boundary
    # When: its imports are inspected
    # Then: it cannot reverse-depend on app, adapters, or presentation
    forbidden = (
        "ohmymeme.app",
        "ohmymeme.integrations",
        "ohmymeme.presentation",
    )
    offenders = [
        (path.relative_to(ROOT), module)
        for path in (LIFECYCLE, SERVICE_PORTS)
        for module in _imports(path)
        if module.startswith(forbidden)
    ]

    assert not offenders


def test_application_ports_do_not_leak_legacy_config_or_adapters() -> None:
    # Given: the bootstrap-facing port definitions
    # When: their imports and syntax are inspected
    # Then: concrete persistence, platform, and presentation dependencies stay out
    forbidden = (
        "ohmymeme.core.config",
        "ohmymeme.core.database",
        "ohmymeme.integrations",
        "ohmymeme.presentation",
    )
    modules = _imports(PORTS)
    tree = ast.parse(PORTS.read_text(encoding="utf-8"), filename=str(PORTS))

    assert not [module for module in modules if module.startswith(forbidden)]
    assert "Protocol" in {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Dict)]


def test_lifecycle_service_uses_the_todo_seven_task_state_value() -> None:
    # Given: the lifecycle application service
    # When: its dependency boundary is inspected
    # Then: lifecycle state is represented by the pure Todo 7 domain value
    assert "ohmymeme.core.domain" in _imports(LIFECYCLE)
