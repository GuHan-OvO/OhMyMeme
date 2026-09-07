import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DOMAIN = ROOT / "src" / "ohmymeme" / "core" / "domain"
FORBIDDEN_PREFIXES = (
    "PIL",
    "boto3",
    "ohmymeme.app",
    "ohmymeme.core.config",
    "ohmymeme.core.database",
    "ohmymeme.integrations",
    "ohmymeme.presentation",
    "ohmymeme.services",
)


def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    imported.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    return {module for module in imported if module}


def test_domain_modules_do_not_depend_on_outer_layers():
    # Given: the isolated domain package
    # When: its source imports are inspected
    # Then: UI, persistence, image, sync, and global configuration stay outside
    assert DOMAIN.is_dir()
    offenders = [
        (path.relative_to(ROOT), module)
        for path in DOMAIN.rglob("*.py")
        for module in _imports(path)
        if module.startswith(FORBIDDEN_PREFIXES)
    ]

    assert not offenders
