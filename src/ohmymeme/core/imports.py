# pyright: basic

"""图片导入应用服务。"""

import base64
import binascii
import hashlib
import io
import json
import logging
import lzma
import os
import sqlite3
import struct
import threading
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from PIL import Image

from .adapters.filesystem.atomic_repository import AtomicFileRepository
from .assets import AssetPaths, is_safe_filename
from .config import _IMPORT_MAX_BYTES, _IMPORT_MAX_PX

logger = logging.getLogger(__name__)


class MemeRepository(Protocol):
    def get_by_hash(self, file_hash: str) -> dict | None: ...

    def add_meme(
        self,
        filename: str,
        file_hash: str,
        width: int,
        height: int,
        file_size: int,
        mime_type: str,
        original_name: str,
        stego_of_hash: str | None = None,
        from_stego: int = 0,
    ) -> int: ...

    def delete_meme(self, meme_id: int) -> None: ...


class _MutationLease(Protocol):
    def commit(self) -> None: ...


class _MutationCoordinator(Protocol):
    def mutation(self, entrypoint: str) -> AbstractContextManager[_MutationLease]: ...


@dataclass(frozen=True, slots=True)
class ImportBytes:
    data: bytes
    original_name: str


@dataclass(frozen=True, slots=True)
class ImportPath:
    path: Path
    original_name: str


ImportRequest = ImportBytes | ImportPath


@dataclass(frozen=True, slots=True)
class ImportResult:
    imported_ids: tuple[int, ...]
    rejected: int


@dataclass(frozen=True, slots=True)
class _ValidatedImage:
    data: bytes
    extension: str
    width: int
    height: int
    file_hash: str
    original_name: str
    from_stego: int


_MAGIC_EXTENSIONS = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
)


def _magic_extension(data: bytes) -> str:
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    for magic, extension in _MAGIC_EXTENSIONS:
        if data.startswith(magic):
            return extension
    return ""


def _decode_stego(data: bytes) -> bytes | None:
    """在内存中还原 STG3，拒绝所有坏载荷。"""
    try:
        from .gif_stego import decode_bytes

        restored, _extension = decode_bytes(data)
    except (IndexError, OSError, ValueError, lzma.LZMAError, struct.error):
        return None
    return restored


class ImageImportService:
    """原子接收图片并同步缓存、数据库和 manifest。"""

    def __init__(
        self,
        db: MemeRepository,
        assets: AssetPaths,
        build_manifest: Callable[[], None],
        stego_decoder: Callable[[bytes], bytes | None] | None = None,
        mutation_coordinator: _MutationCoordinator | None = None,
    ) -> None:
        self._db = db
        self._assets = assets
        self._cache_dir = assets.cache_dir
        self._files = AtomicFileRepository(assets)
        self._build_manifest = build_manifest
        self._stego_decoder = stego_decoder or _decode_stego
        self._lock = _IMPORT_LOCK
        self._mutation_coordinator = mutation_coordinator

    def import_path(self, request: ImportPath) -> ImportResult:
        return self.import_batch((request,))

    def import_bytes(self, request: ImportBytes) -> ImportResult:
        return self.import_batch((request,))

    def register_existing_path(self, request: ImportPath) -> ImportResult:
        if self._mutation_coordinator is not None:
            with self._mutation_coordinator.mutation("library.import.scan") as lease:
                result = self._register_existing_path_locked(request)
                if result.imported_ids:
                    lease.commit()
                return result
        with self._lock:
            return self._register_existing_path_locked(request)

    def _register_existing_path_locked(self, request: ImportPath) -> ImportResult:
        with self._lock:
            validated = self._validate_path(request.path, request.original_name)
            if validated is None:
                return ImportResult((), 1)
            result = self._import_batch_locked((request,))
            destination = self._cache_dir / (
                f"{validated.file_hash[:16]}{validated.extension}"
            )
            if (
                result.imported_ids
                and request.path != destination
                and request.path.parent.resolve() == self._cache_dir.resolve()
            ):
                request.path.unlink()
            return result

    def import_batch(
        self,
        requests: Sequence[ImportRequest],
        cancelled: Callable[[], bool] | None = None,
    ) -> ImportResult:
        if self._mutation_coordinator is not None:
            with self._mutation_coordinator.mutation("library.import") as lease:
                result = self._import_batch_locked(requests, cancelled)
                if result.imported_ids:
                    lease.commit()
                return result
        with self._lock:
            return self._import_batch_locked(requests, cancelled)

    def _import_batch_locked(
        self,
        requests: Sequence[ImportRequest],
        cancelled: Callable[[], bool] | None = None,
    ) -> ImportResult:
        rejected = 0
        manifest_path = self._assets.manifest_path
        manifest_snapshot = (
            manifest_path.read_bytes() if manifest_path.exists() else None
        )
        validated_requests = []
        seen_hashes = set()
        for request in requests:
            validated = self._validate(request)
            if validated is None:
                rejected += 1
                continue
            if (
                validated.file_hash not in seen_hashes
                and self._db.get_by_hash(validated.file_hash) is None
            ):
                validated_requests.append(validated)
                seen_hashes.add(validated.file_hash)
        if cancelled is not None and cancelled():
            return ImportResult((), rejected)
        if not validated_requests:
            return ImportResult((), rejected)
        record = self._new_journal(validated_requests, manifest_snapshot)
        self._write_import_journal(record)
        record["phase"] = "staged"
        self._write_import_journal(record)
        imported_ids = []
        try:
            record["phase"] = "files_committing"
            self._write_import_journal(record)
            for entry, validated in zip(record["entries"], validated_requests):
                asset = self._files.commit_bytes(validated.data, validated.extension)
                entry["created"] = asset.created
                entry["state"] = "replaced" if asset.created else "preserved"
                self._write_import_journal(record)
            record["phase"] = "files_committed"
            self._write_import_journal(record)
            record["phase"] = "metadata_committing"
            self._write_import_journal(record)
            for entry, validated in zip(record["entries"], validated_requests):
                meme_id = self._db.add_meme(
                    filename=entry["filename"],
                    file_hash=validated.file_hash,
                    width=validated.width,
                    height=validated.height,
                    file_size=len(validated.data),
                    mime_type=f"image/{validated.extension[1:]}",
                    original_name=validated.original_name,
                    from_stego=validated.from_stego,
                )
                entry["meme_id"] = meme_id
                imported_ids.append(meme_id)
                self._write_import_journal(record)
            record["phase"] = "db_committed"
            self._write_import_journal(record)
            record["phase"] = "manifest_committing"
            self._write_import_journal(record)
            self._build_manifest()
            record["phase"] = "manifest_committed"
            self._write_import_journal(record)
            self._clear_import_journal()
        except (OSError, RuntimeError, sqlite3.Error):
            record["phase"] = "rollback"
            self._write_import_journal(record)
            cleanup_failures = self._rollback_record(record)
            self._restore_manifest(manifest_snapshot, cleanup_failures)
            if not cleanup_failures:
                self._clear_import_journal()
            raise
        return ImportResult(tuple(imported_ids), rejected)

    def _new_journal(
        self,
        validated_requests: Sequence[_ValidatedImage],
        manifest_snapshot: bytes | None,
    ) -> dict:
        entries = []
        for validated in validated_requests:
            filename = f"{validated.file_hash[:16]}{validated.extension}"
            entries.append(
                {
                    "created": False,
                    "file_hash": validated.file_hash,
                    "filename": filename,
                    "had_target": (self._cache_dir / filename).exists(),
                    "meme_id": None,
                    "state": "pending",
                }
            )
        return {
            "cache_dir": str(self._cache_dir.resolve()),
            "data_dir": str(self._assets.data_dir.resolve()),
            "entries": entries,
            "manifest_snapshot": (
                None
                if manifest_snapshot is None
                else base64.b64encode(manifest_snapshot).decode("ascii")
            ),
            "phase": "staging",
            "version": 2,
        }

    def _validate(self, request: ImportRequest) -> _ValidatedImage | None:
        match request:
            case ImportBytes(data=data, original_name=original_name):
                return self._validate_stego(data, original_name)
            case ImportPath(path=path, original_name=original_name):
                return self._validate_path(path, original_name)

    def _validate_path(self, path: Path, original_name: str) -> _ValidatedImage | None:
        try:
            data = path.read_bytes()
        except OSError:
            return None
        return self._validate_stego(data, original_name)

    def _validate_stego(
        self, data: bytes, original_name: str
    ) -> _ValidatedImage | None:
        validated = self._validate_data(data, original_name)
        if validated is None:
            return None
        if validated.extension != ".gif" or b"STG3" not in data:
            return validated
        try:
            restored = self._stego_decoder(data)
        except (OSError, ValueError, Image.DecompressionBombError):
            return None
        if restored is None:
            return None
        restored_validated = self._validate_data(restored, original_name)
        if restored_validated is None:
            return None
        return _ValidatedImage(
            restored_validated.data,
            restored_validated.extension,
            restored_validated.width,
            restored_validated.height,
            restored_validated.file_hash,
            restored_validated.original_name,
            1,
        )

    def _validate_data(self, data: bytes, original_name: str) -> _ValidatedImage | None:
        extension = _magic_extension(data)
        if not extension or len(data) > _IMPORT_MAX_BYTES:
            return None
        try:
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
        except (OSError, SyntaxError, Image.DecompressionBombError):
            return None
        if width <= 0 or height <= 0 or max(width, height) > _IMPORT_MAX_PX:
            return None
        return _ValidatedImage(
            data,
            extension,
            width,
            height,
            hashlib.sha256(data).hexdigest(),
            Path(original_name).stem,
            0,
        )

    def _compensate(
        self, created_ids: Sequence[int], created_paths: Sequence[Path]
    ) -> list[str]:
        failures = []
        for meme_id in reversed(created_ids):
            try:
                self._db.delete_meme(meme_id)
            except (OSError, RuntimeError, sqlite3.Error) as error:
                failures.append(f"delete_meme:{meme_id}:{error}")
        for path in reversed(created_paths):
            try:
                path.unlink(missing_ok=True)
            except OSError as error:
                failures.append(f"unlink:{path.name}:{error}")
        return failures

    def _restore_manifest(self, snapshot: bytes | None, failures: list[str]) -> None:
        manifest_path = self._assets.manifest_path
        try:
            if snapshot is None:
                manifest_path.unlink(missing_ok=True)
            else:
                temporary = manifest_path.with_suffix(".restore.tmp")
                temporary.write_bytes(snapshot)
                os.replace(temporary, manifest_path)
        except OSError as error:
            failures.append(f"manifest_restore:{error}")

    def recover(self) -> bool:
        """在暴露媒体消费者前回滚未完成的导入事务。"""
        marker = self._assets.recovery_marker_path
        if not marker.exists():
            return False
        try:
            marker_text = marker.read_text(encoding="utf-8")
        except OSError as error:
            raise RuntimeError("invalid_import_recovery_marker") from error
        try:
            record = json.loads(marker_text)
        except json.JSONDecodeError as error:
            if (
                not marker_text.startswith("manifest_restore:")
                or not marker_text[len("manifest_restore:") :].strip()
            ):
                raise RuntimeError("invalid_import_recovery_marker") from error
            self._build_manifest()
            marker.unlink()
            return True
        if isinstance(record, dict) and record.get("version") == 2:
            return self._recover_journal(record)
        if not isinstance(record, dict) or record.get("version") != 1:
            raise RuntimeError("invalid_import_recovery_marker")
        if record.get("data_dir") != str(self._assets.data_dir.resolve()):
            raise RuntimeError("invalid_import_recovery_marker")
        if record.get("cache_dir") != str(self._cache_dir.resolve()):
            raise RuntimeError("invalid_import_recovery_marker")
        phase = record.get("phase")
        if phase == "forward":
            self._build_manifest()
            marker.unlink()
            return True
        if phase != "rollback":
            raise RuntimeError("invalid_import_recovery_marker")
        meme_ids = record.get("meme_ids")
        filenames = record.get("filenames")
        snapshot = record.get("manifest_snapshot")
        if (
            not isinstance(meme_ids, list)
            or not isinstance(filenames, list)
            or snapshot is not None
            and not isinstance(snapshot, str)
            or any(type(meme_id) is not int or meme_id < 1 for meme_id in meme_ids)
            or any(
                not isinstance(filename, str) or not is_safe_filename(filename)
                for filename in filenames
            )
        ):
            raise RuntimeError("invalid_import_recovery_marker")
        try:
            manifest_snapshot = (
                None if snapshot is None else base64.b64decode(snapshot, validate=True)
            )
        except (ValueError, binascii.Error) as error:
            raise RuntimeError("invalid_import_recovery_marker") from error
        for meme_id in reversed(meme_ids):
            self._db.delete_meme(meme_id)
        for filename in reversed(filenames):
            path = self._cache_dir / filename
            if path.is_symlink() or not path.resolve().is_relative_to(
                self._cache_dir.resolve()
            ):
                raise RuntimeError("invalid_import_recovery_marker")
            path.unlink(missing_ok=True)
        self._restore_manifest_or_raise(manifest_snapshot)
        marker.unlink()
        return True

    def _recover_journal(self, record: dict) -> bool:
        manifest_snapshot = self._validate_journal(record)
        phase = record["phase"]
        if phase in {
            "db_committed",
            "manifest_committing",
            "manifest_committed",
            "cleanup_pending",
        }:
            self._build_manifest()
            self._clear_import_journal()
            return True
        failures = self._rollback_record(record)
        self._restore_manifest_or_raise(manifest_snapshot)
        if failures:
            raise RuntimeError("import_recovery_failed")
        self._clear_import_journal()
        return True

    def _validate_journal(self, record: dict) -> bytes | None:
        if set(record) != {
            "cache_dir",
            "data_dir",
            "entries",
            "manifest_snapshot",
            "phase",
            "version",
        }:
            raise RuntimeError("invalid_import_recovery_marker")
        if type(record["version"]) is not int or record["version"] != 2:
            raise RuntimeError("invalid_import_recovery_marker")
        if record["data_dir"] != str(self._assets.data_dir.resolve()):
            raise RuntimeError("invalid_import_recovery_marker")
        if record["cache_dir"] != str(self._cache_dir.resolve()):
            raise RuntimeError("invalid_import_recovery_marker")
        if not isinstance(record["phase"], str) or record["phase"] not in {
            "staging",
            "staged",
            "files_committing",
            "files_committed",
            "metadata_committing",
            "db_committed",
            "manifest_committing",
            "manifest_committed",
            "rollback",
            "cleanup_pending",
        }:
            raise RuntimeError("invalid_import_recovery_marker")
        if not isinstance(record["entries"], list) or not record["entries"]:
            raise RuntimeError("invalid_import_recovery_marker")
        snapshot = record["manifest_snapshot"]
        if snapshot is not None and not isinstance(snapshot, str):
            raise RuntimeError("invalid_import_recovery_marker")
        for entry in record["entries"]:
            if not isinstance(entry, dict) or set(entry) != {
                "created",
                "file_hash",
                "filename",
                "had_target",
                "meme_id",
                "state",
            }:
                raise RuntimeError("invalid_import_recovery_marker")
            file_hash = entry["file_hash"]
            if (
                not isinstance(file_hash, str)
                or len(file_hash) != 64
                or any(character not in "0123456789abcdef" for character in file_hash)
            ):
                raise RuntimeError("invalid_import_recovery_marker")
            filename = entry["filename"]
            if (
                not isinstance(filename, str)
                or not is_safe_filename(filename)
                or not filename.startswith(file_hash[:16])
            ):
                raise RuntimeError("invalid_import_recovery_marker")
            if (
                type(entry["created"]) is not bool
                or type(entry["had_target"]) is not bool
            ):
                raise RuntimeError("invalid_import_recovery_marker")
            if not isinstance(entry["state"], str) or entry["state"] not in {
                "pending",
                "preserved",
                "replaced",
            }:
                raise RuntimeError("invalid_import_recovery_marker")
            meme_id = entry["meme_id"]
            if meme_id is not None and (type(meme_id) is not int or meme_id < 1):
                raise RuntimeError("invalid_import_recovery_marker")
        if snapshot is None:
            return None
        try:
            return base64.b64decode(snapshot, validate=True)
        except (ValueError, binascii.Error) as error:
            raise RuntimeError("invalid_import_recovery_marker") from error

    def _rollback_record(self, record: dict) -> list[str]:
        failures = []
        for entry in reversed(record["entries"]):
            try:
                row = self._db.get_by_hash(entry["file_hash"])
                if row is not None:
                    self._db.delete_meme(row["id"])
            except (KeyError, OSError, RuntimeError, sqlite3.Error) as error:
                failures.append(f"delete_meme:{entry['filename']}:{error}")
            if entry["had_target"]:
                continue
            try:
                path = self._cache_dir / entry["filename"]
                if path.is_symlink() or not path.resolve().is_relative_to(
                    self._cache_dir.resolve()
                ):
                    raise RuntimeError("invalid_import_recovery_marker")
                path.unlink(missing_ok=True)
            except (OSError, RuntimeError) as error:
                failures.append(f"unlink:{entry['filename']}:{error}")
        for temporary in self._cache_dir.glob(".atomic-*.tmp"):
            try:
                temporary.unlink(missing_ok=True)
            except OSError as error:
                failures.append(f"unlink:{temporary.name}:{error}")
        return failures

    def _restore_manifest_or_raise(self, snapshot: bytes | None) -> None:
        manifest_path = self._assets.manifest_path
        if snapshot is None:
            manifest_path.unlink(missing_ok=True)
            return
        temporary = manifest_path.with_suffix(".restore.tmp")
        try:
            temporary.write_bytes(snapshot)
            os.replace(temporary, manifest_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _write_recovery_marker(
        self,
        created_ids: Sequence[int],
        created_paths: Sequence[Path],
        manifest_snapshot: bytes | None,
        failures: Sequence[str],
    ) -> Path:
        marker = self._assets.recovery_marker_path
        record = {
            "version": 1,
            "phase": "rollback",
            "data_dir": str(self._assets.data_dir.resolve()),
            "cache_dir": str(self._cache_dir.resolve()),
            "meme_ids": list(created_ids),
            "filenames": [path.name for path in created_paths],
            "manifest_snapshot": (
                None
                if manifest_snapshot is None
                else base64.b64encode(manifest_snapshot).decode("ascii")
            ),
            "failures": list(failures),
        }
        temporary = marker.with_suffix(".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as output:
                json.dump(record, output, ensure_ascii=False, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, marker)
        finally:
            temporary.unlink(missing_ok=True)
        return marker

    def _write_import_journal(self, record: dict) -> None:
        marker = self._assets.recovery_marker_path
        marker.parent.mkdir(parents=True, exist_ok=True)
        temporary = marker.with_suffix(".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as output:
                json.dump(record, output, ensure_ascii=False, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, marker)
        finally:
            temporary.unlink(missing_ok=True)

    def _clear_import_journal(self) -> None:
        self._assets.recovery_marker_path.unlink(missing_ok=True)


class HostImportSink:
    """向来源提供者暴露的宿主图片导入端口。"""

    def __init__(self, service: ImageImportService) -> None:
        self._service = service

    def import_path(
        self, request: ImportPath, cancelled: Callable[[], bool] | None = None
    ) -> ImportResult:
        return self._service.import_batch((request,), cancelled)

    def import_bytes(
        self, request: ImportBytes, cancelled: Callable[[], bool] | None = None
    ) -> ImportResult:
        return self._service.import_batch((request,), cancelled)

    def import_batch(
        self,
        requests: Sequence[ImportRequest],
        cancelled: Callable[[], bool] | None = None,
    ) -> ImportResult:
        return self._service.import_batch(requests, cancelled)


_IMPORT_LOCK = threading.RLock()
