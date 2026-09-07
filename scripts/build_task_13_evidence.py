"""从当前 presentation 源码生成 Todo 13 边界审计。"""

import ast
import hashlib
from pathlib import Path
from typing import TypedDict


class PresentationAudit(TypedDict):
    passed: bool
    private_database: int
    direct_projection: int
    source_sha256: str


class Task13SourceEvidence(TypedDict):
    source_audit: PresentationAudit


def build_task_report(source_path: Path) -> Task13SourceEvidence:
    return {"source_audit": audit_presentation_source(source_path)}


def audit_presentation_source(source_path: Path) -> PresentationAudit:
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(source_path))
    visitor = _PresentationBoundaryVisitor()
    visitor.visit(tree)
    return {
        "passed": visitor.private_database == 0 and visitor.direct_projection == 0,
        "private_database": visitor.private_database,
        "direct_projection": visitor.direct_projection,
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
    }


class _PresentationBoundaryVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.private_database = 0
        self.direct_projection = 0

    def visit_Attribute(self, node: ast.Attribute) -> None:
        path = _attribute_path(node)
        if path in {"self._db", "self._webui._db"}:
            self.private_database += 1
        if path in {
            "self._container.build_manifest",
            "self._webui._container.build_manifest",
        }:
            self.direct_projection += 1
        self.generic_visit(node)


def _attribute_path(node: ast.Attribute) -> str:
    names = [node.attr]
    value = node.value
    while isinstance(value, ast.Attribute):
        names.append(value.attr)
        value = value.value
    if isinstance(value, ast.Name):
        names.append(value.id)
    return ".".join(reversed(names))
