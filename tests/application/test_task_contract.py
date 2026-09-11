import subprocess
import threading

import pytest

from ohmymeme.app.container import Container
from ohmymeme.core.domain import TaskKind, TaskState


class _Child:
    def __init__(self):
        self.terminated = 0
        self.killed = 0

    def terminate(self):
        self.terminated += 1

    def poll(self):
        return 0 if self.killed else None

    def kill(self):
        self.killed += 1

    def wait(self, timeout):
        if self.killed:
            return 0
        raise subprocess.TimeoutExpired("child", timeout)


class _Socket:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


def test_task_kinds_preserve_legacy_contract_and_add_lan_service():
    legacy = {
        "sync.push",
        "sync.pull",
        "sync.cleanup",
        "sync.delete_all",
        "sync.upload_index",
        "update.check",
        "update.download",
        "import.qq",
        "import.qqnt",
        "import.telegram",
        "import.douyin",
        "import.wechat",
        "cache.rescan",
    }
    assert legacy <= {kind.value for kind in TaskKind}
    assert TaskKind.LAN_SERVICE.value == "lan.service"


def test_start_is_idempotent_and_mutex_groups_reject_conflicting_work():
    from ohmymeme.app.operation_coordinator import OperationBusyError, OperationCoordinator

    coordinator = OperationCoordinator()
    started = threading.Event()
    release = threading.Event()

    def worker(context):
        started.set()
        release.wait()
        if context.cancel_requested:
            return

    first = coordinator.start(TaskKind.SYNC_PUSH, worker)
    assert started.wait(timeout=1)

    duplicate = coordinator.start(TaskKind.SYNC_PUSH, worker)

    assert duplicate.task_id == first.task_id
    assert coordinator.query(TaskKind.SYNC_PUSH) == coordinator.query(TaskKind.SYNC_PUSH)
    with pytest.raises(OperationBusyError, match="operation_busy:sync"):
        coordinator.start(TaskKind.SYNC_PULL, worker)

    assert coordinator.cancel(TaskKind.SYNC_PUSH) == coordinator.cancel(TaskKind.SYNC_PUSH)
    release.set()
    assert coordinator.wait(TaskKind.SYNC_PUSH, timeout=1).state is TaskState.CANCELLED


def test_start_rejects_unknown_task_names_deterministically():
    from ohmymeme.app.operation_coordinator import (
        OperationCoordinator,
        UnsupportedTaskKindError,
    )

    coordinator = OperationCoordinator()

    with pytest.raises(UnsupportedTaskKindError, match="unsupported_task_kind:unknown"):
        coordinator.start("unknown", lambda _context: None)


def test_worker_progress_is_monotonic_and_worker_error_is_a_stable_terminal_snapshot():
    from ohmymeme.app.operation_coordinator import OperationCoordinator

    coordinator = OperationCoordinator()

    def completes(context):
        context.update_progress(0.75)
        context.update_progress(0.25)

    coordinator.start(TaskKind.UPDATE_CHECK, completes)
    completed = coordinator.wait(TaskKind.UPDATE_CHECK, timeout=1)

    assert completed.state is TaskState.COMPLETED
    assert completed.progress == 0.75

    def fails(_context):
        raise RuntimeError("worker failure")

    failures = []
    previous_hook = threading.excepthook
    threading.excepthook = lambda arguments: failures.append(arguments.exc_value)
    try:
        coordinator.start(TaskKind.UPDATE_DOWNLOAD, fails)
        failed = coordinator.wait(TaskKind.UPDATE_DOWNLOAD, timeout=1)
    finally:
        threading.excepthook = previous_hook

    assert failed.state is TaskState.FAILED
    assert failed.error == "RuntimeError: worker failure"
    assert len(failures) == 1


def test_shutdown_reports_noncooperative_threads_and_forcibly_reaps_children():
    from ohmymeme.app.operation_coordinator import OperationCoordinator

    coordinator = OperationCoordinator()
    child = _Child()
    started = threading.Event()
    release = threading.Event()

    def worker(context):
        context.register_process(child)
        started.set()
        release.wait()

    task = coordinator.start(TaskKind.IMPORT_TELEGRAM, worker)
    assert started.wait(timeout=1)

    report = coordinator.shutdown(timeout=0.01, child_grace=0.01)

    assert task.task_id in report.timed_out
    assert task.task_id in report.forced_child_terminated
    assert child.terminated == 1
    assert child.killed == 1

    release.set()
    coordinator.wait(TaskKind.IMPORT_TELEGRAM, timeout=1)


def test_container_shutdown_blocks_database_and_config_close_until_leases_release(tmp_path):
    from ohmymeme.app.operation_coordinator import ResourceKind

    container = Container(tmp_path)
    started = threading.Event()
    release = threading.Event()
    closed = []
    closed_event = threading.Event()

    class Window:
        def __init__(self):
            self.hidden = 0
            self.stopped = 0

        def hide(self):
            self.hidden += 1

        def stop(self):
            self.stopped += 1

    class Tray:
        def __init__(self):
            self.stopped = 0

        def stop(self):
            self.stopped += 1

    window = Window()
    tray = Tray()
    original_close = container.db.close
    original_save = container.config.save

    def close_database():
        closed.append("database")
        original_close()

    def save_config():
        closed.append("config")
        original_save()
        closed_event.set()

    container.db.close = close_database
    object.__setattr__(container.config, "save", save_config)
    shutdown = container.operations.shutdown
    container.operations.shutdown = lambda: shutdown(timeout=0.01, child_grace=0.01)

    def worker(context):
        with context.lease(ResourceKind.DATABASE), context.lease(ResourceKind.CONFIG):
            started.set()
            release.wait()

    container.operations.start(TaskKind.CACHE_RESCAN, worker)
    assert started.wait(timeout=1)

    report = container.close(tray=tray, webui=window)

    assert report.shutdown_blocked
    assert closed == []
    assert window.hidden == 1
    assert tray.stopped == 0
    assert report.inventory.lease_count(ResourceKind.DATABASE) == 1
    assert report.inventory.lease_count(ResourceKind.CONFIG) == 1

    release.set()
    container.operations.wait(TaskKind.CACHE_RESCAN, timeout=1)
    assert closed_event.wait(timeout=1)
    assert closed == ["database", "config"]


def test_resource_inventory_is_cleaned_and_close_rejects_new_work(tmp_path):
    from ohmymeme.app.operation_coordinator import OperationClosedError, ResourceKind

    container = Container(tmp_path)
    socket = _Socket()
    cleaned = []

    def worker(context):
        context.register_socket(socket)
        context.register_temp(lambda: cleaned.append("temp"))
        with context.lease(ResourceKind.DATABASE):
            pass

    container.operations.start(TaskKind.IMPORT_QQ, worker)
    container.operations.wait(TaskKind.IMPORT_QQ, timeout=1)
    report = container.close()

    assert not report.shutdown_blocked
    assert socket.closed == 1
    assert cleaned == ["temp"]
    assert report.inventory.threads == ()
    assert report.inventory.processes == ()
    assert report.inventory.sockets == ()
    assert report.inventory.temporary == ()
    assert report.inventory.database_leases == 0
    assert report.inventory.config_leases == 0
    with pytest.raises(OperationClosedError, match="operation_coordinator_closed"):
        container.operations.start(TaskKind.IMPORT_QQ, worker)
