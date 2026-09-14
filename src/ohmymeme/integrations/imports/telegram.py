from importlib import import_module

_session = None
_EXPORTS = {
    "is_valid_tdata",
    "find_tdata_path",
    "read_local_key",
    "decrypt_tdf_file",
    "decrypt_tdef_file",
    "detect_extension",
    "dedup_static_against_animated",
}


def __getattr__(name):
    # Named lazy exports keep the shipped module import usable without this extra.
    if name not in _EXPORTS:
        raise AttributeError(name)
    return getattr(import_module("ohmymeme_plugin_telegram"), name)


def _default():
    # A compatibility singleton is not shared with any official factory instance.
    global _session
    if _session is None:
        from ohmymeme.presentation.desktop.import_workers import LegacyImportSession

        _session = LegacyImportSession("source.telegram")
    return _session


def start_tg_import(import_callback, tdata_path=None, passcode="", convert_webm=True):
    # Preserve the callback ABI and all published defaults.
    return _default().start(
        import_callback,
        {"tdata_path": tdata_path, "convert_webm": convert_webm},
        {"passcode": passcode},
    )


def get_tg_progress():
    # Read the same compatibility session used by start.
    return _default().get_progress()


def cancel_tg_import():
    # Preserve the None cancellation sentinel.
    _default().cancel()


def convert_webm_to_webp(webm_path, out_path, timeout=120):
    # Retain the public synchronous conversion helper.
    return _default().provider.convert_webm_to_webp(webm_path, out_path, timeout)
