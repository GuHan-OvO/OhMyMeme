"""内容寻址媒体文件的原子仓储。"""

import hashlib
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ohmymeme.core.assets import AssetPaths, is_safe_filename
from ohmymeme.core.recovery import StorageRecovery


@dataclass(frozen=True, slots=True)
class StoredAsset:
    content_hash: str
    filename: str
    path: Path
    created: bool


class AtomicFileRepository:
    """在当前缓存根以 flush、fsync、replace 方式提交内容寻址文件。"""

    def __init__(self, assets: AssetPaths) -> None:
        self.assets = assets
        self._recovery = StorageRecovery(assets.data_dir, assets.cache_dir)

    def commit_bytes(self, data: bytes, extension: str) -> StoredAsset:
        temporary = self._temporary_path()
        try:
            with temporary.open("wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            return self._commit_temporary(temporary, extension)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise

    def commit_path(self, source: Path, extension: str) -> StoredAsset:
        temporary = self._temporary_path()
        try:
            with source.open("rb") as input_file, temporary.open("wb") as output:
                for chunk in iter(lambda: input_file.read(65536), b""):
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            return self._commit_temporary(temporary, extension)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise

    def discard(self, asset: StoredAsset) -> None:
        if asset.created:
            asset.path.unlink()

    def recover(self) -> int:
        if self._recovery.marker_path.exists():
            return 0
        temporary_files = tuple(self.assets.cache_dir.glob(".atomic-*.tmp"))
        for temporary in temporary_files:
            temporary.unlink()
        return len(temporary_files)

    def migrate_to(
        self,
        destination: Path,
        commit_metadata: Callable[[], None],
        write_manifest: Callable[[], None],
    ) -> int:
        return self._recovery.migrate(
            destination, True, commit_metadata, write_manifest
        )

    def recover_migration(
        self, metadata_target: Path, write_manifest: Callable[[], None]
    ) -> bool:
        recovered = self._recovery.recover_before_database(metadata_target)
        if self._recovery.marker_path.exists():
            self._recovery.finish_manifest(write_manifest)
        return recovered

    def locate(self, filename: str) -> Path | None:
        if not is_safe_filename(filename):
            return None
        direct = self.assets.cache_dir / filename
        if direct.is_file():
            return direct
        for candidate in self.assets.cache_dir.rglob(filename):
            if candidate.is_file():
                return candidate
        return None

    def invalidate_thumbnails(self, meme_id: int) -> None:
        for thumbnail in self.assets.thumbnail_dir.glob(f"{meme_id}_*.png"):
            thumbnail.unlink()

    def _temporary_path(self) -> Path:
        self.assets.cache_dir.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=".atomic-", suffix=".tmp", dir=self.assets.cache_dir
        )
        os.close(descriptor)
        return Path(name)

    def _commit_temporary(self, temporary: Path, extension: str) -> StoredAsset:
        content_hash = self._hash(temporary)
        filename = f"{content_hash[:16]}{extension}"
        destination = self.assets.cache_dir / filename
        if destination.exists():
            if self._hash(destination) != content_hash:
                raise OSError(f"content-addressed destination is corrupt: {filename}")
            temporary.unlink()
            return StoredAsset(content_hash, filename, destination, False)
        os.replace(temporary, destination)
        return StoredAsset(content_hash, filename, destination, True)

    @staticmethod
    def _hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as input_file:
            for chunk in iter(lambda: input_file.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
