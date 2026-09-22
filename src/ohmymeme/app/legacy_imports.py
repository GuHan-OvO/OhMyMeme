"""导入操作与导入路径的宿主适配入口。"""

import os
from pathlib import Path

from ohmymeme.core.imports import ImportPath

_RUNTIME_PROVIDERS = frozenset(
    {"source.qqnt", "source.telegram", "source.douyin", "source.wechat"}
)


def get_import_worker(legacy, provider_id):
    # 四个导入 provider 都在子进程 runtime 执行；未知 provider 不回退
    if provider_id not in _RUNTIME_PROVIDERS:
        raise ValueError(f"{provider_id}: provider_unavailable")
    from ohmymeme.app.runtime_imports import get_runtime_import_worker

    return get_runtime_import_worker(legacy, provider_id)


def import_paths(webui, file_paths, names=None):
    """通过当前 Container 的导入服务导入路径。"""

    requests = []
    for index, source in enumerate(file_paths):
        name = (
            names[index]
            if names and index < len(names)
            else os.path.splitext(os.path.basename(source))[0]
        )
        requests.append(ImportPath(Path(source), name))
    sink = webui._container.create_import_sink(webui._decode_stego)
    result = sink.import_batch(requests)
    return {"ids": list(result.imported_ids), "rejected": result.rejected}
