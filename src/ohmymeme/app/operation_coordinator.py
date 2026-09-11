"""Container-owned coordination for bounded background operations."""

# allow: SIZE_OK - one lock-protected operation state machine

from __future__ import annotations

from collections.abc import Callable
from threading import Lock, Thread, current_thread
from time import monotonic
from types import MappingProxyType
from typing import Final, final

from ohmymeme.core.domain import TaskId, TaskKind, TaskState

from .operation_runtime import (
    OperationContext,
    OperationRecord,
    apply_progress,
    is_active,
    lease_count,
)
from .operation_shutdown import (
    ProcessBatch,
    finish_process_drain,
    register_process,
    resource_inventory,
    shutdown_report,
    take_cleanup_resources,
    take_processes,
    terminate_children,
)
from .operations import (
    ChildProcessPort,
    OperationBusyError,
    OperationClosedError,
    OperationContextPort,
    OperationNotFoundError,
    OperationSnapshot,
    ResourceKind,
    ShutdownReport,
    SocketPort,
    parse_task_kind,
)
from .operations import UnsupportedTaskKindError as UnsupportedTaskKindError

SHUTDOWN_DEADLINE_SECONDS: Final = 15.0
CHILD_TERMINATE_GRACE_SECONDS: Final = 2.0
_MUTEX_GROUPS: Final = MappingProxyType(
    {
        TaskKind.SYNC_PUSH: "sync",
        TaskKind.SYNC_PULL: "sync",
        TaskKind.SYNC_CLEANUP: "sync",
        TaskKind.SYNC_DELETE_ALL: "sync",
        TaskKind.SYNC_UPLOAD_INDEX: "sync",
        TaskKind.UPDATE_CHECK: "update",
        TaskKind.UPDATE_DOWNLOAD: "update",
        TaskKind.IMPORT_QQ: "library",
        TaskKind.IMPORT_QQNT: "library",
        TaskKind.IMPORT_TELEGRAM: "library",
        TaskKind.IMPORT_DOUYIN: "library",
        TaskKind.IMPORT_WECHAT: "library",
        TaskKind.CACHE_RESCAN: "library",
        TaskKind.LAN_SERVICE: "lan",
    }
)


@final
class OperationCoordinator:
    """Own one in-process task set and close it without unsafe thread termination."""

    def __init__(self, on_leases_released: Callable[[], None] | None = None) -> None:
        self._lock = Lock()
        self._records: dict[TaskKind, OperationRecord] = {}
        self._sequence = 0
        self._accepting = True
        self._shutdown_requested = False
        self._shutdown_deadline: float | None = None
        self._child_grace = CHILD_TERMINATE_GRACE_SECONDS
        self._on_leases_released = on_leases_released

    def start(
        self, task_kind: TaskKind | str, worker: Callable[[OperationContextPort], None]
    ) -> OperationSnapshot:
        kind = parse_task_kind(task_kind)
        with self._lock:
            if not self._accepting:
                raise OperationClosedError()
            current = self._records.get(kind)
            if current is not None and is_active(current):
                return self._snapshot(current)
            group = _MUTEX_GROUPS[kind]
            for record in self._records.values():
                if _MUTEX_GROUPS[record.kind] == group and is_active(record):
                    raise OperationBusyError(group)
            self._sequence += 1
            record = OperationRecord(TaskId(f"{kind.value}:{self._sequence}"), kind)
            record.state = TaskState.RUNNING
            record.thread = Thread(
                target=self._run, args=(record, worker), daemon=False
            )
            self._records[kind] = record
            record.thread.start()
            return self._snapshot(record)

    def query(self, task_kind: TaskKind | str) -> OperationSnapshot | None:
        kind = parse_task_kind(task_kind)
        with self._lock:
            record = self._records.get(kind)
            return self._snapshot(record) if record is not None else None

    def cancel(self, task_kind: TaskKind | str) -> OperationSnapshot | None:
        kind = parse_task_kind(task_kind)
        with self._lock:
            record = self._records.get(kind)
            if record is None:
                return None
            record.cancel.set()
            return self._snapshot(record)

    def shutdown_deadline(self) -> float | None:
        with self._lock:
            return self._shutdown_deadline

    def wait(self, task_kind: TaskKind | str, timeout: float) -> OperationSnapshot:
        kind = parse_task_kind(task_kind)
        with self._lock:
            record = self._records.get(kind)
            if record is None:
                raise OperationNotFoundError(kind)
            thread = record.thread
        if thread is not None and thread is not current_thread():
            thread.join(timeout)
        with self._lock:
            return self._snapshot(record)

    def shutdown(
        self,
        timeout: float = SHUTDOWN_DEADLINE_SECONDS,
        child_grace: float = CHILD_TERMINATE_GRACE_SECONDS,
    ) -> ShutdownReport:
        deadline = monotonic() + max(timeout, 0.0)
        with self._lock:
            self._accepting = False
            self._shutdown_requested = True
            self._shutdown_deadline = deadline
            self._child_grace = child_grace
            records = tuple(self._records.values())
            for record in records:
                if is_active(record):
                    record.cancel.set()
            batches = take_processes(records)
        forced = terminate_children(batches, child_grace, deadline)
        with self._lock:
            for record, processes in batches:
                finish_process_drain(record, processes, record.task_id in forced)
        for record in records:
            thread = record.thread
            if (
                thread is not None
                and thread is not current_thread()
                and thread.is_alive()
            ):
                thread.join(max(0.0, deadline - monotonic()))
        with self._lock:
            timed_out = tuple(
                record.task_id
                for record in records
                if record.thread is not None
                and record.thread is not current_thread()
                and record.thread.is_alive()
            )
            return shutdown_report(
                records,
                timed_out,
                forced,
                resource_inventory(records, is_active),
            )

    def _run(
        self,
        record: OperationRecord,
        worker: Callable[[OperationContextPort], None],
    ) -> None:
        try:
            worker(OperationContext(self, record))
        except BaseException as error:
            with self._lock:
                if record.state is TaskState.RUNNING:
                    record.state = TaskState.FAILED
                    record.error = f"{type(error).__name__}: {error}"
            raise
        else:
            with self._lock:
                if record.state is TaskState.RUNNING:
                    record.state = (
                        TaskState.CANCELLED
                        if record.cancel.is_set()
                        else TaskState.COMPLETED
                    )
        finally:
            self._cleanup(record)

    def update_progress(self, record: OperationRecord, progress: float) -> None:
        with self._lock:
            apply_progress(record, progress)

    def acquire_lease(self, record: OperationRecord, resource: ResourceKind) -> None:
        with self._lock:
            match resource:
                case ResourceKind.DATABASE:
                    record.database_leases += 1
                case ResourceKind.CONFIG:
                    record.config_leases += 1
                case _:
                    return

    def release_lease(self, record: OperationRecord, resource: ResourceKind) -> None:
        callback: Callable[[], None] | None = None
        with self._lock:
            match resource:
                case ResourceKind.DATABASE:
                    record.database_leases -= 1
                case ResourceKind.CONFIG:
                    record.config_leases -= 1
                case _:
                    return
            if self._shutdown_requested and lease_count(self._records.values()) == 0:
                callback = self._on_leases_released
        if callback is not None:
            callback()

    def register_process(
        self, record: OperationRecord, process: ChildProcessPort
    ) -> None:
        with self._lock:
            batches = register_process(record, process, self._shutdown_requested)
            deadline = self._shutdown_deadline
            child_grace = self._child_grace
        if batches:
            self._drain_processes(batches, child_grace, deadline or monotonic())

    def register_socket(self, record: OperationRecord, socket: SocketPort) -> None:
        with self._lock:
            record.sockets.append(socket)

    def register_temp(
        self, record: OperationRecord, cleanup: Callable[[], None]
    ) -> None:
        with self._lock:
            record.temporary.append(cleanup)

    def _cleanup(self, record: OperationRecord) -> None:
        with self._lock:
            batches, sockets, temporary = take_cleanup_resources(record)
        self._drain_processes(
            batches,
            CHILD_TERMINATE_GRACE_SECONDS,
            monotonic() + CHILD_TERMINATE_GRACE_SECONDS,
        )

        for socket in sockets:
            socket.close()
        for cleanup in temporary:
            cleanup()

    def _drain_processes(
        self, batches: tuple[ProcessBatch, ...], child_grace: float, deadline: float
    ) -> None:
        forced = terminate_children(batches, child_grace, deadline)
        with self._lock:
            for process_record, processes in batches:
                finish_process_drain(
                    process_record, processes, process_record.task_id in forced
                )

    def _snapshot(self, record: OperationRecord) -> OperationSnapshot:
        return OperationSnapshot(
            record.task_id,
            record.kind,
            record.state,
            record.progress,
            record.error,
            record.cancel.is_set(),
            resource_inventory((record,), is_active),
        )
