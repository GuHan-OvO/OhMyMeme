"""同步 backend 分层测试夹具：用本地会话替代 worker 会话。

插件代码仍真实执行，只是不跨进程；worker 全链路由
`tests/application/test_remote_mutation_coordinator.py` 的 loopback 用例覆盖。
"""

import importlib
from pathlib import Path

from ohmymeme.core.plugins.network_config import SYNC_CONFIGS

_MODULES = {
    "sync.ftp": "ohmymeme_plugin_sync_ftp",
    "sync.s3": "ohmymeme_plugin_sync_s3",
    "sync.r2": "ohmymeme_plugin_sync_r2",
    "sync.webdav": "ohmymeme_plugin_sync_webdav",
}
_PATH_ARGUMENTS = {"upload_file": (0,), "download_file": (1,)}


class LocalSecrets:
    """带关闭语义的本地密钥端口。"""

    def __init__(self, values):
        self._values = dict(values or {})
        self._closed = False

    def get(self, key):
        if self._closed:
            raise RuntimeError("operation scope is closed")
        return self._values.get(key)

    def revoke(self):
        self._values.clear()
        self._closed = True


class LocalSession:
    """与 RuntimeSyncSession 相同的方法面，直接调用本地插件 backend。"""

    def __init__(self, provider_id, config, secrets):
        module = importlib.import_module(_MODULES[provider_id])
        provider = module.create_plugin()
        self.backend = provider.create_backend(config, secrets)
        self._secrets = secrets

    def call(self, method, args=(), timeout=None):
        # 与 worker 相同：路径参数在调用前恢复为 Path
        arguments = list(args)
        for index in _PATH_ARGUMENTS.get(method, ()):
            if index < len(arguments):
                arguments[index] = Path(arguments[index])
        return getattr(self.backend, method)(*arguments)

    def close(self):
        try:
            self.backend.close()
        finally:
            self._secrets.revoke()

    def __getattr__(self, name):
        return getattr(self.backend, name)


class LocalSyncRuntime:
    """open_backend 返回本地会话；便于对宿主适配层做进程内断言。"""

    def __init__(self):
        self.sessions = []

    def open_backend(self, provider_id, config, secrets, enabled=None):
        session = LocalSession(provider_id, config, LocalSecrets(secrets))
        self.sessions.append(session)
        return session


class FailingRuntime:
    """模拟 worker 解析失败或缺失 provider。"""

    def __init__(self, reason="plugin_missing"):
        self.reason = reason

    def open_backend(self, provider_id, config, secrets, enabled=None):
        from ohmymeme.core.plugins.runtime import PluginRuntimeError

        raise PluginRuntimeError(self.reason, provider_id)


def config_record(provider_id, **overrides):
    """按 provider 构造合法配置记录。"""
    record = SYNC_CONFIGS[provider_id]
    values = dict(record._field_defaults)
    values.update(overrides)
    return record(**values)
