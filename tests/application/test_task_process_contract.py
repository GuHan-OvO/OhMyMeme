import subprocess
import sys
import threading

from ohmymeme.app.operation_coordinator import OperationCoordinator
from ohmymeme.core.domain import TaskKind, TaskState


def _sleeping_child():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def _terminate_if_running(process):
    if process.poll() is None:
        process.kill()
    process.wait(timeout=1)


def test_normal_worker_completion_reaps_registered_child_process():
    coordinator = OperationCoordinator()
    children = []

    def worker(context):
        child = _sleeping_child()
        children.append(child)
        context.register_process(child)

    try:
        coordinator.start(TaskKind.IMPORT_QQ, worker)
        completed = coordinator.wait(TaskKind.IMPORT_QQ, timeout=2)

        assert completed.state is TaskState.COMPLETED
        assert children[0].poll() is not None
        assert completed.inventory.processes == ()
    finally:
        for child in children:
            _terminate_if_running(child)


def test_shutdown_drains_child_registered_during_process_snapshot(monkeypatch):
    import ohmymeme.app.operation_coordinator as coordinator_module

    coordinator = OperationCoordinator()
    snapshot_ready = threading.Event()
    register_now = threading.Event()
    registered = threading.Event()
    release_shutdown = threading.Event()
    children = []
    reports = []
    original = coordinator_module.terminate_children

    def pause_drain(*args):
        if not snapshot_ready.is_set():
            snapshot_ready.set()
            assert release_shutdown.wait(timeout=1)
        return original(*args)

    monkeypatch.setattr(coordinator_module, "terminate_children", pause_drain)

    def worker(context):
        assert register_now.wait(timeout=1)
        child = _sleeping_child()
        children.append(child)
        context.register_process(child)
        registered.set()

    task = coordinator.start(TaskKind.IMPORT_QQNT, worker)
    shutdown_thread = threading.Thread(
        target=lambda: reports.append(coordinator.shutdown(timeout=1, child_grace=0.1))
    )

    try:
        shutdown_thread.start()
        assert snapshot_ready.wait(timeout=1)
        register_now.set()
        assert registered.wait(timeout=1)
        assert coordinator.wait(TaskKind.IMPORT_QQNT, timeout=1).state is TaskState.CANCELLED
        release_shutdown.set()
        shutdown_thread.join(timeout=2)

        assert not shutdown_thread.is_alive()
        assert children[0].poll() is not None
        assert task.task_id not in reports[0].inventory.processes
    finally:
        release_shutdown.set()
        shutdown_thread.join(timeout=1)
        for child in children:
            _terminate_if_running(child)
