"""远端同步索引清单维护。"""

import json
import logging
import os

from ohmymeme.app.manifest_service import ManifestService, ManifestValidationError

from .assets import INDEX_FILENAME as _INDEX_FILENAME
from .assets import AssetPaths
from .config import get_config
from .database import get_db

logger = logging.getLogger(__name__)
INDEX_FILENAME = _INDEX_FILENAME


def _assets():
    config = get_config()
    return AssetPaths(config.data_dir, config.cache_dir)


def _index_path():
    return _assets().manifest_path


def _build_collection_tree(db, parent_id=None, empty_ids=None):
    if empty_ids is None:
        empty_ids = []
    raw = db.get_collections()
    items = []
    for cid, cname, pid, _ in raw:
        if pid != parent_id:
            continue
        member_rows = db.search(collection_id=cid, limit=999999)
        filenames = [mr["filename"] for mr in member_rows]
        children = _build_collection_tree(db, parent_id=cid, empty_ids=empty_ids)
        if not filenames and not children:
            if not member_rows:
                empty_ids.append(cid)
            continue
        item = {"name": cname, "filenames": filenames}
        if children:
            item["children"] = children
        items.append(item)
    return items


def _write(data):
    path = _index_path()
    tmp = path.with_name(path.name + ".tmp")
    projection = ManifestService().parse_data(data)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as output:
            json.dump(projection.to_data(), output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def build():
    """从数据库重建完整索引并写入磁盘。"""
    db = get_db()
    rows = db.search(keyword="", tags=None, limit=999999)
    assets = _assets()
    memes = []
    for sort_order, row in enumerate(rows):
        filename = row["filename"]
        file_path = assets.cache_dir / filename
        mtime = ""
        if file_path.exists():
            try:
                mtime = str(int(file_path.stat().st_mtime))
            except OSError:
                pass
        memes.append(
            {
                "filename": filename,
                "name": row.get("original_name", os.path.splitext(filename)[0]),
                "sha256": row.get("file_hash", ""),
                "file_size": row.get("file_size", 0),
                "mtime": mtime,
                "sort_order": sort_order,
            }
        )
    empty_ids = []
    collections = _build_collection_tree(db, empty_ids=empty_ids)
    data = {"version": 3, "memes": memes, "collections": collections}
    try:
        _write(data)
        for collection_id in empty_ids:
            db.delete_collection(collection_id)
        logger.debug(
            "manifest written: %d memes, %d collections", len(memes), len(collections)
        )
    except OSError:
        logger.exception("manifest write failed")
        raise
    return memes


def load():
    """加载索引文件，不存在时返回空结构。"""
    path = _index_path()
    if not path.exists():
        return {"version": 3, "memes": [], "collections": []}
    try:
        return ManifestService().parse_json(path.read_bytes()).to_data()
    except (ManifestValidationError, OSError) as error:
        logger.warning("manifest load failed: %s", error)
        return {"version": 3, "memes": [], "collections": []}
