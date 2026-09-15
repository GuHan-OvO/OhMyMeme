"""桌面导入回调与四个提供方的宿主 operation 接线。"""

import os
import shutil
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

from ohmymeme.app.operations import (
    OperationBusyError,
    OperationClosedError,
    ResourceKind,
)
from ohmymeme.core.domain import TaskKind
from ohmymeme.core.imports import ImportPath, ImportResult
from ohmymeme.core.plugins.contracts import ImportPluginContext
from ohmymeme.core.plugins.policy import PluginPolicy
from ohmymeme.core.plugins.registry import PluginRegistry

_IMPORT_WORKERS_LOCK = threading.Lock()


class HostImportWorker:
    def __init__(
        self, provider, descriptor, policy, coordinator, sink, close_thread=None
    ):
        # One host adapter retains one provider across start/progress/cancel.
        self.provider = provider
        self.descriptor = descriptor
        self.policy = policy
        self.coordinator = coordinator
        self.sink = sink
        self._close_thread = close_thread
        self.operation = None
        self._lock = threading.RLock()
        self._active = False
        self._listing = False
        self._progress = provider.get_progress()
        self._kind = {
            "source.qqnt": TaskKind.IMPORT_QQNT,
            "source.telegram": TaskKind.IMPORT_TELEGRAM,
            "source.douyin": TaskKind.IMPORT_DOUYIN,
            "source.wechat": TaskKind.IMPORT_WECHAT,
        }[descriptor.id]

    def get_progress(self):
        # Keep completed snapshots readable after operation secrets are closed.
        with self._lock:
            active, operation = self._active, self.operation
            snapshot = dict(self._progress)
        if active and not self._listing:
            return operation.serialize_progress(self.provider.get_progress())
        return snapshot

    def cancel(self):
        # Stop is idempotent; ownership lasts until the worker exits.
        with self._lock:
            if self._active:
                self.coordinator.cancel(self._kind)
            self.provider.stop()

    def start(self, request, transient_secrets=None, finalize=None, listing=False):
        # Reserve before returning so repeated bridge calls cannot double-start.
        with self._lock:
            if self._active:
                return False
            previous = self.coordinator.query(self._kind)
            operation = self.policy.operation(
                self.descriptor, transient_secrets=transient_secrets
            )
            self.operation = operation
            self._listing = listing

            def emit(kind, value):
                if kind == "progress" and not listing:
                    with self._lock:
                        self._progress = value

            def progress(value):
                operation.emit_progress(value)
                self.policy.flush_outputs(operation, emit)

            context = ImportPluginContext(
                self.descriptor,
                self.sink,
                progress,
                lambda: False,
                operation=operation,
                request=dict(request),
            )
            try:
                if not listing and not self.provider.start(context):
                    self.policy.close_operation(operation)
                    return False
                self._active = True
                if not listing:
                    self._progress = operation.serialize_progress(
                        self.provider.get_progress()
                    )

                def run(runtime):
                    # Register cleanup before any plugin I/O or blocking call.
                    def cleanup():
                        self.provider.finish()
                        self.policy.close_operation(operation)
                        with self._lock:
                            self._active = False

                    runtime.register_temp(cleanup)
                    resources = SimpleNamespace(
                        register_process=runtime.register_process
                    )
                    if self.descriptor.id == "source.wechat":
                        from ohmymeme.integrations.imports.wechat import (
                            prepare_wechat_helper,
                        )

                        resources.wechat_helper = lambda: prepare_wechat_helper(
                            operation, lambda: runtime.cancel_requested
                        )
                    bound = context._replace(
                        is_cancelled=lambda: runtime.cancel_requested,
                        resources=resources,
                    )
                    try:
                        with runtime.lease(ResourceKind.DATABASE):
                            result = (
                                self.provider.list_stickers(bound)
                                if listing
                                else self.provider.import_media(bound)
                            )
                        if finalize and result is not None:
                            finalize(result)
                        progress(self.provider.get_progress())
                        if finalize and result is not None and not listing:
                            self._progress["result"] = operation.serialize_progress(
                                result
                            )
                    except Exception as error:
                        progress(
                            {
                                **self.provider.get_progress(),
                                "status": "error",
                                "error": str(error),
                            }
                        )
                    finally:
                        self.policy.flush_outputs(operation, emit)
                        if self._close_thread:
                            self._close_thread()

                snapshot = self.coordinator.start(self._kind, run)
                if previous and snapshot.task_id == previous.task_id:
                    raise OperationBusyError("library")
                return True
            except (OperationBusyError, OperationClosedError):
                self.provider.stop()
                self.provider.finish()
                self._active = False
                self.policy.close_operation(operation)
                return False
            except Exception:
                self.provider.stop()
                self.provider.finish()
                self._active = False
                self.policy.close_operation(operation)
                raise

    def list_wechat(self, user_root, account_path=None):
        # Retain the synchronous list ABI while owning its helper and temporary scope.
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

    def start_qqnt(self, qq_number, output_dir, image_only, overwrite, config):
        # Keep cache aliases and external overwrite policy entirely in the host.
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
            # Ordinary exports preserve non-images and never enter the sink.
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


def get_import_worker(legacy, provider_id):
    # Cache on the host; polling never reconstructs the plugin.
    from .api.plugin_dispatch import _descriptor

    webui = legacy._webui
    with _IMPORT_WORKERS_LOCK:
        if not hasattr(webui, "_import_workers"):
            webui._import_workers = {}
        if provider_id not in webui._import_workers:
            descriptor = _descriptor(provider_id)
            registry = getattr(getattr(webui, "_container", None), "plugins", None)
            if registry is None:
                registry = PluginRegistry((descriptor,))
            provider = registry.require(
                provider_id, getattr(webui, "_enabled_import_plugins", None)
            )
            webui._import_workers[provider_id] = HostImportWorker(
                provider,
                descriptor,
                PluginPolicy(webui._cfg),
                webui._container.operations,
                webui._container.create_import_sink(webui._decode_stego),
                webui._container.db.close,
            )
        return webui._import_workers[provider_id]


class CallbackSink:
    def __init__(self, callback):
        # Adapt the published callback ABI without exposing its owner to plugins.
        self._callback = callback

    def import_batch(self, requests, cancelled=None):
        # Cancellation is checked before the legacy host callback is admitted.
        if cancelled and cancelled():
            return ImportResult((), 0)
        result = self._callback([str(request.path) for request in requests])
        return ImportResult(tuple(result.get("ids", [])), result.get("rejected", 0))


class LegacyImportSession:
    def __init__(self, provider_id):
        # Only the ABI shim uses a default session; official factories never do.
        from ohmymeme.app.operation_coordinator import OperationCoordinator

        from .api.plugin_dispatch import _descriptor

        self.descriptor = _descriptor(provider_id)
        self.provider = PluginRegistry((self.descriptor,)).require(provider_id)
        self.coordinator = OperationCoordinator()
        self.worker = None
        self._lock = threading.Lock()

    def start(self, callback, request, secrets=None):
        # The compatibility path also owns its operation and background thread.
        with self._lock:
            if self.worker and self.worker._active:
                return False
            temporary = tempfile.TemporaryDirectory(prefix="ohmm-legacy-import-")
            policy = PluginPolicy(
                SimpleNamespace(
                    data_dir=Path(temporary.name),
                    get_plugin_value=lambda provider, key, legacy, secret=False: None,
                )
            )
            self.worker = HostImportWorker(
                self.provider,
                self.descriptor,
                policy,
                self.coordinator,
                CallbackSink(callback),
            )
            original_close = policy.close_operation

            def close(operation):
                original_close(operation)
                temporary.cleanup()

            policy.close_operation = close
            if callback is None and self.descriptor.id == "source.wechat":
                return self.worker.list_wechat(
                    request.get("user_root"), request.get("account_path")
                )
            return self.worker.start(request, secrets)

    def list_wechat(self, user_root, account_path=None):
        # Reuse the compatibility host's coordinator and resource preparation.
        return self.start(None, {"user_root": user_root, "account_path": account_path})

    def cancel(self):
        # Cancellation targets the same provider instance used by start/progress.
        if self.worker:
            self.worker.cancel()
        else:
            self.provider.stop()

    def get_progress(self):
        # Preserve idle/finished progress when no operation is currently active.
        return (
            self.worker.get_progress() if self.worker else self.provider.get_progress()
        )


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
