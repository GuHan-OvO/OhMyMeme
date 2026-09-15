"""LAN v1 command handlers independent from UDP/TCP runtime ownership."""

import base64
import binascii
import hashlib
import hmac
import os
import sqlite3
from pathlib import Path

from ohmymeme import __version__
from ohmymeme.app.manifest_service import ManifestService, ManifestValidationError
from ohmymeme.app.remote_mutation_coordinator import (
    RemoteMutationBusyError,
    RemoteMutationConflictError,
)
from ohmymeme.core.assets import AssetPaths
from ohmymeme.core.config import _SECRET_KEYS, get_config
from ohmymeme.core.database import get_db
from ohmymeme.core.imports import ImageImportService, ImportBytes
from ohmymeme.core.manifest import build as build_manifest

MAX_FILE_SIZE = 64 * 1024 * 1024


def _safe_fname(name) -> bool:
    """校验文件名，拒绝路径穿越与绝对路径。"""
    return (
        isinstance(name, str)
        and bool(name)
        and name not in (".", "..")
        and not name.startswith((".", "/", "\\", "~", ".."))
        and "/" not in name
        and "\\" not in name
    )


def _find_meme_file(filename: str, config=None):
    """在缓存目录递归查找表情文件。"""
    cache_dir = (config or get_config()).cache_dir
    direct = cache_dir / filename
    if direct.exists() and direct.is_file():
        return direct
    for root, _, files in os.walk(cache_dir):
        if filename in files:
            full = os.path.join(root, filename)
            if os.path.isfile(full):
                return full
    return None


def _import_bytes(data: bytes, filename: str) -> dict:
    """校验收到的图片字节并原子入库。"""
    try:
        config = get_config()
        result = ImageImportService(
            get_db(), AssetPaths(config.data_dir, config.cache_dir), build_manifest
        ).import_bytes(ImportBytes(data, filename))
    except OSError:
        return {"ok": False, "error": "写入缓存失败"}
    if result.rejected:
        return {"ok": False, "error": "图片解析失败或超过导入限制"}
    if not result.imported_ids:
        return {"ok": True, "dedup": True}
    row = get_db().get_by_id(result.imported_ids[0])
    return {"ok": True, "filename": row["filename"]}


def _detect_ext(data: bytes):
    """按魔数识别图片扩展名，未知返回空串。"""
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"\xff\xd8"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    return ""


class CommandHandlers:
    """处理已获授权的 LAN v1 应用命令。"""

    def __init__(self, server, sync_service=None):
        self._server = server
        self._sync_service = sync_service

    def dispatch(self, msg: dict) -> dict:
        """分发 LAN v1 命令并保持既有响应形状。"""
        cmd = msg.get("cmd")
        if cmd == "pull_manifest":
            return self._cmd_pull_manifest()
        if cmd == "push_manifest":
            return self._cmd_push_manifest(msg.get("manifest"))
        if cmd == "pull_file":
            return self._cmd_pull_file(msg.get("filename", ""))
        if cmd == "push_file":
            return self._cmd_push_file(msg)
        if cmd == "get_config":
            return self._cmd_get_config()
        if cmd == "send_config":
            return self._cmd_send_config(msg.get("config"))
        if cmd == "ping":
            return {"ok": True, "ver": __version__}
        return {"ok": False, "error": f"未知命令: {cmd}"}

    def _cmd_pull_manifest(self) -> dict:
        """返回本地清单。"""
        from ohmymeme.core.manifest import load as load_manifest

        coordinator = getattr(self._server, "_mutation_coordinator", None)
        try:
            if coordinator is None:
                build_manifest()
                return {"ok": True, "manifest": load_manifest()}
            with coordinator.mutation("lan.pull_manifest") as lease:
                self._build_manifest()
                manifest = (
                    self._sync_service.load_manifest()
                    if self._sync_service is not None
                    else load_manifest()
                )
                lease.commit()
                return {"ok": True, "manifest": manifest}
        except (RemoteMutationBusyError, RemoteMutationConflictError) as error:
            return {"ok": False, "error": str(error)}

    def _cmd_push_manifest(self, manifest) -> dict:
        """合并远端清单的排序与分组。"""
        try:
            projection = ManifestService().parse_data(manifest, strict_hash=True)
            coordinator = getattr(self._server, "_mutation_coordinator", None)
            if coordinator is None:
                self._apply_manifest(projection.to_data())
                build_manifest()
            else:
                with coordinator.mutation("lan.push_manifest") as lease:
                    lease.assert_generation()
                    self._apply_manifest(projection.to_data(), lease)
                    self._build_manifest()
                    lease.commit()
        except (
            ManifestValidationError,
            OSError,
            RuntimeError,
            sqlite3.Error,
            ValueError,
        ) as error:
            self._server._logger.warning("push_manifest apply error: %s", error)
            if isinstance(
                error, (RemoteMutationBusyError, RemoteMutationConflictError)
            ):
                return {"ok": False, "error": str(error)}
            return {"ok": False, "error": "manifest 格式错误"}
        return {"ok": True, "local_count": self._db().count()}

    def _apply_manifest(self, manifest, lease=None) -> None:
        """通过注入的同步服务应用 LAN 清单。"""
        if self._sync_service is None:
            from ohmymeme.services.sync.planning import _apply_remote_metadata

            _apply_remote_metadata(manifest, self._db())
            return
        if lease is None:
            self._sync_service.apply_remote_order(manifest)
            self._sync_service.apply_remote_collections(manifest)
        else:
            self._sync_service.apply_remote_order(manifest, lease=lease)
            self._sync_service.apply_remote_collections(manifest, lease=lease)

    def _cmd_pull_file(self, filename: str) -> dict:
        """返回指定缓存文件的 base64 内容。"""
        if not _safe_fname(filename):
            return {"ok": False, "error": "非法文件名"}
        path = _find_meme_file(filename, self._config())
        if not path:
            return {"ok": False, "error": "文件不存在"}
        try:
            data = Path(path).read_bytes()
        except OSError as error:
            return {"ok": False, "error": str(error)}
        return {
            "ok": True,
            "filename": filename,
            "data": base64.b64encode(data).decode(),
        }

    def _cmd_push_file(self, msg: dict) -> dict:
        """校验并导入客户端上传的图片。"""
        filename = msg.get("filename", "")
        if not _safe_fname(filename):
            return {"ok": False, "error": "非法文件名"}
        data_b64 = msg.get("data", "")
        if not data_b64:
            return {"ok": False, "error": "缺少文件数据"}
        try:
            data = base64.b64decode(data_b64, validate=True)
        except (ValueError, binascii.Error):
            return {"ok": False, "error": "文件数据解码失败"}
        if len(data) > MAX_FILE_SIZE:
            return {"ok": False, "error": "文件超过大小限制"}
        expected = msg.get("sha256")
        if expected and not hmac.compare_digest(
            hashlib.sha256(data).hexdigest(), expected
        ):
            return {"ok": False, "error": "文件哈希不一致"}
        coordinator = getattr(self._server, "_mutation_coordinator", None)
        if coordinator is None:
            return _import_bytes(data, filename)
        try:
            with coordinator.mutation("lan.push_file") as lease:
                result = self._server._import_service().import_bytes(
                    ImportBytes(data, filename)
                )
                if result.rejected:
                    return {"ok": False, "error": "图片解析失败或超过导入限制"}
                lease.commit()
                if not result.imported_ids:
                    return {"ok": True, "dedup": True}
                row = self._db().get_by_id(result.imported_ids[0])
                return {"ok": True, "filename": row["filename"]}
        except (RemoteMutationBusyError, RemoteMutationConflictError, OSError) as error:
            return {"ok": False, "error": str(error)}

    def _db(self):
        return getattr(self._server, "_database", None) or get_db()

    def _build_manifest(self):
        builder = getattr(self._server, "_build_manifest", None)
        if builder is None:
            build_manifest()
        else:
            builder()

    def _cmd_get_config(self) -> dict:
        """返回按当前密钥策略过滤的配置。"""
        config = self._config().export_for_lan(self._server._allow_secret_config())
        return {"ok": True, "config": config}

    def _cmd_send_config(self, config) -> dict:
        """按当前密钥策略合并客户端配置。"""
        if not isinstance(config, dict):
            return {"ok": False, "error": "配置格式错误"}
        if not self._server._allow_secret_config():
            config = {
                key: value for key, value in config.items() if key not in _SECRET_KEYS
            }
        coordinator = getattr(self._server, "_mutation_coordinator", None)
        try:
            if coordinator is None:
                target = self._config()
                target.update_from_dict(config)
                target.save()
                return {"ok": True}
            with coordinator.mutation("lan.send_config") as lease:
                target = self._config()
                target.update_from_dict(config)
                target.save()
                lease.commit()
                return {"ok": True}
        except (RemoteMutationBusyError, RemoteMutationConflictError) as error:
            return {"ok": False, "error": str(error)}

    def _config(self):
        return getattr(self._server, "_config", None) or get_config()
