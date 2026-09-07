from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from .sync import TaskState
    from .values import CollectionId


class DomainError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class InvalidDomainValue(DomainError):
    field: str
    reason: str

    code: ClassVar[str] = "invalid_domain_value"

    def __str__(self) -> str:
        return f"{self.code}:{self.field}:{self.reason}"


@dataclass(frozen=True, slots=True)
class DuplicateValueError(DomainError):
    field: str
    value: str

    code: ClassVar[str] = "duplicate_value"

    def __str__(self) -> str:
        return f"{self.code}:{self.field}:{self.value}"


@dataclass(frozen=True, slots=True)
class DuplicateCollectionError(DomainError):
    collection_id: CollectionId

    code: ClassVar[str] = "duplicate_collection"

    def __str__(self) -> str:
        return f"{self.code}:{self.collection_id.value}"


@dataclass(frozen=True, slots=True)
class UnknownCollectionParentError(DomainError):
    collection_id: CollectionId
    parent_id: CollectionId

    code: ClassVar[str] = "unknown_collection_parent"

    def __str__(self) -> str:
        return f"{self.code}:{self.collection_id.value}:{self.parent_id.value}"


@dataclass(frozen=True, slots=True)
class CollectionCycleError(DomainError):
    path: tuple[CollectionId, ...]

    code: ClassVar[str] = "collection_cycle"

    def __str__(self) -> str:
        values = ">".join(str(identifier.value) for identifier in self.path)
        return f"{self.code}:{values}"


@dataclass(frozen=True, slots=True)
class UnsupportedTaskStateError(DomainError):
    state: str

    code: ClassVar[str] = "unsupported_task_state"

    def __str__(self) -> str:
        return f"{self.code}:{self.state}"


@dataclass(frozen=True, slots=True)
class InvalidTaskTransitionError(DomainError):
    current: TaskState
    target: TaskState

    code: ClassVar[str] = "invalid_task_transition"

    def __str__(self) -> str:
        return f"{self.code}:{self.current.value}:{self.target.value}"


@dataclass(frozen=True, slots=True)
class UnsupportedSyncPlanError(DomainError):
    operation: str
    reason: str = "unknown_operation"

    code: ClassVar[str] = "unsupported_sync_plan"

    def __str__(self) -> str:
        return f"{self.code}:{self.operation}:{self.reason}"
