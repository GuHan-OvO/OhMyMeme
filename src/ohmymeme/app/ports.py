"""Application-facing ports for the desktop composition root."""

from collections.abc import Callable
from typing import Protocol

from ohmymeme.services.ports import (
    ClosePort,
    HotkeyPort,
    SavePort,
    TrayPort,
    WindowPort,
)


class DesktopFactoryPort(Protocol):
    """Create the platform objects needed by one desktop application."""

    def create_webui(self, update_debug: bool, silent_start: bool) -> WindowPort: ...

    def create_hotkey(self) -> HotkeyPort: ...

    def create_tray(
        self,
        on_show: Callable[[], None],
        on_quit: Callable[[], None],
        source_mode: bool,
    ) -> TrayPort: ...


class LifecycleOwnerPort(Protocol):
    """Expose only the durable resources a bootstrap failure must release."""

    db: ClosePort
    config: SavePort
