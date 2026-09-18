"""Legacy import shim regression contracts."""

from types import SimpleNamespace
from unittest.mock import Mock


def test_legacy_telegram_shim_admits_a_host_callback_without_presentation(
    monkeypatch, tmp_path
):
    """Given the legacy Telegram ABI, it delivers media through its callback port."""
    from ohmymeme.app import legacy_imports
    from ohmymeme.core.domain import TaskKind
    from ohmymeme.integrations.imports import telegram

    class Provider:
        provider_id = "source.telegram"
        api_version = 1

        def __init__(self):
            self._progress = {"status": "idle"}

        def get_progress(self):
            return dict(self._progress)

        def start(self, _context):
            return True

        def import_media(self, context):
            path = context.operation.temporary.path("callback.gif")
            path.write_bytes(b"legacy callback")
            context.sink.import_batch((SimpleNamespace(path=path),))
            self._progress = {"status": "done"}

        def stop(self):
            pass

        def finish(self):
            pass

    class Registry:
        def __init__(self, _descriptors):
            self._provider = Provider()

        def require(self, _provider_id):
            return self._provider

    callback = Mock(return_value={"ids": [7], "rejected": 0})
    previous = telegram._session
    monkeypatch.setattr(legacy_imports, "PluginRegistry", Registry)
    telegram._session = None
    try:
        assert telegram.start_tg_import(callback, str(tmp_path), "", False)
        telegram._session.coordinator.wait(TaskKind.IMPORT_TELEGRAM, 5)
        callback.assert_called_once()
        assert callback.call_args.args[0][0].endswith("callback.gif")
        assert telegram.get_tg_progress()["status"] == "done"
    finally:
        telegram._session = previous
