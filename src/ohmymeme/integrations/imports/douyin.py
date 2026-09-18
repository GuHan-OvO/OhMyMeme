from importlib import import_module

_session = None
_EXPORTS = {
    "API_STICKER",
    "API_TTWID",
    "API_SELF",
    "UA",
    "HEADERS",
    "_gen_random_str",
    "_gen_verify_fp",
    "_sign_url",
    "_policy_request",
    "_build_session",
    "_check_login",
    "_download_sticker",
}


def __getattr__(name):
    # Explicit lazy aliases retain CLI/library consumers, not algorithm copies.
    if name not in _EXPORTS:
        raise AttributeError(name)
    return getattr(import_module("ohmymeme_plugin_douyin"), name)


def _default():
    # Legacy callers retain one session; official factories are independent.
    global _session
    if _session is None:
        from ohmymeme.app.legacy_imports import LegacyImportSession

        _session = LegacyImportSession("source.douyin")
    return _session


def start_douyin_import(import_callback, cookie: str) -> bool:
    # Cookie is operation-only, matching the original callback signature.
    return _default().start(import_callback, {}, {"cookie": cookie})


def get_douyin_progress():
    # Poll the same provider that accepted start.
    return _default().get_progress()


def cancel_douyin_import():
    # Keep the existing None sentinel for cancellation.
    _default().cancel()


def _fetch_sticker_list(session):
    # Preserve the CLI/library list helper without a second implementation.
    return _default().provider._fetch_sticker_list(session)
