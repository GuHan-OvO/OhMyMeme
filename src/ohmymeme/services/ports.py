"""Narrow lifecycle capabilities supplied by outer adapters."""

from collections.abc import Callable
from typing import Protocol


class StopPort(Protocol):
    def stop(self) -> None: ...


class ClosePort(Protocol):
    def close(self) -> None: ...


class SavePort(Protocol):
    def save(self) -> None: ...


class HotkeyPort(Protocol):
    def register(self, hotkey: str, callback: Callable[[], None]) -> None: ...

    def unregister(self) -> None: ...


class TrayPort(StopPort, Protocol):
    def start(self) -> None: ...


class WindowPort(StopPort, Protocol):
    def set_on_hotkey_change(self, callback: Callable[[str], None]) -> None: ...

    def start(self) -> bool: ...

    def toggle_hotkey_safe(self) -> None: ...

    def toggle_safe(self) -> None: ...
