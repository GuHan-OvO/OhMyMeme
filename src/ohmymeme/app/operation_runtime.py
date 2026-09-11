"""Mutable operation record and worker-scoped coordination context."""

from __future__ import annotations

from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager
from threading import Event, Thread
from typing import Protocol, final

from ohmymeme.core.domain import TaskId, TaskKind, TaskState

from .operations import (
    ChildProcessPort,
    InvalidOperationProgressError,
    ResourceKind,
    SocketPort,
)


@final
class OperationRecord:
    """Mutable state is confined to one coordinator lock."""

    def __init__(self, task_id: TaskId, kind: TaskKind) -> None:
        self.task_id = task_id
        self.kind = kind
        self.state = TaskState.PENDING
        self.progress = 0.0
        self.error: str | None = None
        self.cancel = Event()
        self.thread: Thread | None = None
        self.processes: list[ChildProcessPort] = []
        self.draining_processes: list[ChildProcessPort] = []
        self.sockets: list[SocketPort] = []
        self.temporary: list[Callable[[], None]] = []
        self.database_leases = 0
        self.config_leases = 0
        self.forced_children = False


class OperationCoordinatorPort(Protocol):
    def shutdown_deadline(self) -> float | None: ...

    def update_progress(self, record: OperationRecord, progress: float) -> None: ...

    def acquire_lease(
        self, record: OperationRecord, resource: ResourceKind
    ) -> None: ...

    def release_lease(
        self, record: OperationRecord, resource: ResourceKind
    ) -> None: ...

    def register_process(
        self, record: OperationRecord, process: ChildProcessPort
    ) -> None: ...

    def register_socket(self, record: OperationRecord, socket: SocketPort) -> None: ...

    def register_temp(
        self, record: OperationRecord, cleanup: Callable[[], None]
    ) -> None: ...


def is_active(record: OperationRecord) -> bool:
    return record.thread is not None and record.thread.is_alive()


def apply_progress(record: OperationRecord, progress: float) -> None:
    if not 0.0 <= progress <= 1.0:
        raise InvalidOperationProgressError(progress)
    if record.state is TaskState.RUNNING:
        record.progress = max(record.progress, progress)


def lease_count(records: Iterable[OperationRecord]) -> int:
    return sum(record.database_leases + record.config_leases for record in records)


@final
class OperationContext:
    def __init__(
        self, coordinator: OperationCoordinatorPort, record: OperationRecord
    ) -> None:
        self._coordinator = coordinator
        self._record = record

    @property
    def cancel_requested(self) -> bool:
        return self._record.cancel.is_set()

    @property
    def shutdown_deadline(self) -> float | None:
        return self._coordinator.shutdown_deadline()

    def wait_cancelled(self) -> bool:
        return self._record.cancel.wait()

    def update_progress(self, progress: float) -> None:
        self._coordinator.update_progress(self._record, progress)

    @contextmanager
    def lease(self, resource: ResourceKind) -> Generator[None, None, None]:
        self._coordinator.acquire_lease(self._record, resource)
        try:
            yield
        finally:
            self._coordinator.release_lease(self._record, resource)

    def register_process(self, process: ChildProcessPort) -> None:
        self._coordinator.register_process(self._record, process)

    def register_socket(self, socket: SocketPort) -> None:
        self._coordinator.register_socket(self._record, socket)

    def register_temp(self, cleanup: Callable[[], None]) -> None:
        self._coordinator.register_temp(self._record, cleanup)
