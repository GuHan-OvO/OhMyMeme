"""The only adapter allowed to touch the pywebview runtime object."""

from collections.abc import Sequence
from typing import Literal, Protocol


class Window(Protocol):
    """Minimum pywebview window surface used by the desktop boundary."""

    x: int
    y: int

    def evaluate_js(self, script: str) -> None: ...

    def move(self, x: int, y: int) -> None: ...

    def create_file_dialog(
        self,
        dialog_kind: str,
        directory: str | None = None,
        allow_multiple: bool = False,
        save_filename: str | None = None,
        file_types: tuple[str, ...] = (),
    ) -> str | tuple[str, ...] | list[str] | None: ...


class WindowHost(Protocol):
    """Desktop owner that keeps explicit window-role handles."""

    _window: Window | None
    _settings_window: Window | None


class FileDialogValues(Protocol):
    """Dialog enum values exposed by pywebview."""

    OPEN: str
    SAVE: str
    FOLDER: str


class WebViewRuntime(Protocol):
    """Minimum module surface exposed by pywebview."""

    windows: Sequence[Window]
    FileDialog: FileDialogValues


WindowRole = Literal["main", "settings"]


class PyWebViewAdapter:
    """Resolve windows by role and fail closed when the runtime is unavailable."""

    def __init__(
        self, runtime: WebViewRuntime | None, host: WindowHost | None = None
    ) -> None:
        self._runtime = runtime
        self._host = host

    def main_window(self) -> Window | None:
        """Return the explicitly owned main window, never a settings window."""
        owned = getattr(self._host, "_window", None)
        if owned is not None:
            return owned
        return self._window_at(0)

    def settings_window(self) -> Window | None:
        """Return the explicitly owned settings window, never a guessed window."""
        owned = getattr(self._host, "_settings_window", None)
        if owned is not None:
            return owned
        return self._window_at(1)

    def window(self, role: WindowRole) -> Window | None:
        """Resolve a named desktop window."""
        if role == "main":
            return self.main_window()
        return self.settings_window()

    def evaluate_main(self, script: str) -> bool:
        """Evaluate JavaScript in the main window when it exists."""
        window = self.main_window()
        if window is None:
            return False
        try:
            window.evaluate_js(script)
        except (AttributeError, RuntimeError, TypeError):
            return False
        return True

    def file_dialog(
        self,
        role: WindowRole,
        kind: Literal["open", "save", "folder"],
        allow_multiple: bool = False,
        file_types: tuple[str, ...] = (),
        save_filename: str | None = None,
    ) -> str | tuple[str, ...] | list[str] | None:
        """Open a role-specific native dialog, returning None when unavailable."""
        window = self.window(role)
        runtime = self._runtime
        if window is None or runtime is None:
            return None
        dialog_kind = getattr(runtime.FileDialog, kind.upper())
        try:
            return window.create_file_dialog(
                dialog_kind,
                None,
                allow_multiple,
                save_filename,
                file_types,
            )
        except (AttributeError, RuntimeError, TypeError):
            return None

    def move(self, role: WindowRole, dx: int, dy: int) -> bool:
        """Move a named window by a delta, returning false when not ready."""
        window = self.window(role)
        if window is None:
            return False
        try:
            window.move(window.x + dx, window.y + dy)
        except (AttributeError, RuntimeError, TypeError):
            return False
        return True

    def _window_at(self, index: int) -> Window | None:
        runtime = self._runtime
        if runtime is None:
            return None
        windows = runtime.windows
        if index >= len(windows):
            return None
        return windows[index]
