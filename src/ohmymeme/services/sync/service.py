"""云端同步 - FTP / S3 (兼容 R2/MinIO) 实现"""

import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, TypeVar

from ohmymeme.app.manifest_service import ManifestService, ManifestValidationError
from ohmymeme.app.pull_commit_service import PullCommitError, PullCommitService
from ohmymeme.app.remote_mutation_coordinator import (
    RemoteMutationCoordinator,
    RemoteMutationLease,
)
from ohmymeme.app.remote_mutation_errors import RemoteMutationWorkerError
from ohmymeme.core.assets import AssetPaths
from ohmymeme.core.config import Config, get_config
from ohmymeme.core.database import MemeDB, get_db
from ohmymeme.core.imports import ImageImportService, ImportPath
from ohmymeme.core.manifest import INDEX_FILENAME
from ohmymeme.core.manifest import _write as write_manifest
from ohmymeme.core.manifest import build as build_manifest
from ohmymeme.core.manifest import load as load_manifest

from . import planning
from .backends import SyncError, connect_ftp, get_backend

logger = logging.getLogger(__name__)

REMOTE_INDEX = planning.REMOTE_INDEX
REMOTE_MEME_DIR = planning.REMOTE_MEME_DIR
_HEARTBEAT_INTERVAL = 5

_sync_lock = threading.Lock()
_sync_run_lock = threading.Lock()  # 防止 push/pull 并发执行


@dataclass(frozen=True, slots=True)
class _SyncRuntime:
    config: Config
    database: MemeDB
    build_manifest: Callable[[], None]
    write_manifest: Callable[[dict], None]
    coordinator: RemoteMutationCoordinator
    lease: RemoteMutationLease | None
    legacy_lock: bool = False


_SYNC_RUNTIME: ContextVar[_SyncRuntime | None] = ContextVar(
    "ohmymeme_sync_runtime", default=None
)
_LegacyResult = TypeVar("_LegacyResult")


class SyncService:
    """Bind the legacy sync algorithm to one Container and its mutation lease."""

    def __init__(
        self,
        config: Config,
        database: MemeDB,
        build_manifest: Callable[[], None],
        write_manifest: Callable[[dict], None],
        coordinator: RemoteMutationCoordinator,
    ) -> None:
        self._config = config
        self._database = database
        self._build_manifest = build_manifest
        self._write_manifest = write_manifest
        self._coordinator = coordinator

    def push(self, delete_remote: bool | None = None) -> dict:
        with self._coordinator.mutation("sync.push") as lease:
            with self._bind(lease):
                result = _push_impl(delete_remote)
                lease.commit()
                return result

    def pull(self, remove_local: bool | None = None) -> dict:
        with self._coordinator.mutation("sync.pull") as lease:
            with self._bind(lease):
                result = _pull_impl(remove_local)
                lease.commit()
                return result

    def upload_index(self) -> bool:
        with self._coordinator.mutation("sync.upload_index") as lease:
            with self._bind(lease):
                result = _upload_index_impl()
                if result:
                    lease.commit()
                return result

    def delete_all_remote(self) -> dict:
        with self._coordinator.mutation("sync.delete_all") as lease:
            with self._bind(lease):
                result = _delete_all_remote_impl()
                if result.get("ok"):
                    lease.commit()
                return result

    def cleanup_remote_orphans(self, delete: bool = False) -> dict:
        if not delete:
            with self._bind(None):
                return _cleanup_remote_orphans_impl(False)
        with self._coordinator.mutation("sync.cleanup") as lease:
            with self._bind(lease):
                result = _cleanup_remote_orphans_impl(delete)
                if result.get("ok"):
                    lease.commit()
                return result

    def download_index(self) -> dict | None:
        with self._bind(None):
            return download_index()

    def sync_test(self) -> str:
        with self._bind(None):
            return sync_test()

    def get_sync_progress(self) -> dict:
        return get_sync_progress()

    def apply_remote_order(
        self, remote_data: dict, lease: RemoteMutationLease | None = None
    ) -> None:
        if lease is not None:
            with self._bind(lease):
                _apply_remote_order(remote_data)
            return
        with self._coordinator.mutation("sync.apply_remote_order") as owned_lease:
            with self._bind(owned_lease):
                _apply_remote_order(remote_data)
                owned_lease.commit()

    def apply_remote_collections(
        self, remote_data: dict, lease: RemoteMutationLease | None = None
    ) -> None:
        if lease is not None:
            with self._bind(lease):
                _apply_remote_collections(remote_data)
            return
        with self._coordinator.mutation("sync.apply_remote_collections") as owned_lease:
            with self._bind(owned_lease):
                _apply_remote_collections(remote_data)
                owned_lease.commit()

    def load_manifest(self) -> dict:
        with self._bind(None):
            return _load_runtime_manifest()

    @contextmanager
    def _bind(self, lease: RemoteMutationLease | None) -> Iterator[None]:
        token = _SYNC_RUNTIME.set(
            _SyncRuntime(
                self._config,
                self._database,
                self._build_manifest,
                self._write_manifest,
                self._coordinator,
                lease,
                False,
            )
        )
        try:
            yield
        finally:
            _SYNC_RUNTIME.reset(token)


def _runtime() -> _SyncRuntime | None:
    return _SYNC_RUNTIME.get()


def _validate_worker_lease(lease: RemoteMutationLease) -> _SyncRuntime:
    runtime = _runtime()
    if runtime is None or runtime.lease is not lease:
        raise RemoteMutationWorkerError("mismatched")
    runtime.coordinator.validate_lease(lease, owner_required=False)
    lease.assert_generation()
    return runtime


def _runtime_config() -> Config:
    runtime = _runtime()
    return runtime.config if runtime is not None else get_config()


def _runtime_db() -> MemeDB:
    runtime = _runtime()
    return runtime.database if runtime is not None else get_db()


def _runtime_build_manifest() -> None:
    runtime = _runtime()
    if runtime is None:
        build_manifest()
    else:
        runtime.build_manifest()


def _runtime_write_manifest(data: dict) -> None:
    runtime = _runtime()
    if runtime is None:
        write_manifest(data)
    else:
        runtime.write_manifest(data)


def _load_runtime_manifest() -> dict:
    runtime = _runtime()
    if runtime is None:
        return load_manifest()
    path = runtime.config.data_dir / INDEX_FILENAME
    if not path.exists():
        return {"version": 3, "memes": [], "collections": []}
    return ManifestService().parse_json(path.read_bytes()).to_data()


@contextmanager
def _bind_legacy_runtime(
    coordinator: RemoteMutationCoordinator,
    lease: RemoteMutationLease,
    config: Config | None = None,
    database: MemeDB | None = None,
) -> Iterator[None]:
    token = _SYNC_RUNTIME.set(
        _SyncRuntime(
            config or get_config(),
            database or get_db(),
            build_manifest,
            write_manifest,
            coordinator,
            lease,
            True,
        )
    )
    try:
        yield
    finally:
        _SYNC_RUNTIME.reset(token)


def _run_legacy_mutation(
    entrypoint: str,
    coordinator: RemoteMutationCoordinator | None,
    operation: Callable[[], _LegacyResult],
) -> _LegacyResult:
    if coordinator is None and _runtime() is None:
        owned = RemoteMutationCoordinator(_runtime_config().data_dir)
        try:
            return _run_legacy_mutation(entrypoint, owned, operation)
        finally:
            owned.close()
    if _runtime() is not None:
        return operation()
    with coordinator.mutation(entrypoint) as lease:
        with _bind_legacy_runtime(coordinator, lease):
            result = operation()
            lease.commit()
            return result


# 同步进度状态（全局，供 JS 轮询）
_sync_state = {
    "status": "idle",  # idle | uploading | downloading | done | error
    "direction": "",  # upload | download
    "progress": 0,  # 0-100
    "files_done": 0,
    "files_total": 0,
    "bytes_done": 0,
    "bytes_total": 0,
    "current_file": "",
    "speed": 0,  # bytes/sec
    "start_time": 0,
    "results": None,
    "error": "",
    "failed_items": [],  # [{filename, status: error|unknown, error}]
}


def _reset_sync_state(direction, files_total, bytes_total):
    global _sync_state
    _sync_state.update(
        status="idle",
        direction=direction,
        progress=0,
        files_done=0,
        files_total=files_total,
        bytes_done=0,
        bytes_total=bytes_total,
        current_file="",
        speed=0,
        start_time=0,
        results=None,
        error="",
        failed_items=[],
    )


def _update_sync_state(**kw):
    with _sync_lock:
        _sync_state.update(**kw)


def _increment_sync_progress(files_add=0, bytes_add=0, current_file=""):
    """原子递增进度计数器（多线程安全）"""
    with _sync_lock:
        _sync_state["files_done"] += files_add
        _sync_state["bytes_done"] += bytes_add
        if current_file:
            _sync_state["current_file"] = current_file


def get_sync_progress() -> dict:
    """返回当前同步进度（供 JS 轮询）"""
    s = _sync_state
    if s["start_time"] > 0 and s["status"] in ("uploading", "downloading"):
        elapsed = max(time.time() - s["start_time"], 0.001)
        s["speed"] = s["bytes_done"] / elapsed
        if s["bytes_total"] > 0:
            s["progress"] = min(int(s["bytes_done"] * 100 / s["bytes_total"]), 99)
    return dict(s)


def _get_backend():
    return get_backend(_runtime_config())


def _connect():
    """快捷方式：直接建立 FTP 连接（供 sync_test_ftp 等外部调用）"""
    return connect_ftp(_runtime_config())


def _chunk_list(lst, n):
    return planning._chunk_list(lst, n)


def _remote_root(cfg):
    return planning._remote_root(cfg)


def _safe_remote_fname(name: str) -> bool:
    return planning._safe_remote_fname(name)


def _fetch_remote_memes(bk, remote_root):
    return planning._fetch_remote_memes(bk, remote_root, _runtime_config())


def _apply_remote_collections(remote_data: dict):
    def operation():
        return planning._apply_remote_collections(remote_data, _runtime_db())

    return _run_legacy_mutation("sync.apply_remote_collections", None, operation)


def _apply_remote_order(remote_data: dict):
    def operation():
        return planning._apply_remote_order(remote_data, _runtime_db())

    return _run_legacy_mutation("sync.apply_remote_order", None, operation)


def _apply_remote_metadata(remote_data: dict):
    def operation():
        return planning._apply_remote_metadata(remote_data, _runtime_db())

    return _run_legacy_mutation("sync.apply_remote_metadata", None, operation)


def list_remote_orphans(bk, remote_root) -> list:
    return planning.list_remote_orphans(bk, remote_root, _runtime_config())


# ─── 多线程工作函数 ───


def _push_worker(entries, remote_root, cache_dir, remote_memes):
    """单线程批量上传一批文件"""
    runtime = _runtime()
    if runtime is None or runtime.lease is None:
        raise RemoteMutationWorkerError("push")
    _validate_worker_lease(runtime.lease)
    bk = _get_backend()
    bk.connect()
    local_results = {
        "uploaded": 0,
        "skipped": 0,
        "errors": 0,
        "bytes": 0,
        "failed": [],
        "confirmed": [],
    }
    try:
        for entry in entries:
            fname = entry["filename"]
            local_file = cache_dir / fname
            fsize = local_file.stat().st_size if local_file.exists() else 0
            local_hash = entry["sha256"]
            remote_entry = remote_memes.get(fname)
            rem_path = remote_root.rstrip("/") + "/" + REMOTE_MEME_DIR + "/" + fname
            if remote_entry and remote_entry.get("sha256") == local_hash:
                try:
                    remote_ok = bk.file_exists(rem_path)
                except Exception:
                    remote_ok = False
                if remote_ok:
                    local_results["skipped"] += 1
                    local_results["confirmed"].append(fname)
                    _increment_sync_progress(files_add=1)
                    continue
            if not local_file.exists():
                local_results["errors"] += 1
                local_results["failed"].append(
                    {"filename": fname, "status": "error", "error": "本地文件缺失"}
                )
                _increment_sync_progress(files_add=1)
                continue
            bk.ensure_remote_dir(os.path.dirname(rem_path))
            if bk.upload_file(local_file, rem_path):
                local_results["uploaded"] += 1
                local_results["bytes"] += fsize
                local_results["confirmed"].append(fname)
                _increment_sync_progress(files_add=1, bytes_add=fsize)
            else:
                local_results["errors"] += 1
                local_results["failed"].append(
                    {"filename": fname, "status": "error", "error": "上传失败"}
                )
                _increment_sync_progress(files_add=1)
        return local_results
    except Exception as e:
        logger.warning("push worker error: %s", e)
        done = (
            local_results["uploaded"]
            + local_results["skipped"]
            + local_results["errors"]
        )
        remaining = len(entries) - done
        if remaining > 0:
            local_results["errors"] += remaining
            _increment_sync_progress(files_add=remaining)
            local_results["failed"].append(
                {
                    "filename": "",
                    "status": "error",
                    "error": "%d 个文件因 worker 中断未处理" % remaining,
                }
            )
        return local_results
    finally:
        bk.close()


def _push_worker_with_barrier(
    lease: RemoteMutationLease, entries, remote_root, cache_dir, remote_memes
):
    _validate_worker_lease(lease)
    try:
        return _push_worker(entries, remote_root, cache_dir, remote_memes)
    finally:
        lease.worker_done()


def _heartbeat_data(local, remote_memes, confirmed):
    confirmed_names = set(confirmed)
    local_by_name = {entry["filename"]: entry for entry in local["memes"]}
    entries = [
        entry for name, entry in remote_memes.items() if name not in confirmed_names
    ]
    entries.extend(local_by_name[name] for name in confirmed if name in local_by_name)
    entries = [{**entry, "sort_order": index} for index, entry in enumerate(entries)]
    known_names = {entry["filename"] for entry in entries}

    def retain(collection):
        children = [
            retained
            for child in collection.get("children", [])
            if (retained := retain(child))
        ]
        filenames = [
            name for name in collection.get("filenames", []) if name in known_names
        ]
        if not filenames and not children:
            return None
        data = {"name": collection["name"], "filenames": filenames}
        if children:
            data["children"] = children
        return data

    collections = [
        retained for item in local.get("collections", []) if (retained := retain(item))
    ]
    return (
        ManifestService()
        .parse_data({"version": 3, "memes": entries, "collections": collections})
        .to_data()
    )


def _publish_heartbeat(backend, remote_path, local, remote_memes, confirmed, data_dir):
    temporary = None
    try:
        data = _heartbeat_data(local, remote_memes, confirmed)
        descriptor, name = tempfile.mkstemp(
            prefix=".manifest-heartbeat-", suffix=".json", dir=str(data_dir)
        )
        os.close(descriptor)
        temporary = Path(name)
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        if not backend.upload_file(temporary, remote_path):
            logger.warning("manifest heartbeat upload failed")
    except (ManifestValidationError, OSError, SyncError) as error:
        logger.warning("manifest heartbeat failed: %s", error)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _pull_worker(entries, remote_root, cache_dir, db):
    """单线程批量下载一批文件"""
    runtime = _runtime()
    if runtime is None or runtime.lease is None:
        raise RemoteMutationWorkerError("pull")
    _validate_worker_lease(runtime.lease)
    bk = _get_backend()
    bk.connect()
    local_results = {
        "downloaded": 0,
        "skipped": 0,
        "errors": 0,
        "bytes": 0,
        "failed": [],
        "created_files": [],
        "created_meme_ids": [],
        "overwritten_files": [],
    }
    local_idx = {}
    try:
        from ohmymeme.core.manifest import load as _load_manifest

        ld = _load_manifest()
        local_idx = {m["filename"]: m for m in ld.get("memes", [])}
    except Exception:
        pass
    try:
        for fname, rentry in entries:
            if not _safe_remote_fname(fname):
                local_results["skipped"] += 1
                _increment_sync_progress(files_add=1)
                continue
            expected_hash = rentry.get("sha256")
            if not (
                isinstance(expected_hash, str)
                and len(expected_hash) == 64
                and all(character in "0123456789abcdef" for character in expected_hash)
            ):
                local_results["errors"] += 1
                local_results["failed"].append(
                    {"filename": fname, "status": "error", "error": "远端哈希非法"}
                )
                _increment_sync_progress(files_add=1)
                continue
            local_entry = local_idx.get(fname)
            if (
                local_entry
                and local_entry.get("sha256") == rentry.get("sha256")
                and (cache_dir / fname).exists()
            ):
                local_results["skipped"] += 1
                _increment_sync_progress(files_add=1)
                continue
            rem_path = remote_root.rstrip("/") + "/" + REMOTE_MEME_DIR + "/" + fname
            local_path = cache_dir / fname
            fsize = rentry.get("file_size", 0)
            backup_path = None
            if local_path.exists():
                fd, backup_name = tempfile.mkstemp(
                    prefix=".%s.pull-" % fname,
                    suffix=".bak",
                    dir=str(local_path.parent),
                )
                os.close(fd)
                backup_path = Path(backup_name)
                backup_path.unlink()
                os.replace(local_path, backup_path)
            if bk.download_file(rem_path, local_path):
                if local_path.stat().st_size == 0:
                    # 下载到空文件视为失败：清理并计错误，避免污染本地清单
                    local_results["errors"] += 1
                    local_results["failed"].append(
                        {"filename": fname, "status": "error", "error": "下载内容为空"}
                    )
                    _increment_sync_progress(files_add=1)
                    if local_path.exists():
                        local_path.unlink()
                    if backup_path is not None:
                        os.replace(backup_path, local_path)
                    continue
                with open(local_path, "rb") as downloaded:
                    actual_hash = hashlib.file_digest(downloaded, "sha256").hexdigest()
                if actual_hash != expected_hash:
                    local_results["errors"] += 1
                    local_results["failed"].append(
                        {
                            "filename": fname,
                            "status": "error",
                            "error": "下载哈希不一致",
                        }
                    )
                    _increment_sync_progress(files_add=1)
                    local_path.unlink(missing_ok=True)
                    if backup_path is not None:
                        os.replace(backup_path, local_path)
                    continue
                row = db.get_by_filename(fname)
                if not row:
                    try:
                        oname = rentry.get("name", "") or os.path.splitext(fname)[0]
                        imported = ImageImportService(
                            db,
                            AssetPaths(_runtime_config().data_dir, cache_dir),
                            lambda: None,
                        ).register_existing_path(ImportPath(local_path, oname))
                        if imported.rejected:
                            logger.info(f"pull skip (invalid image): {fname}")
                            local_results["skipped"] += 1
                            _increment_sync_progress(files_add=1)
                            if local_path.exists():
                                local_path.unlink()
                            if backup_path is not None:
                                os.replace(backup_path, local_path)
                            continue
                        local_results["created_meme_ids"].extend(imported.imported_ids)
                        local_results["created_files"].append(local_path)
                    except Exception as e:
                        # DB 写入失败：清理残留 cache，避免“文件在但无记录”的游离态
                        logger.warning("pull db add failed %s: %s", fname, e)
                        local_results["errors"] += 1
                        local_results["failed"].append(
                            {
                                "filename": fname,
                                "status": "error",
                                "error": "数据库写入失败",
                            }
                        )
                        _increment_sync_progress(files_add=1)
                        if local_path.exists():
                            local_path.unlink()
                        if backup_path is not None:
                            os.replace(backup_path, local_path)
                        continue
                if backup_path is not None:
                    local_results["overwritten_files"].append((local_path, backup_path))
                local_results["downloaded"] += 1
                local_results["bytes"] += fsize
                _increment_sync_progress(files_add=1, bytes_add=fsize)
            else:
                if backup_path is not None:
                    os.replace(backup_path, local_path)
                local_results["errors"] += 1
                local_results["failed"].append(
                    {"filename": fname, "status": "error", "error": "下载失败"}
                )
                _increment_sync_progress(files_add=1)
        return local_results
    except Exception as e:
        logger.warning("pull worker error: %s", e)
        done = (
            local_results["downloaded"]
            + local_results["skipped"]
            + local_results["errors"]
        )
        remaining = len(entries) - done
        if remaining > 0:
            local_results["errors"] += remaining
            _increment_sync_progress(files_add=remaining)
            local_results["failed"].append(
                {
                    "filename": "",
                    "status": "error",
                    "error": "%d 个文件因 worker 中断未处理" % remaining,
                }
            )
        return local_results
    finally:
        bk.close()


def _rollback_pull_changes(db, aggregated):
    for meme_id in aggregated["created_meme_ids"]:
        db.delete_meme(meme_id)
    for path in aggregated["created_files"]:
        if path.exists():
            path.unlink()
    for path, backup_path in aggregated["overwritten_files"]:
        if path.exists():
            path.unlink()
        os.replace(backup_path, path)


def _discard_pull_backups(aggregated):
    for _path, backup_path in aggregated["overwritten_files"]:
        if backup_path.exists():
            backup_path.unlink()


# ─── 公开 API ───


def upload_index(bk=None) -> bool:
    """保留旧入口，并纳入短生命周期 library-state lease。"""
    return _run_legacy_mutation(
        "sync.upload_index", None, lambda: _upload_index_impl(bk)
    )


def _upload_index_impl(bk=None) -> bool:
    """上传本地 manifest 到远端"""
    cfg = _runtime_config()
    remote_root = _remote_root(cfg)
    _runtime_build_manifest()
    local_index = cfg.data_dir / INDEX_FILENAME
    ManifestService().parse_json(local_index.read_bytes())
    own_backend = bk is None
    if own_backend:
        bk = _get_backend()
        bk.connect()
    try:
        runtime = _runtime()
        if runtime is not None:
            runtime.coordinator.assert_generation(runtime.lease.generation)
        bk.ensure_remote_dir(remote_root)
        remote_path = remote_root.rstrip("/") + "/" + REMOTE_INDEX
        ok = bk.upload_file(local_index, remote_path)
        if ok:
            logger.info("manifest uploaded")
        return ok
    finally:
        if own_backend:
            bk.close()


def download_index() -> Optional[dict]:
    """从远端下载 manifest。

    无 manifest 返回 None；读取/解析失败抛 SyncError。
    """
    cfg = _runtime_config()
    remote_root = _remote_root(cfg)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".remote-index-", suffix=".json", dir=str(cfg.data_dir)
    )
    os.close(fd)
    tmp = Path(tmp_name)
    bk = _get_backend()
    bk.connect()
    try:
        remote_path = remote_root.rstrip("/") + "/" + REMOTE_INDEX
        if not bk.file_exists(remote_path):
            return None
        if not bk.download_file(remote_path, tmp):
            raise SyncError("远端 manifest 下载失败")
        raw_bytes = tmp.read_bytes()
        return ManifestService().parse_json(raw_bytes, strict_hash=True).to_data()
    except SyncError:
        raise
    except Exception as e:
        logger.warning("download_index failed: %s", e)
        raise SyncError("远端 manifest 解析失败: %s" % e)
    finally:
        bk.close()
        if tmp.exists():
            tmp.unlink()


def push(delete_remote: bool = None) -> dict:
    """保留旧入口，并纳入短生命周期 library-state lease。"""
    return _run_legacy_mutation("sync.push", None, lambda: _push_impl(delete_remote))


def _push_impl(delete_remote: bool = None) -> dict:
    """本地 -> 远端：上传缺失/变更的表情包和清单（多线程）"""
    cfg = _runtime_config()
    if delete_remote is None:
        delete_remote = cfg.get("sync_delete_remote", False)
    remote_root = _remote_root(cfg)
    cache_dir = cfg.cache_dir
    max_workers = max(1, min(8, int(cfg.get("sync_threads", 3))))
    local = _load_runtime_manifest()
    if not local.get("memes"):
        _runtime_build_manifest()
        local = _load_runtime_manifest()
        if not local.get("memes"):
            raise SyncError("local manifest is empty, nothing to push")

    # 计算总文件数和字节数
    files_total = len(local["memes"])
    bytes_total = 0
    for entry in local["memes"]:
        fp = cache_dir / entry["filename"]
        if fp.exists():
            bytes_total += fp.stat().st_size

    runtime = _runtime()
    owns_sync_lock = runtime is None or runtime.legacy_lock
    if owns_sync_lock and not _sync_run_lock.acquire(blocking=False):
        raise SyncError("同步正在进行中")

    _reset_sync_state("upload", files_total, bytes_total)
    start = time.time()
    _update_sync_state(status="uploading", start_time=start)

    bk = None
    try:
        bk = _get_backend()
        bk.connect()
        remote_memes = _fetch_remote_memes(bk, remote_root)
        bk.ensure_remote_dir(remote_root)
        local_idx = {m["filename"]: m for m in local["memes"]}

        entries = local["memes"]
        if max_workers <= 1 or len(entries) <= 1:
            chunks = [entries]
        else:
            chunks = _chunk_list(entries, min(max_workers, len(entries)))

        aggregated = {
            "uploaded": 0,
            "skipped": 0,
            "errors": 0,
            "bytes": 0,
            "failed": [],
            "confirmed": [],
        }
        heartbeat_at = time.monotonic()
        runtime = _runtime()
        lease = runtime.lease if runtime is not None else None
        if lease is not None:
            lease.start_workers(len(chunks))
        with ThreadPoolExecutor(max_workers=len(chunks)) as executor:
            if lease is None:
                futures = [
                    executor.submit(
                        copy_context().run,
                        _push_worker,
                        ch,
                        remote_root,
                        cache_dir,
                        remote_memes,
                    )
                    for ch in chunks
                ]
            else:
                futures = [
                    executor.submit(
                        copy_context().run,
                        _push_worker_with_barrier,
                        lease,
                        ch,
                        remote_root,
                        cache_dir,
                        remote_memes,
                    )
                    for ch in chunks
                ]
            for future in as_completed(futures):
                r = future.result()
                aggregated["uploaded"] += r["uploaded"]
                aggregated["skipped"] += r["skipped"]
                aggregated["errors"] += r["errors"]
                aggregated["bytes"] += r["bytes"]
                aggregated["failed"].extend(r.get("failed", []))
                aggregated["confirmed"].extend(r.get("confirmed", []))
                if time.monotonic() - heartbeat_at >= _HEARTBEAT_INTERVAL:
                    lease = _runtime().lease if _runtime() is not None else None
                    if lease is not None:
                        lease.assert_generation()
                    _publish_heartbeat(
                        bk,
                        remote_root.rstrip("/") + "/" + REMOTE_INDEX,
                        local,
                        remote_memes,
                        aggregated["confirmed"],
                        cfg.data_dir,
                    )
                    heartbeat_at = time.monotonic()
            if lease is not None:
                lease.wait_for_workers(timeout=1)

        if aggregated["errors"] > 0:
            _update_sync_state(failed_items=aggregated["failed"])
            msg = "%d 个文件上传失败，未更新远端 manifest" % aggregated["errors"]
            logger.warning("sync push aborted: %s", msg)
            raise SyncError(msg)

        failed_files = list(aggregated["failed"])
        results = {
            "uploaded": aggregated["uploaded"],
            "skipped": aggregated["skipped"],
            "errors": aggregated["errors"],
            "deleted": 0,
            "failed_files": failed_files,
        }

        deleted_fnames = set()
        if delete_remote:
            for fname in list(remote_memes.keys()):
                if fname not in local_idx:
                    rem_path = (
                        remote_root.rstrip("/") + "/" + REMOTE_MEME_DIR + "/" + fname
                    )
                    if bk.delete_file(rem_path):
                        deleted_fnames.add(fname)
                        results["deleted"] += 1
                    else:
                        # 删除结果不确定：复核远端是否真的已删
                        unknown = False
                        try:
                            still = bk.file_exists(rem_path)
                        except Exception:
                            still = True
                            unknown = True  # 复核异常 → unknown，保留待下次重查
                        if not still:
                            # 复核确认已删 → 视为删除成功
                            deleted_fnames.add(fname)
                            results["deleted"] += 1
                        else:
                            # 仍在/未知 → 保留在远端 manifest，记录失败供 UI 展示
                            failed_files.append(
                                {
                                    "filename": fname,
                                    "status": "unknown" if unknown else "error",
                                    "error": (
                                        "删除结果不确定，将在下次同步复核"
                                        if unknown
                                        else "远端删除失败（文件仍存在）"
                                    ),
                                }
                            )
        runtime = _runtime()
        if runtime is not None:
            runtime.coordinator.assert_generation(runtime.lease.generation)
        _runtime_build_manifest()
        remote_manifest_path = remote_root.rstrip("/") + "/" + REMOTE_INDEX
        merged_file = None
        try:
            # 远端仍保留、但本地清单没有的项合并进待上传清单，避免孤儿
            data = load_manifest()
            local_fnames = {m["filename"] for m in data["memes"]}
            kept = [
                m
                for fname, m in remote_memes.items()
                if fname not in local_fnames and fname not in deleted_fnames
            ]
            manifest_file = cfg.data_dir / INDEX_FILENAME
            if kept:
                data["memes"] = [
                    {**entry, "sort_order": index}
                    for index, entry in enumerate([*data["memes"], *kept])
                ]
                fd, tmp_name = tempfile.mkstemp(
                    prefix=".remote-merged-", suffix=".json", dir=str(cfg.data_dir)
                )
                os.close(fd)
                merged_file = Path(tmp_name)
                merged_file.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                manifest_file = merged_file
            ok = bk.upload_file(manifest_file, remote_manifest_path)
            if not ok:
                raise SyncError("远端 manifest 上传失败")
        finally:
            if merged_file is not None and merged_file.exists():
                try:
                    merged_file.unlink()
                except Exception:
                    pass

        _update_sync_state(
            status="done",
            progress=100,
            files_done=files_total,
            bytes_done=bytes_total,
            results=results,
            failed_items=results["failed_files"],
        )
        logger.info("sync push done: %s", results)
        return results
    except Exception as e:
        _update_sync_state(status="error", error=str(e))
        raise
    finally:
        if bk is not None:
            bk.close()
        if owns_sync_lock:
            _sync_run_lock.release()


def _apply_pulled_metadata(projection, database):
    data = projection.to_data()
    method = getattr(database, "apply_remote_pull_metadata", None)
    if method is not None:
        method(data)
        return
    _apply_remote_metadata(data)
    for meme in data["memes"]:
        if database.get_by_filename(meme["filename"]) is None:
            database.add_meme(
                meme["filename"],
                meme["sha256"],
                file_size=meme["file_size"],
                original_name=meme["name"],
            )


def _pull_with_commit(remove_local, cancelled=None):
    cfg = _runtime_config()
    remote_root = _remote_root(cfg)
    database = _runtime_db()
    assets = AssetPaths(cfg.data_dir, cfg.cache_dir)
    local_data = _load_runtime_manifest()
    remote_data = download_index()
    if remote_data is None:
        raise SyncError("no remote manifest available")
    try:
        projection = ManifestService().parse_data(remote_data, strict_hash=True)
    except ManifestValidationError as error:
        raise SyncError("远端 manifest 解析失败: %s" % error) from error
    files_total = len(projection.memes)
    bytes_total = sum(meme.file_size for meme in projection.memes)
    _reset_sync_state("download", files_total, bytes_total)
    _update_sync_state(status="downloading", start_time=time.time())
    backend = _get_backend()
    backend.connect()
    try:

        def download(meme, destination):
            _update_sync_state(current_file=meme.filename)
            path = remote_root.rstrip("/") + "/" + REMOTE_MEME_DIR + "/" + meme.filename
            return backend.download_file(path, destination)

        result = PullCommitService(
            assets,
            lambda manifest: _apply_pulled_metadata(manifest, database),
            lambda manifest: _runtime_write_manifest(manifest.to_data()),
            _runtime_build_manifest,
            lambda record: all(
                database.get_by_filename(entry["filename"])
                for entry in record["entries"]
            ),
        ).commit(projection, download, cancelled=cancelled)
    except PullCommitError as error:
        _update_sync_state(status="error", error=str(error))
        code, _, filename = str(error).partition(":")
        if code == "cancelled":
            results = {
                "downloaded": 0,
                "skipped": 0,
                "errors": 0,
                "removed_local": 0,
                "failed_files": [],
                "cancelled": True,
            }
            _update_sync_state(status="cancelled", results=results, failed_items=[])
            return results
        if code == "invalid_staged_asset":
            _update_sync_state(
                failed_items=[
                    {"filename": filename, "status": "error", "error": "图片校验失败"}
                ]
            )
            return {
                "downloaded": 0,
                "skipped": 0,
                "errors": 1,
                "removed_local": 0,
                "failed_files": [
                    {"filename": filename, "status": "error", "error": "图片校验失败"}
                ],
            }
        if code in {"download_failed", "empty_staged_asset"}:
            _update_sync_state(
                failed_items=[
                    {"filename": filename, "status": "error", "error": "下载失败"}
                ]
            )
            raise SyncError("下载失败") from error
        raise SyncError(str(error)) from error
    finally:
        backend.close()
    results = {
        "downloaded": files_total,
        "skipped": 0,
        "errors": 0,
        "removed_local": 0,
        "failed_files": [],
        "cleanup_pending": result["cleanup_pending"],
    }
    if remove_local:
        remote_filenames = {meme.filename for meme in projection.memes}
        for entry in local_data.get("memes", []):
            filename = entry.get("filename", "")
            if filename in remote_filenames:
                continue
            row = database.get_by_filename(filename)
            if row:
                database.delete_meme(row["id"])
            path = assets.cache_dir / filename
            if path.exists():
                path.unlink()
                results["removed_local"] += 1
            thumbnail = assets.thumbnail_dir / filename
            thumbnail.unlink(missing_ok=True)
    _update_sync_state(
        status="done",
        progress=100,
        files_done=files_total,
        bytes_done=bytes_total,
        results=results,
        failed_items=[],
    )
    return results


def sync_test() -> str:
    """测试当前配置的存储后端连接是否可用，返回 'ok' 或错误信息"""
    try:
        bk = _get_backend()
        bk.connect()
        bk.test_connection()
        bk.close()
        return "ok"
    except Exception as e:
        return str(e)


def _legacy_pull(remove_local: bool = None) -> dict:
    """远端 -> 本地：下载缺失/变更的表情包和清单（多线程）"""
    return _pull_with_commit(remove_local)

    # Legacy implementation retained below only for historical source context.
    cfg = get_config()
    if remove_local is None:
        remove_local = cfg.get("sync_remove_local", False)
    remote_root = _remote_root(cfg)
    cache_dir = cfg.cache_dir
    thumb_dir = cfg.thumbnail_dir
    max_workers = max(1, min(8, int(cfg.get("sync_threads", 3))))
    db = get_db()

    if not _sync_run_lock.acquire(blocking=False):
        raise SyncError("同步正在进行中")

    try:
        remote_data = download_index()
        if not remote_data:
            raise SyncError("no remote manifest available")

        remote_idx = {m["filename"]: m for m in remote_data.get("memes", [])}
        local_data = load_manifest()
        local_idx = {m["filename"]: m for m in local_data.get("memes", [])}

        files_total = len(remote_idx)
        bytes_total = 0
        for m in remote_data.get("memes", []):
            bytes_total += m.get("file_size", 0)

        _reset_sync_state("download", files_total, bytes_total)
        start = time.time()
        _update_sync_state(status="downloading", start_time=start)

        entries = list(remote_idx.items())
        if max_workers <= 1 or len(entries) <= 1:
            chunks = [entries]
        else:
            chunks = _chunk_list(entries, min(max_workers, len(entries)))

        aggregated = {
            "downloaded": 0,
            "skipped": 0,
            "errors": 0,
            "bytes": 0,
            "failed": [],
            "created_files": [],
            "created_meme_ids": [],
            "overwritten_files": [],
        }
        with ThreadPoolExecutor(max_workers=len(chunks)) as executor:
            runtime = _runtime()
            if runtime is not None:
                futures = [
                    executor.submit(
                        copy_context().run,
                        _pull_worker,
                        ch,
                        remote_root,
                        cache_dir,
                        db,
                    )
                    for ch in chunks
                ]
            else:
                futures = [
                    executor.submit(_pull_worker, ch, remote_root, cache_dir, db)
                    for ch in chunks
                ]
            for future in as_completed(futures):
                r = future.result()
                aggregated["downloaded"] += r["downloaded"]
                aggregated["skipped"] += r["skipped"]
                aggregated["errors"] += r["errors"]
                aggregated["bytes"] += r["bytes"]
                aggregated["failed"].extend(r.get("failed", []))
                aggregated["created_files"].extend(r.get("created_files", []))
                aggregated["created_meme_ids"].extend(r.get("created_meme_ids", []))
                aggregated["overwritten_files"].extend(r.get("overwritten_files", []))

        results = {
            "downloaded": aggregated["downloaded"],
            "skipped": aggregated["skipped"],
            "errors": aggregated["errors"],
            "removed_local": 0,
            "failed_files": aggregated["failed"],
        }

        if aggregated["errors"] > 0:
            _rollback_pull_changes(db, aggregated)
            _update_sync_state(failed_items=aggregated["failed"])
            msg = "%d 个文件下载失败，本地清单仅包含成功项" % aggregated["errors"]
            logger.warning("sync pull aborted: %s", msg)
            raise SyncError(msg)

        manifest_path = cfg.data_dir / INDEX_FILENAME
        manifest_backup = None
        if manifest_path.exists():
            fd, backup_name = tempfile.mkstemp(
                prefix=".manifest.pull-", suffix=".bak", dir=str(cfg.data_dir)
            )
            os.close(fd)
            manifest_backup = Path(backup_name)
            shutil.copyfile(manifest_path, manifest_backup)
        try:
            write_manifest(remote_data)
        except OSError:
            if manifest_backup is not None:
                shutil.copyfile(manifest_backup, manifest_path)
                manifest_backup.unlink()
            _rollback_pull_changes(db, aggregated)
            raise

        try:
            _apply_remote_metadata(remote_data)
        except Exception:
            if manifest_backup is not None:
                shutil.copyfile(manifest_backup, manifest_path)
                manifest_backup.unlink()
            _rollback_pull_changes(db, aggregated)
            raise
        if manifest_backup is not None:
            manifest_backup.unlink()

        if remove_local:
            for fname in list(local_idx.keys()):
                if fname not in remote_idx:
                    row = db.get_by_filename(fname)
                    if row:
                        db.delete_meme(row["id"])
                    local_path = cache_dir / fname
                    if local_path.exists():
                        try:
                            local_path.unlink()
                            results["removed_local"] += 1
                        except Exception:
                            pass
                    thumb_path = thumb_dir / fname
                    if thumb_path.exists():
                        try:
                            thumb_path.unlink()
                        except Exception:
                            pass

        _discard_pull_backups(aggregated)

        _update_sync_state(
            status="done",
            progress=100,
            files_done=files_total,
            bytes_done=bytes_total,
            results=results,
            failed_items=results["failed_files"],
        )
        logger.info("sync pull done: %s", results)
        return results
    except Exception as e:
        _update_sync_state(status="error", error=str(e))
        raise
    finally:
        _sync_run_lock.release()


def pull(remove_local: bool = None, cancelled=None) -> dict:
    """远端 -> 本地：严格校验、暂存并可恢复地提交。"""
    return _run_legacy_mutation(
        "sync.pull", None, lambda: _pull_impl(remove_local, cancelled)
    )


def _pull_impl(remove_local: bool = None, cancelled=None) -> dict:
    """远端 -> 本地：严格校验、暂存并可恢复地提交。"""
    cfg = _runtime_config()
    if remove_local is None:
        remove_local = cfg.get("sync_remove_local", False)
    runtime = _runtime()
    owns_sync_lock = runtime is None or runtime.legacy_lock
    if owns_sync_lock and not _sync_run_lock.acquire(blocking=False):
        raise SyncError("同步正在进行中")
    try:
        return _pull_with_commit(remove_local, cancelled)
    finally:
        if owns_sync_lock:
            _sync_run_lock.release()


def delete_all_remote() -> dict:
    """删除远端所有表情包和清单"""
    return _run_legacy_mutation("sync.delete_all", None, _delete_all_remote_impl)


def _delete_all_remote_impl() -> dict:
    """删除远端所有表情包和清单"""
    cfg = _runtime_config()
    remote_root = _remote_root(cfg)
    bk = _get_backend()
    bk.connect()
    try:
        remote_memes = _fetch_remote_memes(bk, remote_root)
        count = 0
        failed = 0
        for fname in remote_memes:
            rem_path = remote_root.rstrip("/") + "/" + REMOTE_MEME_DIR + "/" + fname
            try:
                if bk.delete_file(rem_path):
                    count += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
        rem_manifest = remote_root.rstrip("/") + "/" + REMOTE_INDEX
        try:
            if not bk.delete_file(rem_manifest):
                failed += 1
        except Exception:
            failed += 1
        result = {"ok": failed == 0, "deleted": count}
        if failed:
            result["failed"] = failed
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        bk.close()


def cleanup_remote_orphans(delete: bool = False) -> dict:
    """识别远端孤儿文件；delete=True 时物理删除，返回 {ok, orphans, removed}。"""
    return _run_legacy_mutation(
        "sync.cleanup", None, lambda: _cleanup_remote_orphans_impl(delete)
    )


def _cleanup_remote_orphans_impl(delete: bool = False) -> dict:
    """识别远端孤儿文件；delete=True 时物理删除，返回 {ok, orphans, removed}。"""
    cfg = _runtime_config()
    remote_root = _remote_root(cfg)
    bk = _get_backend()
    bk.connect()
    try:
        orphans = list_remote_orphans(bk, remote_root)
        removed = 0
        failed = 0
        if delete:
            for fname in orphans:
                rem_path = remote_root.rstrip("/") + "/" + REMOTE_MEME_DIR + "/" + fname
                if bk.delete_file(rem_path):
                    removed += 1
                else:
                    failed += 1
        result = {"ok": failed == 0, "orphans": orphans, "removed": removed}
        if failed:
            result["failed"] = failed
        return result
    except Exception as e:
        logger.warning("cleanup_remote_orphans failed: %s", e)
        return {"ok": False, "error": str(e)}
    finally:
        bk.close()


def cleanup_stale_temp_files() -> int:
    """清理中断遗留的临时文件（.remote-* / *.tmp，含 cache 目录），返回清理数量。"""
    cfg = get_config()
    count = 0
    for base in (cfg.data_dir, cfg.cache_dir):
        if not base.exists():
            continue
        for p in base.iterdir():
            if not p.is_file():
                continue
            name = p.name
            if name.startswith(".remote-") or name.endswith(".tmp"):
                try:
                    p.unlink()
                    count += 1
                except Exception:
                    pass
    return count
