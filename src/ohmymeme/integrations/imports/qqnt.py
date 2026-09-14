# QQFavoriteExtract compatibility API; algorithms and original GPL notice live in
# ohmymeme_plugin_qqnt. Host retains nickname persistence and output projection.
import json
import os
import time
from importlib import import_module
from pathlib import Path

from ohmymeme.core.adapters.fetch_policy import FetchPolicy

_FETCH_POLICY = FetchPolicy()
DEFAULT_INI_PATH = r"C:\Users\Public\Documents\Tencent\QQ\UserDataInfo.ini"
_EXPORTS = {
    "FILE_SIGNATURES",
    "batch_correct_extensions",
    "copy_directory_with_progress",
    "correct_file_extension",
    "extract_qq_emojis",
    "get_actual_extension",
    "get_available_qq_numbers",
    "get_emoji_dir",
    "get_numeric_subdirectories",
    "get_userdata_save_path",
    "is_content_valid",
    "read_file_with_correct_encoding",
    "sanitize_filename",
}


def __getattr__(name):
    # Lazy named ABI exports let a missing optional package disable only QQNT.
    if name not in _EXPORTS:
        raise AttributeError(name)
    return getattr(import_module("ohmymeme_plugin_qqnt"), name)


def get_extract_status(
    ini_path=DEFAULT_INI_PATH, userdata_save_path=None, fetch_nicknames=False
):
    # Preserve the published three-argument environment probe.
    return import_module("ohmymeme_plugin_qqnt").get_extract_status(
        ini_path, userdata_save_path, get_user_nickname if fetch_nicknames else None
    )


def get_nickname_cache_path():
    # Nickname cache remains host-owned at the shipped APPDATA location.
    appdata = os.getenv("APPDATA") or os.path.join(
        os.getenv("USERPROFILE", ""), "AppData", "Roaming"
    )
    cache_dir = os.path.join(appdata, "OhMyMeme")
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, "nickname_cache.json")


def load_nickname_cache(cache_path=None):
    # Failed cache reads preserve the offline sentinel.
    try:
        with open(
            cache_path or get_nickname_cache_path(), "r", encoding="utf-8"
        ) as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return {}


def save_nickname_cache(cache_data, cache_path=None):
    # Persist only nickname data, not a plugin setting or credential.
    try:
        with open(
            cache_path or get_nickname_cache_path(), "w", encoding="utf-8"
        ) as stream:
            json.dump(cache_data, stream, ensure_ascii=False, indent=2)
    except OSError:
        pass


def get_user_nickname(qq_number, cache_path=None):
    # Keep the existing one-hour host cache and policy-controlled network lookup.
    cache = load_nickname_cache(cache_path)
    now = int(time.time())
    entry = cache.get(str(qq_number))
    if entry and entry.get("username_expire_time", 0) > now:
        return entry.get("name", "")
    try:
        data = json.loads(
            _FETCH_POLICY.fetch_bytes(
                "https://uapis.cn/api/v1/social/qq/userinfo?qq=" + str(qq_number),
                headers={"User-Agent": "OhMyMeme"},
                max_bytes=64 * 1024,
            ).decode("utf-8")
        )
        name = data.get("nickname") or ""
    except Exception:
        return ""
    if name:
        cache[str(qq_number)] = {"name": name, "username_expire_time": now + 3600}
        save_nickname_cache(cache, cache_path)
    return name


def get_display_name(qq_number, fetch_nickname=True, cache_path=None):
    # Project the legacy nickname plus account display name.
    name = get_user_nickname(qq_number, cache_path) if fetch_nickname else ""
    return name + "（" + str(qq_number) + "）" if name else str(qq_number)


def get_default_output_dir(save_path, qq_number, fetch_nickname=True, cache_path=None):
    # The host alone projects the user's final export directory.
    display = get_display_name(qq_number, fetch_nickname, cache_path)
    return os.path.join(
        save_path, __getattr__("sanitize_filename")(display) + "_提取的表情"
    )


def targets_library_output(output_dir, cache_dir):
    # Cache, aliases, children and ancestors must route through host admission.
    output = Path(output_dir).expanduser().resolve(strict=False)
    cache = Path(cache_dir).expanduser().resolve(strict=False)
    return (
        output == cache or output.is_relative_to(cache) or cache.is_relative_to(output)
    )
