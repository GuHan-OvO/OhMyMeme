"""Composition-root lifecycle contracts."""

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from ohmymeme.core.domain import TaskState


class _Events:
    def __init__(self) -> None:
        self.values: list[str] = []

    def add(self, value: str) -> None:
        self.values.append(value)


class _Hotkey:
    def __init__(self, events: _Events, fail: bool = False) -> None:
        self._events = events
        self._fail = fail

    def register(self, hotkey: str, callback: object) -> None:
        self._events.add(f"register:{hotkey}")

    def unregister(self) -> None:
        self._events.add("hotkey")
        if self._fail:
            raise RuntimeError("hotkey stop failed")


class _Tray:
    def __init__(self, events: _Events) -> None:
        self._events = events

    def start(self) -> None:
        self._events.add("tray-start")

    def stop(self) -> None:
        self._events.add("tray")


class _Database:
    def __init__(self, events: _Events) -> None:
        self._events = events

    def close(self) -> None:
        self._events.add("database")


class _Config:
    def __init__(self, events: _Events) -> None:
        self._events = events

    def get(self, key: str, default: str | bool) -> str | bool:
        return default

    def save(self) -> None:
        self._events.add("config")


class _Window:
    def __init__(self, events: _Events, fail: bool = False) -> None:
        self._events = events
        self._fail = fail
        self.started = threading.Event()
        self.stopped = threading.Event()

    def set_on_hotkey_change(self, callback: object) -> None:
        self._events.add("window-callback")

    def start(self) -> bool:
        self._events.add("window-start")
        self.started.set()
        if self._fail:
            raise RuntimeError("window start failed")
        self.stopped.wait(timeout=2)
        return True

    def stop(self) -> None:
        self._events.add("window")
        self.stopped.set()

    def toggle_hotkey_safe(self) -> None:
        self._events.add("hotkey-toggle")

    def toggle_safe(self) -> None:
        self._events.add("tray-toggle")


class _Container:
    def __init__(self, events: _Events, window: _Window, hotkey: _Hotkey) -> None:
        self.config = _Config(events)
        self.db = _Database(events)
        self._events = events
        self._window = window
        self._hotkey = hotkey

    def create_webui(self, update_debug: bool, silent_start: bool) -> _Window:
        self._events.add(f"create-window:{update_debug}:{silent_start}")
        return self._window

    def create_hotkey(self) -> _Hotkey:
        return self._hotkey

    def create_tray(
        self, on_show: object, on_quit: object, source_mode: bool
    ) -> _Tray:
        return _Tray(self._events)


def test_lifecycle_closes_resources_once_when_close_is_repeated() -> None:
    from ohmymeme.services.lifecycle import LifecycleResources, LifecycleService

    # Given: one fully constructed desktop resource set
    events = _Events()
    lifecycle = LifecycleService(
        LifecycleResources(
            _Hotkey(events),
            _Tray(events),
            lambda: events.add("lan"),
            _Window(events),
            _Database(events),
            _Config(events),
        )
    )

    # When: shutdown is requested twice
    lifecycle.close()
    lifecycle.close()

    # Then: every owned resource is released exactly once in compatibility order
    assert events.values == ["hotkey", "tray", "lan", "window", "database", "config"]
    assert lifecycle.state is TaskState.CANCELLED


def test_lifecycle_reports_close_failure_after_releasing_remaining_resources() -> None:
    from ohmymeme.services.lifecycle import LifecycleResources, LifecycleService

    # Given: a hotkey that cannot unregister
    events = _Events()
    lifecycle = LifecycleService(
        LifecycleResources(
            _Hotkey(events, fail=True),
            _Tray(events),
            lambda: events.add("lan"),
            _Window(events),
            _Database(events),
            _Config(events),
        )
    )

    # When: shutdown reaches the failing resource
    with pytest.raises(ExceptionGroup) as failure:
        lifecycle.close()

    # Then: it cannot report success and later resources still release
    assert len(failure.value.exceptions) == 1
    assert events.values == ["hotkey", "tray", "lan", "window", "database", "config"]


def test_bootstrap_shutdown_races_with_long_running_window_without_duplicate_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ohmymeme.app import bootstrap

    # Given: a startup window that remains active until shutdown stops it
    events = _Events()
    window = _Window(events)
    container = _Container(events, window, _Hotkey(events))
    app = bootstrap.OhMyMemeApp(container)
    monkeypatch.setattr(bootstrap, "_ensure_vue_frontend", lambda: None)
    monkeypatch.setattr(bootstrap, "cleanup_stale_temp_files", lambda: None)
    monkeypatch.setattr(bootstrap, "_adb_init", lambda: None)
    monkeypatch.setattr(bootstrap.platform, "system", lambda: "Linux")
    monkeypatch.setattr(bootstrap.lan, "stop", lambda: events.add("lan"))

    # When: shutdown overlaps the blocking GUI startup path
    runner = threading.Thread(target=app.run)
    runner.start()
    assert window.started.wait(timeout=2)
    app.shutdown()
    runner.join(timeout=2)

    # Then: the application exits and each owned resource closes once
    assert not runner.is_alive()
    assert events.values.count("hotkey") == 1
    assert events.values.count("lan") == 1
    assert events.values.count("window") == 1
    assert events.values.count("database") == 1
    assert events.values.count("config") == 1


def test_bootstrap_failure_releases_constructed_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ohmymeme.app import bootstrap

    # Given: a window whose GUI loop fails after all resources are constructed
    events = _Events()
    container = _Container(events, _Window(events, fail=True), _Hotkey(events))
    app = bootstrap.OhMyMemeApp(container)
    monkeypatch.setattr(bootstrap, "_ensure_vue_frontend", lambda: None)
    monkeypatch.setattr(bootstrap, "cleanup_stale_temp_files", lambda: None)
    monkeypatch.setattr(bootstrap, "_adb_init", lambda: None)
    monkeypatch.setattr(bootstrap.platform, "system", lambda: "Linux")
    monkeypatch.setattr(bootstrap.lan, "stop", lambda: events.add("lan"))

    # When: the GUI start action raises
    with pytest.raises(RuntimeError, match="window start failed"):
        app.run()

    # Then: the failure path closes every resource without a misleading success
    assert events.values[-5:] == ["hotkey", "lan", "window", "database", "config"]


def test_early_startup_failure_keeps_failed_state_after_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ohmymeme.app import bootstrap

    # Given: a startup dependency that fails before the lifecycle starts running
    events = _Events()
    container = _Container(events, _Window(events), _Hotkey(events))
    app = bootstrap.OhMyMemeApp(container)
    monkeypatch.setattr(
        bootstrap,
        "_ensure_vue_frontend",
        lambda: (_ for _ in ()).throw(RuntimeError("early startup failure")),
    )
    monkeypatch.setattr(bootstrap.lan, "stop", lambda: events.add("lan"))

    # When: the application starts and the dependency raises
    with pytest.raises(RuntimeError, match="early startup failure"):
        app.run()

    # Then: cleanup completes without changing the terminal failed status
    assert app._lifecycle_service().state is TaskState.FAILED
    assert events.values == ["lan", "database", "config"]


def test_lifecycle_rejects_late_failure_transition_after_terminal_close() -> None:
    from ohmymeme.services.lifecycle import LifecycleResources, LifecycleService

    # Given: a lifecycle already closed before startup could begin
    events = _Events()
    lifecycle = LifecycleService(
        LifecycleResources(
            None,
            None,
            lambda: events.add("lan"),
            None,
            _Database(events),
            _Config(events),
        )
    )
    lifecycle.close()

    # When: a late failure notification arrives
    lifecycle.mark_failed()

    # Then: the terminal cancelled result cannot become failed retroactively
    assert lifecycle.state is TaskState.CANCELLED
    assert events.values == ["lan", "database", "config"]


def test_lifecycle_rejects_late_failure_after_successful_close() -> None:
    from ohmymeme.services.lifecycle import LifecycleResources, LifecycleService

    # Given: a lifecycle that has completed a normal running shutdown
    events = _Events()
    lifecycle = LifecycleService(
        LifecycleResources(
            None,
            None,
            lambda: events.add("lan"),
            None,
            _Database(events),
            _Config(events),
        )
    )
    lifecycle.mark_running()
    lifecycle.close()

    # When: a failure is reported after the successful terminal transition
    lifecycle.mark_failed()

    # Then: completed remains terminal and cleanup remains single-shot
    assert lifecycle.state is TaskState.COMPLETED
    assert events.values == ["lan", "database", "config"]


def test_compose_app_cleans_container_when_dependency_initialization_fails() -> None:
    from ohmymeme.app import bootstrap

    # Given: a container whose required configuration cannot be read
    events = _Events()
    window = _Window(events)
    container = _Container(events, window, _Hotkey(events))

    def fail_get(key: str, default: str | bool) -> str | bool:
        raise RuntimeError("malformed startup dependency")

    container.config.get = fail_get

    # When: the composition root builds the application
    with pytest.raises(RuntimeError, match="malformed startup dependency"):
        bootstrap.compose_app(container_factory=lambda: container)

    # Then: the partially composed object graph releases its owned resources
    assert events.values == ["database", "config"]


def test_compose_app_keeps_sequential_object_graphs_isolated() -> None:
    from ohmymeme.app import bootstrap

    # Given: two separately supplied composition-root containers
    first_events = _Events()
    second_events = _Events()
    first = _Container(first_events, _Window(first_events), _Hotkey(first_events))
    second = _Container(second_events, _Window(second_events), _Hotkey(second_events))

    # When: each container forms an application and both applications close
    first_app = bootstrap.compose_app(container_factory=lambda: first)
    second_app = bootstrap.compose_app(container_factory=lambda: second)
    first_app.shutdown()
    second_app.shutdown()

    # Then: no singleton or closed resource leaks from one graph into the other
    assert first_app._container is first
    assert second_app._container is second
    assert first_events.values == ["database", "config"]
    assert second_events.values == ["database", "config"]


def test_module_entrypoint_help_remains_observable() -> None:
    # Given: the installed source package entrypoint
    root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root / "src")

    # When: a caller asks for CLI help
    result = subprocess.run(
        [sys.executable, "-m", "ohmymeme", "--help"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    # Then: the package still routes to the bootstrap CLI surface
    assert result.returncode == 0, result.stderr
    assert "--silent" in result.stdout
    assert "--debug-update" in result.stdout
