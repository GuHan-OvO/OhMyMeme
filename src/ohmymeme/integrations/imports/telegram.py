from importlib import import_module

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
