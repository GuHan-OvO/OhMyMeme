"""Durable local pull commit coordination."""

import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from ohmymeme.core.assets import is_safe_filename
from ohmymeme.core.config import _IMPORT_MAX_BYTES, _IMPORT_MAX_PX
from ohmymeme.core.recovery import StorageRecovery

_JOURNAL_NAME = ".ohmymeme-pull-recovery.json"


class PullCommitError(RuntimeError):
    pass


class PullCommitService:
    """Stage media before explicitly committing metadata and manifest state."""

    def __init__(
        self,
        assets,
        apply_metadata,
        write_manifest,
        rebuild_manifest,
        metadata_committed=None,
    ):
        self._assets = assets
        self._apply_metadata = apply_metadata
        self._write_manifest = write_manifest
        self._rebuild_manifest = rebuild_manifest
        self._metadata_committed = metadata_committed or (lambda _record: False)
        self._storage_recovery = StorageRecovery(assets.data_dir, assets.cache_dir)

    @property
    def journal_path(self):
        return self._assets.data_dir / _JOURNAL_NAME

    def commit(self, projection, download, cancelled=None):
        if self.journal_path.exists():
            self.recover()
        with self._storage_recovery.locked(self._assets.cache_dir):
            entries = self._stage(projection, download)
            if cancelled is not None and cancelled():
                self._cleanup({"entries": entries})
                raise PullCommitError("cancelled")
            record = {
                "version": 1,
                "cache_root": str(self._assets.cache_dir.resolve()),
                "phase": "staged",
                "entries": entries,
            }
            self._write_journal(record)
            try:
                self._replace_staged(record)
            except (OSError, sqlite3.Error) as error:
                self._restore_files(record)
                self._clear_journal()
                raise PullCommitError("file_replace_failed") from error
            record["phase"] = "files_replaced"
            self._write_journal(record)
            record["phase"] = "metadata_committing"
            self._write_journal(record)
            try:
                self._apply_metadata(projection)
            except (OSError, sqlite3.Error) as error:
                self._restore_files(record)
                record["phase"] = "needs_recovery"
                self._write_journal(record)
                raise PullCommitError("metadata_failed") from error
            except RuntimeError:
                self._restore_files(record)
                record["phase"] = "needs_recovery"
                self._write_journal(record)
                raise
            record["phase"] = "db_committed"
            self._write_journal(record)
            try:
                self._write_manifest(projection)
            except OSError as error:
                raise PullCommitError("manifest_failed") from error
            record["phase"] = "manifest_committed"
            self._write_journal(record)
            try:
                self._cleanup(record)
            except OSError:
                record["phase"] = "cleanup_pending"
                self._write_journal(record)
                return {"ok": True, "cleanup_pending": True}
            self._clear_journal()
            return {"ok": True, "cleanup_pending": False}

    def recover(self):
        if not self.journal_path.exists():
            return False
        with self._storage_recovery.locked(self._assets.cache_dir):
            record = self._read_journal()
            phase = record["phase"]
            if phase == "staged":
                self._cleanup(record)
                self._clear_journal()
                return True
            if phase in ("files_replaced", "needs_recovery"):
                self._restore_files(record)
                self._cleanup(record)
                self._clear_journal()
                return True
            if phase == "metadata_committing":
                if self._metadata_committed(record):
                    self._rebuild_manifest()
                else:
                    self._restore_files(record)
                self._cleanup(record)
                self._clear_journal()
                return True
            if phase in ("db_committed", "manifest_committed", "cleanup_pending"):
                self._rebuild_manifest()
                self._cleanup(record)
                self._clear_journal()
                return True
            raise PullCommitError("invalid_journal")

    def _stage(self, projection, download):
        self._assets.cache_dir.mkdir(parents=True, exist_ok=True)
        entries = []
        try:
            for meme in projection.memes:
                descriptor, name = tempfile.mkstemp(
                    prefix=".pull-stage-", suffix=".tmp", dir=self._assets.cache_dir
                )
                os.close(descriptor)
                stage = Path(name)
                if not download(meme, stage):
                    stage.unlink(missing_ok=True)
                    raise PullCommitError(f"download_failed:{meme.filename}")
                self._validate_stage(stage, meme)
                entries.append(
                    {
                        "filename": meme.filename,
                        "stage": stage.name,
                        "backup": "",
                        "state": "pending",
                        "had_original": (
                            self._assets.cache_dir / meme.filename
                        ).exists(),
                    }
                )
        except (OSError, PullCommitError):
            for entry in entries:
                (self._assets.cache_dir / entry["stage"]).unlink(missing_ok=True)
            raise
        return entries

    def _replace_staged(self, record):
        for entry in record["entries"]:
            target = self._assets.cache_dir / entry["filename"]
            stage = self._assets.cache_dir / entry["stage"]
            if entry["had_original"]:
                descriptor, name = tempfile.mkstemp(
                    prefix=f".{entry['filename']}.pull-",
                    suffix=".bak",
                    dir=self._assets.cache_dir,
                )
                os.close(descriptor)
                backup = Path(name)
                backup.unlink()
                entry["backup"] = backup.name
                self._write_journal(record)
                os.replace(target, backup)
                entry["state"] = "backed_up"
                self._write_journal(record)
            os.replace(stage, target)
            entry["state"] = "replaced"
            self._write_journal(record)

    def _restore_files(self, record):
        for entry in reversed(record["entries"]):
            target = self._owned_path(entry["filename"], "")
            backup_name = entry["backup"]
            backup = self._owned_path(backup_name, ".") if backup_name else None
            if backup is not None and backup.exists():
                target.unlink(missing_ok=True)
                os.replace(backup, target)
            elif not entry["had_original"]:
                target.unlink(missing_ok=True)

    def _cleanup(self, record):
        for entry in record["entries"]:
            self._owned_path(entry["stage"], ".pull-stage-").unlink(missing_ok=True)
            backup = entry["backup"]
            if backup:
                self._owned_path(backup, ".").unlink(missing_ok=True)

    def _validate_stage(self, stage, meme):
        if stage.stat().st_size == 0:
            stage.unlink(missing_ok=True)
            raise PullCommitError(f"empty_staged_asset:{meme.filename}")
        if stage.stat().st_size > _IMPORT_MAX_BYTES:
            stage.unlink(missing_ok=True)
            raise PullCommitError("invalid_staged_asset")
        digest = self._hash(stage)
        if digest != meme.sha256:
            stage.unlink(missing_ok=True)
            raise PullCommitError("invalid_staged_asset")
        try:
            with Image.open(stage) as image:
                image.verify()
            with Image.open(stage) as image:
                width, height = image.size
        except (OSError, SyntaxError, UnidentifiedImageError) as error:
            stage.unlink(missing_ok=True)
            raise PullCommitError("invalid_staged_asset") from error
        if max(width, height) > _IMPORT_MAX_PX:
            stage.unlink(missing_ok=True)
            raise PullCommitError("invalid_staged_asset")

    def _read_journal(self):
        try:
            record = json.loads(self.journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise PullCommitError("invalid_journal") from error
        if not isinstance(record, dict) or record.get("version") != 1:
            raise PullCommitError("invalid_journal")
        if record.get("cache_root") != str(self._assets.cache_dir.resolve()):
            raise PullCommitError("invalid_journal")
        if not isinstance(record.get("phase"), str) or not isinstance(
            record.get("entries"), list
        ):
            raise PullCommitError("invalid_journal")
        for entry in record["entries"]:
            if not isinstance(entry, dict) or not is_safe_filename(
                entry.get("filename", "")
            ):
                raise PullCommitError("invalid_journal")
            for field in ("stage", "backup"):
                value = entry.get(field, "")
                if not isinstance(value, str) or (value and Path(value).name != value):
                    raise PullCommitError("invalid_journal")
            if not entry["stage"].startswith(".pull-stage-"):
                raise PullCommitError("invalid_journal")
            if entry["backup"] and not entry["backup"].startswith(
                f".{entry['filename']}.pull-"
            ):
                raise PullCommitError("invalid_journal")
            if entry.get("state") not in {"pending", "backed_up", "replaced"}:
                raise PullCommitError("invalid_journal")
        return record

    def _owned_path(self, name, prefix):
        if (
            not name
            or Path(name).name != name
            or (prefix and not name.startswith(prefix))
        ):
            raise PullCommitError("invalid_journal")
        path = self._assets.cache_dir / name
        if path.is_symlink() or not path.resolve().is_relative_to(
            self._assets.cache_dir.resolve()
        ):
            raise PullCommitError("invalid_journal")
        return path

    def _write_journal(self, record):
        self._assets.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.journal_path.with_name(self.journal_path.name + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as output:
                json.dump(record, output, ensure_ascii=False, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.journal_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _clear_journal(self):
        self.journal_path.unlink(missing_ok=True)

    @staticmethod
    def _hash(path):
        digest = hashlib.sha256()
        with path.open("rb") as input_file:
            for chunk in iter(lambda: input_file.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
