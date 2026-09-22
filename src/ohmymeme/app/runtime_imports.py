# pyright: basic

"""Runtime 支撑的宿主导入适配器：以 worker RPC 替代进程内 provider。"""

import shutil
import threading
from pathlib import Path

from ohmymeme.app.operations import (
    OperationBusyError,
    OperationClosedError,
    ResourceKind,
)
from ohmymeme.core.domain import TaskKind
from ohmymeme.core.imports import ImportPath
from ohmymeme.core.plugins.manifest import canonical_descriptor
from ohmymeme.core.plugins.policy import PluginPolicy
from ohmymeme.core.plugins.runtime import PluginRuntimeError
from ohmymeme.core.plugins.runtime.channel import ChannelClosed, RpcError

_RUNTIME_WORKERS_LOCK = threading.Lock()

_KINDS = {
    "source.qqnt": TaskKind.IMPORT_QQNT,
    "source.telegram": TaskKind.IMPORT_TELEGRAM,
    "source.douyin": TaskKind.IMPORT_DOUYIN,
    "source.wechat": TaskKind.IMPORT_WECHAT,
}


class RuntimeImportWorker:
    """保持 HostImportWorker 公开形状，执行体移到 worker 进程。"""

    def __init__(
        self,
        manager,
        provider_id,
        descriptor,
        policy,
        coordinator,
        sink,
        close_thread=None,
    ):
        self.manager = manager
        self.provider_id = provider_id
        self.descriptor = descriptor
        self.policy = policy
        self.coordinator = coordinator
        self.sink = sink
        self._close_thread = close_thread
        self._kind = _KINDS[provider_id]
        self._lock = threading.RLock()
        self._active = False
        self._listing = False
        self._operation = None
        self._handle = None
        self._progress = {}

    # 返回最近一次快照；空闲时向 worker 查询插件初始状态
    def get_progress(self):
        with self._lock:
            if self._progress:
                return dict(self._progress)
            active = self._active
        if active:
            return {}
        try:
            snapshot = self.manager.query(self.provider_id, "get_progress", {})
        except (PluginRuntimeError, ChannelClosed, RpcError):
            return {}
        if not isinstance(snapshot, dict):
            return {}
        with self._lock:
            if not self._progress:
                self._progress = dict(snapshot)
            return dict(self._progress)

    # 取消当前操作并结束协调器任务
    def cancel(self):
        with self._lock:
            active, handle = self._active, self._handle
            if active:
                self.coordinator.cancel(self._kind)
        if active and handle is not None:
            handle.cancel()

    # 查询插件固定方法（inspect 等）
    def query(self, name, params=None, nickname_lookup=None):
        return self.manager.query(
            self.provider_id,
            name,
            dict(params or {}),
            nickname_lookup=nickname_lookup,
        )

    # 启动操作：宿主建 operation，worker 执行插件方法
    def start(self, request, transient_secrets=None, finalize=None, listing=False):
        with self._lock:
            if self._active:
                return False
            previous = self.coordinator.query(self._kind)
            operation = self.policy.operation(
                self.descriptor, transient_secrets=transient_secrets
            )
            self._operation = operation
            self._listing = listing
            self._progress = {}

            def emit(kind, value):
                if kind == "progress" and not listing:
                    with self._lock:
                        self._progress = value

            def progress(value):
                operation.emit_progress(value)
                self.policy.flush_outputs(operation, emit)

            holder = {}

            def sink_batch(requests):
                paths = tuple(
                    ImportPath(Path(item["path"]), str(item["name"]))
                    for item in requests
                )
                handle = holder.get("handle")
                result = self.sink.import_batch(
                    paths,
                    cancelled=None if handle is None else lambda: handle.cancelled,
                )
                return {
                    "ids": list(result.imported_ids),
                    "rejected": result.rejected,
                }

            def config_set(key, value):
                operation.settings.set(key, value)
                self.policy.commit_settings(operation)

            def helper_wechat():
                from ohmymeme.integrations.imports.wechat import (
                    prepare_wechat_helper,
                )

                handle = holder.get("handle")
                helper = prepare_wechat_helper(
                    operation, None if handle is None else lambda: handle.cancelled
                )
                return [str(helper[0]), str(helper[1])]

            try:
                handle = self.manager.start_operation(
                    self.provider_id,
                    "list" if listing else "import",
                    dict(request),
                    workspace=operation.temporary.root,
                    config=operation.export_config(),
                    secrets=operation.export_secrets(),
                    listing=listing,
                    progress=progress,
                    sink_batch=sink_batch,
                    config_set=config_set,
                    helper_wechat=helper_wechat,
                )
            except (PluginRuntimeError, ChannelClosed, RpcError):
                self.policy.close_operation(operation)
                self._operation = None
                raise
            holder["handle"] = handle
            self._handle = handle
            self._active = True
            self._progress = dict(handle.initial_progress or {})

            def run(runtime):
                # 先登记清理，再允许任何阻塞调用
                def cleanup():
                    with self._lock:
                        self._active = False
                        self._handle = None
                        self._operation = None
                    self.policy.close_operation(operation)
                    if self._close_thread:
                        self._close_thread()

                runtime.register_temp(cleanup)
                try:
                    with runtime.lease(ResourceKind.DATABASE):
                        outcome = handle.wait()
                    if outcome is None:
                        outcome = {"ok": False, "error": "operation timeout"}
                    result = outcome.get("result")
                    if (
                        outcome.get("ok")
                        and finalize is not None
                        and result is not None
                    ):
                        finalize(result)
                    if outcome.get("ok"):
                        progress(self.get_progress())
                        if finalize is not None and result is not None and not listing:
                            with self._lock:
                                self._progress["result"] = operation.serialize_progress(
                                    result
                                )
                    else:
                        progress(
                            {
                                **self.get_progress(),
                                "status": "error",
                                "message": "导入失败",
                                "error": str(outcome.get("error") or ""),
                            }
                        )
                except Exception as error:
                    progress(
                        {
                            **self.get_progress(),
                            "status": "error",
                            "message": "导入失败",
                            "error": str(error),
                        }
                    )
                finally:
                    self.policy.flush_outputs(operation, emit)

            try:
                snapshot = self.coordinator.start(self._kind, run)
            except (OperationBusyError, OperationClosedError):
                handle.cancel()
                self._active = False
                self._handle = None
                self._operation = None
                self.policy.close_operation(operation)
                return False
            if previous and snapshot.task_id == previous.task_id:
                handle.cancel()
                self._active = False
                self._handle = None
                self._operation = None
                self.policy.close_operation(operation)
                return False
            return True

    # 保持 qqnt 输出策略在宿主：库内导入走 staging+sink，外部导出保留复制语义
    def start_qqnt(self, qq_number, output_dir, image_only, overwrite, config):
        from ohmymeme.integrations.imports import qqnt

        userdata = config.get("qqnt_userdata_path") or None
        ini = config.get("qqnt_ini_path") or qqnt.DEFAULT_INI_PATH
        library = qqnt.targets_library_output(output_dir, config.cache_dir)
        output = Path(output_dir).expanduser().resolve()
        if not library:
            source_root = userdata or qqnt.get_userdata_save_path(ini)
            if source_root and qqnt.targets_library_output(
                output, qqnt.get_emoji_dir(source_root, qq_number)
            ):
                raise ValueError("输出目录不能与源表情目录重叠")
            if output.exists() and not overwrite and any(output.iterdir()):
                raise FileExistsError("输出目录已存在且非空: " + str(output))

        def finalize(result):
            # 普通导出保留非图片与坏图复制语义，不经 sink
            if not library:
                staging = Path(result["output_dir"])
                if overwrite and output.is_dir():
                    for entry in output.iterdir():
                        if entry.is_dir() and not entry.is_symlink():
                            shutil.rmtree(entry)
                        else:
                            entry.unlink()
                shutil.copytree(staging, output, dirs_exist_ok=True)
            result["output_dir"] = output_dir

        return self.start(
            {
                "qq_number": qq_number,
                "userdata_save_path": userdata,
                "ini_path": ini,
                "image_only": image_only,
                "submit": library,
            },
            finalize=finalize,
        )

    def list_wechat(self, user_root, account_path=None):
        # 保留同步列表 ABI：等待 worker 完成并返回结果字典
        result = {}
        if not self.start(
            {"user_root": user_root, "account_path": account_path},
            finalize=result.update,
            listing=True,
        ):
            return result
        self.coordinator.wait(self._kind, 150)
        if self._active:
            self.cancel()
            self.coordinator.wait(self._kind, 5)
            return {}
        return result


def get_runtime_import_worker(legacy, provider_id):
    # 缓存挂在 webui 上，轮询不重建 worker
    webui = legacy._webui
    with _RUNTIME_WORKERS_LOCK:
        cache = getattr(webui, "_runtime_import_workers", None)
        if cache is None:
            cache = {}
            webui._runtime_import_workers = cache
        worker = cache.get(provider_id)
        if worker is not None:
            return worker
        enabled = getattr(webui, "_enabled_import_plugins", None)
        if enabled is not None and provider_id not in enabled:
            raise ValueError(f"{provider_id}: provider_disabled")
        descriptor = canonical_descriptor(provider_id)
        manager = getattr(webui._container, "plugin_runtime", None)
        if manager is None:
            raise ValueError(f"{provider_id}: provider_unavailable")
        worker = RuntimeImportWorker(
            manager,
            provider_id,
            descriptor,
            PluginPolicy(webui._cfg),
            webui._container.operations,
            webui._container.create_import_sink(webui._decode_stego),
            webui._container.db.close,
        )
        cache[provider_id] = worker
        return worker
