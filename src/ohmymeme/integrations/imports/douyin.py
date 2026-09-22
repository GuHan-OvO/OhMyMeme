from importlib import import_module

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
