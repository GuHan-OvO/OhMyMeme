from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import assert_never

from .errors import (
    InvalidTaskTransitionError,
    UnsupportedSyncPlanError,
    UnsupportedTaskStateError,
)
from .values import MemeFilename, Sha256, TaskId


class TaskKind(StrEnum):
    SYNC_PUSH = "sync.push"
    SYNC_PULL = "sync.pull"
    SYNC_CLEANUP = "sync.cleanup"
    SYNC_DELETE_ALL = "sync.delete_all"
    SYNC_UPLOAD_INDEX = "sync.upload_index"
    UPDATE_CHECK = "update.check"
    UPDATE_DOWNLOAD = "update.download"
    IMPORT_QQ = "import.qq"
    IMPORT_QQNT = "import.qqnt"
    IMPORT_TELEGRAM = "import.telegram"
    IMPORT_DOUYIN = "import.douyin"
    IMPORT_WECHAT = "import.wechat"
    CACHE_RESCAN = "cache.rescan"
    LAN_SERVICE = "lan.service"


class TaskState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @classmethod
    def parse(cls, value: str) -> TaskState:
        match value:
            case "pending":
                return cls.PENDING
            case "running":
                return cls.RUNNING
            case "completed":
                return cls.COMPLETED
            case "failed":
                return cls.FAILED
            case "cancelled":
                return cls.CANCELLED
            case unsupported:
                raise UnsupportedTaskStateError(unsupported)

    @property
    def next_states(self) -> tuple[TaskState, ...]:
        match self:
            case TaskState.PENDING:
                return (TaskState.RUNNING, TaskState.CANCELLED)
            case TaskState.RUNNING:
                return (
                    TaskState.COMPLETED,
                    TaskState.FAILED,
                    TaskState.CANCELLED,
                )
            case TaskState.COMPLETED | TaskState.FAILED | TaskState.CANCELLED:
                return ()
            case unreachable:
                assert_never(unreachable)


@dataclass(frozen=True, slots=True)
class Task:
    id: TaskId
    kind: TaskKind
    state: TaskState = TaskState.PENDING

    def transition(self, target: TaskState) -> Task:
        if target not in self.state.next_states:
            raise InvalidTaskTransitionError(self.state, target)
        return replace(self, state=target)


class SyncOperation(StrEnum):
    PUSH = "push"
    PULL = "pull"
    CLEANUP = "cleanup"
    DELETE_ALL = "delete_all"
    UPLOAD_INDEX = "upload_index"

    @classmethod
    def parse(cls, value: str) -> SyncOperation:
        match value:
            case "push":
                return cls.PUSH
            case "pull":
                return cls.PULL
            case "cleanup":
                return cls.CLEANUP
            case "delete_all":
                return cls.DELETE_ALL
            case "upload_index":
                return cls.UPLOAD_INDEX
            case unsupported:
                raise UnsupportedSyncPlanError(unsupported)


@dataclass(frozen=True, slots=True)
class SyncEntry:
    filename: MemeFilename
    content_hash: Sha256


@dataclass(frozen=True, slots=True)
class SyncPlan:
    operation: SyncOperation
    entries: tuple[SyncEntry, ...] = ()

    def __post_init__(self) -> None:
        if type(self.operation) is not SyncOperation:
            raise UnsupportedSyncPlanError(
                str(self.operation), "must_be_sync_operation"
            )
        match self.operation:
            case SyncOperation.PUSH | SyncOperation.PULL | SyncOperation.CLEANUP:
                if not self.entries:
                    raise UnsupportedSyncPlanError(
                        self.operation.value, "entries_required"
                    )
            case SyncOperation.DELETE_ALL | SyncOperation.UPLOAD_INDEX:
                if self.entries:
                    raise UnsupportedSyncPlanError(
                        self.operation.value, "entries_not_allowed"
                    )
            case unreachable:
                assert_never(unreachable)
        filenames = tuple(entry.filename for entry in self.entries)
        if len(set(filenames)) != len(filenames):
            raise UnsupportedSyncPlanError(self.operation.value, "duplicate_filename")

    @classmethod
    def from_operation(
        cls, operation: str, entries: tuple[SyncEntry, ...] | list[SyncEntry]
    ) -> SyncPlan:
        return cls(SyncOperation.parse(operation), tuple(entries))
