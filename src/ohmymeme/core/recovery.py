"""跨根存储变更的可恢复协调事务。"""

import hashlib
import json
import os
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path

_MARKER_NAME = ".ohmymeme-storage-recovery.json"
_LOCK_NAME = ".ohmymeme-storage-recovery.lock"
_PROCESS_LOCK = threading.Lock()
_HELD_ROOTS = set()


class RecoveryLocked(RuntimeError):
    """另一个进程正在修改同一数据或缓存根。"""


class RecoveryUnavailable(RuntimeError):
    """恢复所需的根目录或状态不可安全访问。"""


class StorageRecovery:
    """为缓存迁移协调文件、持久元数据和 manifest。"""

    def __init__(self, data_dir, cache_root):
        self._data_dir = Path(data_dir).resolve()
        self._cache_root = Path(cache_root).resolve()
        self._record = None

    @property
    def marker_path(self):
        return self._data_dir / _MARKER_NAME

    @contextmanager
    def locked(self, *roots):
        handles = []
        claimed = []
        try:
            ordered_roots = sorted(
                {
                    self._data_dir,
                    self._cache_root,
                    *(Path(root).resolve() for root in roots),
                },
                key=str,
            )
            with _PROCESS_LOCK:
                for root in ordered_roots:
                    if root in _HELD_ROOTS:
                        raise RecoveryLocked(str(root))
                    _HELD_ROOTS.add(root)
                    claimed.append(root)
            for root in ordered_roots:
                root.mkdir(parents=True, exist_ok=True)
                handle = open(root / _LOCK_NAME, "a+b")
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
                handle.seek(0)
                try:
                    self._lock_file(handle)
                except OSError as error:
                    handle.close()
                    raise RecoveryLocked(str(root)) from error
                handles.append(handle)
            yield
        finally:
            for handle in reversed(handles):
                self._unlock_file(handle)
                handle.close()
            with _PROCESS_LOCK:
                for root in claimed:
                    _HELD_ROOTS.remove(root)

    def begin(self, destination, move_files):
        target_root = Path(destination).resolve()
        self._record = {
            "version": 1,
            "phase": "prepared",
            "source_root": str(self._cache_root),
            "target_root": str(target_root),
            "move_files": bool(move_files),
            "entries": [],
        }
        self._write_record()
        if not move_files:
            return
        try:
            self._record["entries"] = self._plan_entries(self._cache_root, target_root)
        except OSError as error:
            self._record["phase"] = "collision"
            self._record["collision"] = str(error)
            self._write_record()
            raise RecoveryUnavailable("storage target collision") from error
        self._write_record()

    def copy_files(self):
        record = self._require_record()
        source_root, target_root = self._roots(record)
        target_root.mkdir(parents=True, exist_ok=True)
        for entry in record["entries"]:
            source = source_root / entry["relative"]
            destination = target_root / entry["relative"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            if entry["strategy"] == "replace":
                os.replace(source, destination)
            else:
                self._copy_and_verify(source, destination, entry["sha256"])
            entry["complete"] = True
            self._write_record()
        record["phase"] = "files_copied"
        self._write_record()

    def migrate(self, destination, move_files, commit_database, write_manifest):
        with self.locked(destination):
            self.begin(destination, move_files)
            try:
                self.copy_files()
                self._require_record()["phase"] = "metadata_committing"
                self._write_record()
                commit_database()
            except OSError:
                self._rollback_before_database()
                raise
            self._require_record()["phase"] = "db_committed"
            self._write_record()
            self._forward_files()
            write_manifest()
            moved = len(self._require_record()["entries"])
            self._clear_record()
            return moved

    def recover_before_database(self, metadata_target=None):
        if not self.marker_path.exists():
            return False
        record = self._read_record()
        source_root, target_root = self._roots(record)
        self._ensure_roots_available(source_root, target_root)
        with self.locked(source_root, target_root):
            self._record = record
            phase = record["phase"]
            if phase in ("prepared", "files_copied"):
                self._rollback_before_database()
                return True
            if phase == "metadata_committing":
                target_persisted = (
                    metadata_target is not None
                    and Path(metadata_target).resolve() == target_root
                )
                if target_persisted:
                    self._forward_files()
                    record["phase"] = "db_committed"
                    self._write_record()
                    return True
                self._rollback_before_database()
                return True
            if phase == "db_committed":
                self._forward_files()
                return True
            raise RecoveryUnavailable("unknown recovery phase")

    def finish_manifest(self, write_manifest):
        if not self.marker_path.exists():
            return False
        record = self._read_record()
        source_root, target_root = self._roots(record)
        self._ensure_roots_available(source_root, target_root)
        with self.locked(source_root, target_root):
            self._record = record
            if record["phase"] != "db_committed":
                raise RecoveryUnavailable("manifest requested before database commit")
            write_manifest()
            self._clear_record()
            return True

    def _plan_entries(self, source_root, target_root):
        entries = []
        for source in source_root.rglob("*"):
            if (
                not source.is_file()
                or _LOCK_NAME in source.name
                or _MARKER_NAME in source.name
            ):
                continue
            relative = source.relative_to(source_root)
            destination = target_root / relative
            if destination.exists():
                raise OSError(f"storage destination exists: {relative}")
            strategy = "replace" if self._same_volume(source, destination) else "copy"
            entries.append(
                {
                    "relative": str(relative),
                    "sha256": self._hash(source),
                    "strategy": strategy,
                    "complete": False,
                }
            )
        return entries

    def _rollback_before_database(self):
        record = self._require_record()
        source_root, target_root = self._roots(record)
        for entry in reversed(record["entries"]):
            source = source_root / entry["relative"]
            destination = target_root / entry["relative"]
            if entry["strategy"] == "replace":
                if destination.exists() and not source.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(destination, source)
            elif destination.exists():
                destination.unlink()
        self._clear_record()

    def _forward_files(self):
        record = self._require_record()
        source_root, target_root = self._roots(record)
        for entry in record["entries"]:
            source = source_root / entry["relative"]
            destination = target_root / entry["relative"]
            if entry["strategy"] == "replace":
                if destination.exists() and not source.exists():
                    continue
                if source.exists() and not destination.exists():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(source, destination)
                    continue
                raise RecoveryUnavailable(
                    f"ambiguous same-volume state: {entry['relative']}"
                )
            if not destination.exists():
                if not source.exists():
                    raise RecoveryUnavailable(
                        f"missing cross-root data: {entry['relative']}"
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                self._copy_and_verify(source, destination, entry["sha256"])
            if self._hash(destination) != entry["sha256"]:
                raise RecoveryUnavailable(f"hash mismatch: {entry['relative']}")
            if source.exists():
                source.unlink()

    def _copy_and_verify(self, source, destination, expected_hash):
        temporary = destination.with_name(destination.name + ".recovery.tmp")
        try:
            with source.open("rb") as input_file, temporary.open("xb") as output_file:
                shutil.copyfileobj(input_file, output_file)
                output_file.flush()
                os.fsync(output_file.fileno())
            if self._hash(temporary) != expected_hash:
                raise OSError(f"copy hash mismatch: {source.name}")
            os.replace(temporary, destination)
        except OSError:
            if temporary.exists():
                temporary.unlink()
            raise

    def _read_record(self):
        try:
            record = json.loads(self.marker_path.read_text(encoding="utf-8"))
            if record["version"] != 1 or not isinstance(record["entries"], list):
                raise KeyError
            return record
        except (json.JSONDecodeError, KeyError, OSError, TypeError) as error:
            raise RecoveryUnavailable("invalid recovery marker") from error

    def _write_record(self):
        record = self._require_record()
        source_root, target_root = self._roots(record)
        markers = (
            self.marker_path,
            source_root / _MARKER_NAME,
            target_root / _MARKER_NAME,
        )
        for marker in markers:
            marker.parent.mkdir(parents=True, exist_ok=True)
            temporary = marker.with_name(marker.name + ".tmp")
            with temporary.open("w", encoding="utf-8") as output:
                json.dump(record, output, ensure_ascii=False, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, marker)

    def _clear_record(self):
        record = self._require_record()
        source_root, target_root = self._roots(record)
        markers = (
            self.marker_path,
            source_root / _MARKER_NAME,
            target_root / _MARKER_NAME,
        )
        for marker in markers:
            if marker.exists():
                marker.unlink()
        self._record = None

    def _roots(self, record):
        try:
            source_root = Path(record["source_root"]).resolve()
            target_root = Path(record["target_root"]).resolve()
            if not source_root.is_absolute() or not target_root.is_absolute():
                raise ValueError
            return source_root, target_root
        except (KeyError, TypeError, ValueError) as error:
            raise RecoveryUnavailable("invalid recovery roots") from error

    def _ensure_roots_available(self, source_root, target_root):
        if not source_root.exists() or not target_root.exists():
            raise RecoveryUnavailable("cache root unavailable")

    def _require_record(self):
        if self._record is None:
            raise RecoveryUnavailable("recovery transaction not started")
        return self._record

    @staticmethod
    def _hash(path):
        digest = hashlib.sha256()
        with path.open("rb") as input_file:
            for chunk in iter(lambda: input_file.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _same_volume(source, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        return os.stat(source.parent).st_dev == os.stat(destination.parent).st_dev

    @staticmethod
    def _lock_file(handle):
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_file(handle):
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
