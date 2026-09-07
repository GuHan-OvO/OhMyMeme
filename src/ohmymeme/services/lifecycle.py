"""Application service for deterministic desktop resource ownership."""

from _thread import LockType
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock

from ohmymeme.core.domain import TaskState

from .ports import ClosePort, HotkeyPort, SavePort, StopPort, TrayPort


@dataclass(frozen=True, slots=True)
class LifecycleResources:
    """The resources that one desktop process owns and releases."""

    hotkey: HotkeyPort | None
    tray: TrayPort | None
    lan_stop: Callable[[], None] | None
    window: StopPort | None
    database: ClosePort | None
    config: SavePort | None


class LifecycleService:
    """Close an owned resource set exactly once and expose its state."""

    def __init__(self, resources: LifecycleResources) -> None:
        self._resources: LifecycleResources = resources
        self._state: TaskState = TaskState.PENDING
        self._closed: bool = False
        self._lock: LockType = Lock()

    @property
    def state(self) -> TaskState:
        return self._state

    def mark_running(self) -> None:
        """Mark the fully composed application as running."""
        with self._lock:
            if self._state is TaskState.PENDING:
                self._state = TaskState.RUNNING

    def mark_failed(self) -> None:
        """Preserve startup failure state before resource cleanup begins."""
        with self._lock:
            if self._closed:
                return
            match self._state:
                case TaskState.PENDING | TaskState.RUNNING:
                    self._state = TaskState.FAILED
                case TaskState.COMPLETED | TaskState.FAILED | TaskState.CANCELLED:
                    return

    def close(self) -> None:
        """Release every owned resource once and re-raise accumulated failures."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._state is TaskState.PENDING:
                self._state = TaskState.CANCELLED

        actions: list[Callable[[], None]] = []
        if self._resources.hotkey is not None:
            actions.append(self._resources.hotkey.unregister)
        if self._resources.tray is not None:
            actions.append(self._resources.tray.stop)
        if self._resources.lan_stop is not None:
            actions.append(self._resources.lan_stop)
        if self._resources.window is not None:
            actions.append(self._resources.window.stop)
        if self._resources.database is not None:
            actions.append(self._resources.database.close)
        if self._resources.config is not None:
            actions.append(self._resources.config.save)

        failures: list[Exception] = []
        for action in actions:
            try:
                action()
            except Exception as error:
                failures.append(error)

        with self._lock:
            if self._state is TaskState.RUNNING:
                self._state = TaskState.COMPLETED

        if failures:
            raise ExceptionGroup("desktop resource shutdown failed", failures)
