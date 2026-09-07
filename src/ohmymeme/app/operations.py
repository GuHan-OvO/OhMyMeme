"""Shared task snapshots, resources, and worker context contracts."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, override

from ohmymeme.core.domain import TaskId, TaskKind, TaskState


class ResourceKind(StrEnum):
    THREAD = "thread"
    PROCESS = "process"
    SOCKET = "socket"
    TEMPORARY = "temporary"
    DATABASE = "database"
    CONFIG = "config"


@dataclass(frozen=True, slots=True)
class ResourceInventory:
    threads: tuple[TaskId, ...]
    processes: tuple[TaskId, ...]
    sockets: tuple[TaskId, ...]
    temporary: tuple[TaskId, ...]
    database_leases: int
    config_leases: int

    def lease_count(self, resource: ResourceKind) -> int:
        match resource:
            case ResourceKind.DATABASE:
                return self.database_leases
            case ResourceKind.CONFIG:
                return self.config_leases
            case (
                ResourceKind.THREAD
                | ResourceKind.PROCESS
                | ResourceKind.SOCKET
                | ResourceKind.TEMPORARY
            ):
                return 0


@dataclass(frozen=True, slots=True)
class OperationSnapshot:
    task_id: TaskId
    kind: TaskKind
    state: TaskState
    progress: float
    error: str | None
    cancel_requested: bool
    inventory: ResourceInventory


@dataclass(frozen=True, slots=True)
class ShutdownReport:
    completed: tuple[TaskId, ...]
    cancelled: tuple[TaskId, ...]
    failed: tuple[TaskId, ...]
    timed_out: tuple[TaskId, ...]
    forced_child_terminated: tuple[TaskId, ...]
    shutdown_blocked: bool
    inventory: ResourceInventory


@dataclass(frozen=True, slots=True)
class UnsupportedTaskKindError(Exception):
    task_kind: str

    @override
    def __str__(self) -> str:
        return f"unsupported_task_kind:{self.task_kind}"


@dataclass(frozen=True, slots=True)
class OperationBusyError(Exception):
    group: str

    @override
    def __str__(self) -> str:
        return f"operation_busy:{self.group}"


class OperationClosedError(Exception):
    @override
    def __str__(self) -> str:
        return "operation_coordinator_closed"


@dataclass(frozen=True, slots=True)
class InvalidOperationProgressError(Exception):
    progress: float

    @override
    def __str__(self) -> str:
        return f"invalid_operation_progress:{self.progress}"


@dataclass(frozen=True, slots=True)
class OperationNotFoundError(Exception):
    task_kind: TaskKind

    @override
    def __str__(self) -> str:
        return f"operation_not_found:{self.task_kind.value}"


def parse_task_kind(task_kind: TaskKind | str) -> TaskKind:
    if isinstance(task_kind, TaskKind):
        return task_kind
    try:
        return TaskKind(task_kind)
    except ValueError:
        raise UnsupportedTaskKindError(task_kind) from None


class ChildProcessPort(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float) -> int: ...


class SocketPort(Protocol):
    def close(self) -> None: ...


class OperationContextPort(Protocol):
    @property
    def cancel_requested(self) -> bool: ...

    def update_progress(self, progress: float) -> None: ...

    def lease(self, resource: ResourceKind) -> AbstractContextManager[None]: ...

    def register_process(self, process: ChildProcessPort) -> None: ...

    def register_socket(self, socket: SocketPort) -> None: ...

    def register_temp(self, cleanup: Callable[[], None]) -> None: ...
