"""Deterministic task resource inventory and child process shutdown helpers."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from time import monotonic

from ohmymeme.core.domain import TaskId, TaskState

from .operation_runtime import OperationRecord
from .operations import ChildProcessPort, ResourceInventory, ShutdownReport, SocketPort

type ProcessBatch = tuple[OperationRecord, tuple[ChildProcessPort, ...]]
type CleanupResources = tuple[
    tuple[ProcessBatch, ...], tuple[SocketPort, ...], tuple[Callable[[], None], ...]
]


def register_process(
    record: OperationRecord, process: ChildProcessPort, shutdown_requested: bool
) -> tuple[ProcessBatch, ...]:
    if not shutdown_requested:
        record.processes.append(process)
        return ()
    record.draining_processes.append(process)
    return ((record, (process,)),)


def take_processes(records: tuple[OperationRecord, ...]) -> tuple[ProcessBatch, ...]:
    batches: list[ProcessBatch] = []
    for record in records:
        processes = tuple(record.processes)
        record.processes.clear()
        record.draining_processes.extend(processes)
        if processes:
            batches.append((record, processes))
    return tuple(batches)


def finish_process_drain(
    record: OperationRecord,
    processes: tuple[ChildProcessPort, ...],
    forced: bool,
) -> None:
    for process in processes:
        record.draining_processes.remove(process)
    if forced:
        record.forced_children = True


def take_cleanup_resources(record: OperationRecord) -> CleanupResources:
    batches = take_processes((record,))
    sockets = tuple(record.sockets)
    temporary = tuple(record.temporary)
    record.sockets.clear()
    record.temporary.clear()
    return batches, sockets, temporary


def terminate_children(
    batches: tuple[ProcessBatch, ...], child_grace: float, deadline: float
) -> tuple[TaskId, ...]:
    forced: list[TaskId] = []
    for record, processes in batches:
        forced_child = False
        for process in processes:
            if process.poll() is not None:
                continue
            process.terminate()
            try:
                _ = process.wait(min(child_grace, max(0.0, deadline - monotonic())))
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    _ = process.wait(max(0.0, deadline - monotonic()))
                except subprocess.TimeoutExpired:
                    continue
                else:
                    forced_child = True
        if forced_child:
            forced.append(record.task_id)
    return tuple(forced)


def resource_inventory(
    records: tuple[OperationRecord, ...], active: Callable[[OperationRecord], bool]
) -> ResourceInventory:
    return ResourceInventory(
        tuple(record.task_id for record in records if active(record)),
        tuple(
            record.task_id
            for record in records
            if record.processes or record.draining_processes
        ),
        tuple(record.task_id for record in records if record.sockets),
        tuple(record.task_id for record in records if record.temporary),
        sum(record.database_leases for record in records),
        sum(record.config_leases for record in records),
    )


def shutdown_report(
    records: tuple[OperationRecord, ...],
    timed_out: tuple[TaskId, ...],
    forced: tuple[TaskId, ...],
    inventory: ResourceInventory,
) -> ShutdownReport:
    return ShutdownReport(
        tuple(
            record.task_id for record in records if record.state is TaskState.COMPLETED
        ),
        tuple(
            record.task_id for record in records if record.state is TaskState.CANCELLED
        ),
        tuple(record.task_id for record in records if record.state is TaskState.FAILED),
        timed_out,
        forced,
        inventory.database_leases + inventory.config_leases > 0,
        inventory,
    )
