"""已发布微信 ABI 与宿主持有的 helper 资源缓存。"""

import logging
import os
import platform
import shutil
import tempfile
import threading
from importlib import import_module
from pathlib import Path

from ohmymeme.core.adapters.fetch_policy import FetchPolicy
from ohmymeme.core.assets import ResourceLocator

logger = logging.getLogger(__name__)
_FETCH_POLICY = FetchPolicy()
_WECHAT_KEYFINDER_SHA256 = {
    "Windows": "72f281c6b7638735b13c2c80f8de92034f49ce0e20d75feb6640c8c6e0dd4e31",
}
_WECHAT_KEYFINDER_URLS = {
    "Windows": (
        "https://github.com/ZE514/OhMyMeme/releases/download/v0.6.3/"
        "wechat_keyfinder-windows-x64.exe"
    ),
}
_DL_LOCK = threading.Lock()


def _implementation():
    # Importing the compatibility module itself does not activate an optional extra.
    return import_module("ohmymeme_plugin_wechat")


def _get_wechat_dir():
    # Durable helper storage belongs to the host, not to a provider context.
    if platform.system() == "Windows":
        base = os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
    else:
        base = Path.home() / ".local" / "share"
    return Path(base) / "OhMyMeme" / ".wechat"


def _binary_name():
    # Preserve the shipped resource identity.
    return (
        "wechat_keyfinder.exe" if platform.system() == "Windows" else "wechat_keyfinder"
    )


def _offsets_path():
    # ResourceLocator remains entirely inside the host.
    return ResourceLocator.for_source(Path.home()).offsets_path


def detect_wechat_keyfinder():
    # Preserve the published path-or-empty sentinel.
    candidate = _get_wechat_dir() / _binary_name()
    return str(candidate) if candidate.exists() else ""


def verify_binary_integrity(path):
    # The actual release hash and explicit development exception are host policy.
    return _implementation().verify_binary_integrity(
        path,
        _WECHAT_KEYFINDER_SHA256.get(platform.system(), ""),
        os.environ.get("OHMYMEME_INSECURE_SKIP_HELPER_HASH") == "1",
    )


def _ensure_keyfinder(cancelled):
    # Synchronous bounded fetch runs on the coordinator worker, not a detached thread.
    expected = _WECHAT_KEYFINDER_SHA256.get(platform.system(), "")
    if (not expected or expected == "PLACEHOLDER_UPDATE_ON_RELEASE") and os.environ.get(
        "OHMYMEME_INSECURE_SKIP_HELPER_HASH"
    ) != "1":
        return ""
    if cancelled() or not _DL_LOCK.acquire(timeout=0.25):
        return ""
    temporary = None
    try:
        cached = detect_wechat_keyfinder()
        if cached and verify_binary_integrity(cached):
            return cached
        url = _WECHAT_KEYFINDER_URLS.get(platform.system(), "")
        if not url or cancelled():
            return ""
        root = _get_wechat_dir()
        root.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="helper-", suffix=".tmp", dir=root)
        os.close(fd)
        _FETCH_POLICY.download_to(
            url, Path(temporary), headers={"User-Agent": "OhMyMeme"}
        )
        if cancelled() or not verify_binary_integrity(temporary):
            return ""
        destination = root / _binary_name()
        Path(temporary).replace(destination)
        return str(destination)
    except (OSError, RuntimeError) as error:
        logger.error("wechat_keyfinder download failed: %s", error)
        return ""
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
        _DL_LOCK.release()


def ensure_wechat_keyfinder():
    # Retain the public zero-argument compatibility entry point.
    return _ensure_keyfinder(lambda: False)


def prepare_wechat_helper(operation, cancelled):
    # Only operation-owned copies cross the resource port, never durable paths.
    operation.require("process.helper")
    operation.require("network.https")
    binary = _ensure_keyfinder(cancelled)
    if not binary or cancelled():
        return None
    staged = operation.temporary.path("helper/" + _binary_name())
    offsets = operation.temporary.path("helper/offsets.json")
    shutil.copy2(binary, staged)
    if not verify_binary_integrity(staged):
        return None
    shutil.copy2(_offsets_path(), offsets)
    return str(staged), str(offsets)


def inspect_wechat_environment(user_root=None):
    # Inspection does not need a worker or persistence.
    return _implementation().inspect_wechat_environment(user_root)
