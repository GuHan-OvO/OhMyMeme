from copy import deepcopy
from functools import partial
from math import isfinite
from types import MappingProxyType

from ohmymeme.core.plugins.contracts import PluginCapabilities, PluginDescriptor
from ohmymeme.core.plugins.manifest import (
    CANONICAL_BY_ID,
    ENTRY_POINT_GROUP,
    PLUGIN_API_VERSION,
)
from ohmymeme.core.plugins.registry import PluginRegistry
from ohmymeme.core.schemas.bridge import (
    MAIN_BRIDGE_CONTRACT,
    SETTINGS_BRIDGE_CONTRACT,
)

# Host-owned compatibility data, not a plugin SDK or a dynamic Bridge registry.
# Each row fixes provider(s), positional argument names, failure and label.
IMPORT_ACTIONS = MappingProxyType(
    {
        "qqnt_check_env": ("source.qqnt", (), {}, "检测环境"),
        "qqnt_pick_ini": ("source.qqnt", (), {"ok": False}, "选择配置文件"),
        "qqnt_pick_userdata": ("source.qqnt", (), {"ok": False}, "选择用户数据目录"),
        "qqnt_pick_base": ("source.qqnt", (), {"ok": False}, "选择保存文件夹"),
        "qqnt_default_dir": (
            "source.qqnt",
            ("base", "qq_number"),
            {"ok": False},
            "输出目录",
        ),
        "qqnt_start": (
            "source.qqnt",
            ("qq_number", "output_dir", "image_only", "overwrite"),
            {"ok": False},
            "开始提取",
        ),
        "qqnt_get_progress": ("source.qqnt", (), {}, "提取进度"),
        "qqnt_cancel": ("source.qqnt", (), None, "取消"),
        "qqnt_open_dir": ("source.qqnt", ("path",), False, "打开目录"),
        "pick_tg_tdata": ("source.telegram", (), {"ok": False}, "手动指定 tdata 目录"),
        "start_tg_import": (
            "source.telegram",
            ("tdata_path", "passcode", "convert_webm"),
            {"ok": False},
            "开始导入",
        ),
        "get_tg_import_progress": ("source.telegram", (), {}, "导入进度"),
        "cancel_tg_import": ("source.telegram", (), None, "取消导入"),
        "start_douyin_import": (
            "source.douyin",
            ("cookie",),
            {"ok": False},
            "开始下载",
        ),
        "get_douyin_import_progress": ("source.douyin", (), {}, "下载进度"),
        "cancel_douyin_import": ("source.douyin", (), None, "取消"),
        "pick_wechat_root": ("source.wechat", (), {"ok": False}, "选择目录"),
        "inspect_wechat_environment": ("source.wechat", ("user_root",), {}, "检测环境"),
        "list_wechat_stickers": (
            "source.wechat",
            ("user_root", "account_path"),
            {},
            "表情列表",
        ),
        "start_wechat_import": (
            "source.wechat",
            ("user_root", "download", "account_path"),
            {"ok": False},
            "开始导入",
        ),
        "get_wechat_import_progress": ("source.wechat", (), {}, "导入进度"),
        "cancel_wechat_import": ("source.wechat", (), None, "取消导入"),
    }
)
SYNC_PROVIDERS = MappingProxyType(
    {
        "ftp": "sync.ftp",
        "s3": "sync.s3",
        "r2": "sync.r2",
        "webdav": "sync.webdav",
    }
)
_SYNC = tuple(SYNC_PROVIDERS.values())
_LAN = ("transport.lan",)
_SYNC_FAILURE = {"ok": False, "error": "sync failed", "failed_files": []}
HOST_ACTIONS = {
    "main": {
        "sync_push": (_SYNC, (), _SYNC_FAILURE, "上传"),
        "sync_pull": (_SYNC, (), _SYNC_FAILURE, "下载"),
        "sync_test": (_SYNC, (), "", "测试连接"),
        "get_sync_progress": (_SYNC, (), {}, "同步进度"),
        "run_auto_sync": (
            _SYNC,
            (),
            {"fetched": False, "synced": False, "error": ""},
            "自动同步",
        ),
        "lan_confirm_device": (
            _LAN,
            ("approved", "confirm_id"),
            {"ok": False},
            "设备确认",
        ),
    },
    "settings": {
        "sync_push": (_SYNC, ("delete_remote",), _SYNC_FAILURE, "上传"),
        "sync_pull": (_SYNC, ("remove_local",), _SYNC_FAILURE, "下载"),
        "sync_test": (_SYNC, (), "", "测试连接"),
        "get_sync_progress": (_SYNC, (), {}, "同步进度"),
        "delete_all_cloud": (_SYNC, (), {"ok": False}, "清空云端"),
        "get_remote_orphans": (_SYNC, ("delete",), {"ok": False}, "孤儿文件"),
        "check_sync_status": (_SYNC, (), {"ok": False}, "检查同步状态"),
        "lan_start": (_LAN, ("port", "secret"), {"ok": False}, "启用局域网互联"),
        "lan_stop": (_LAN, (), {"ok": False}, "停止局域网互联"),
        "lan_get_status": (_LAN, (), {}, "局域网状态"),
        "lan_get_ip": (_LAN, (), "", "局域网 IP"),
        "lan_set_allow_secret_config": (
            _LAN,
            ("enabled",),
            {"ok": False},
            "允许密钥传输",
        ),
    },
}
ACTIONS = {
    "main": MappingProxyType(HOST_ACTIONS["main"]),
    "settings": MappingProxyType({**IMPORT_ACTIONS, **HOST_ACTIONS["settings"]}),
}
CONTRACTS = {"main": MAIN_BRIDGE_CONTRACT, "settings": SETTINGS_BRIDGE_CONTRACT}
IMPORT_IDS = tuple(dict.fromkeys(row[0] for row in IMPORT_ACTIONS.values()))


def action_spec(surface, action):
    # Resolve only literal, host-declared actions, never an arbitrary attribute.
    if not isinstance(action, str) or action not in ACTIONS[surface]:
        raise ValueError("action: unknown fixed action")
    return ACTIONS[surface][action]


def validate_arguments(surface, action, args):
    # Validate exact positions/types without logging credentials or input values.
    _, names, _, _ = action_spec(surface, action)
    if not isinstance(args, (list, tuple)) or len(args) != len(names):
        raise ValueError("args: expected fixed positional argument count")
    spec = CONTRACTS[surface]._specs[action]
    for index, (adapter, value) in enumerate(zip(spec.arguments, args, strict=True)):
        try:
            adapter.validate_python(value, strict=True)
        except ValueError as error:
            raise ValueError(f"args[{index}].{names[index]}: invalid type") from error
    return tuple(args)


def validate_contribution(value):
    # Validate inert metadata for the existing shell; no UI is registered here.
    fields = {"provider", "action", "label", "args"}
    if not isinstance(value, dict):
        raise ValueError("contribution: expected object")
    for field in sorted(fields - value.keys()):
        raise ValueError(f"contribution.{field}: missing field")
    for field in sorted(value.keys() - fields):
        raise ValueError(f"contribution.{field}: forbidden field")
    provider, _, _, label = action_spec("settings", value["action"])
    providers = (provider,) if isinstance(provider, str) else provider
    if value["provider"] not in providers:
        raise ValueError("contribution.provider: action belongs to another provider")
    if value["label"] != label:
        raise ValueError("contribution.label: expected fixed host label")
    return validate_arguments("settings", value["action"], value["args"])


def _descriptor(provider_id):
    # Describe the built-in host adapter using the same official ABI identity.
    _, package, capabilities = CANONICAL_BY_ID[provider_id]
    return PluginDescriptor(
        provider_id,
        PLUGIN_API_VERSION,
        package,
        ENTRY_POINT_GROUP,
        provider_id,
        f"{package}:create_plugin",
        PluginCapabilities(capabilities),
    )


class HostActionAdapter:
    # This host-owned adapter is never given to a plugin as context or capability.
    def __init__(self, provider_id, handlers):
        # Snapshot only the explicitly allowed callable bindings for one provider.
        if provider_id not in IMPORT_IDS or not isinstance(handlers, dict):
            raise ValueError("provider: expected official import adapter")
        for action, handler in handlers.items():
            if action not in IMPORT_ACTIONS or IMPORT_ACTIONS[action][0] != provider_id:
                raise ValueError("action: outside provider action set")
            if not callable(handler):
                raise ValueError("action: expected host callback")
        self.provider_id = provider_id
        self._handlers = MappingProxyType(dict(handlers))

    def call(self, action, args):
        # The adapter exposes no unbounded execute or attribute lookup path.
        checked = validate_arguments("settings", action, args)
        if action not in self._handlers:
            raise ValueError("action: unavailable in provider adapter")
        return self._handlers[action](*checked)


def _validate_import_result(action, result):
    # Preserve legacy dictionaries but reject invalid success/progress/UI payloads.
    result = SETTINGS_BRIDGE_CONTRACT.validate_output(action, result)
    if not isinstance(result, dict):
        return result
    if set(result) & {"html", "script", "javascript", "bridge_methods", "ui"}:
        raise ValueError("result: forbidden UI contribution")
    for field in ("ok", "cancelled"):
        if field in result and type(result[field]) is not bool:
            raise ValueError(f"result.{field}: expected boolean")
    if action in (
        "qqnt_start",
        "start_tg_import",
        "start_douyin_import",
        "start_wechat_import",
    ):
        if type(result.get("ok")) is not bool:
            raise ValueError("result.ok: missing boolean")
    for field in ("status", "message", "error", "error_code"):
        if field in result and not isinstance(result[field], str):
            raise ValueError(f"result.{field}: expected string")
    for field in ("progress", "elapsed_s", "done", "total"):
        if field in result and (
            type(result[field]) not in (int, float) or not isfinite(result[field])
        ):
            raise ValueError(f"result.{field}: expected finite number")
    return result


class HostDispatcher:
    def __init__(self, surface, registry, host_actions=None, enabled=None):
        # Freeze startup activation; registry absence never selects another provider.
        if surface not in ACTIONS:
            raise ValueError("surface: unknown host surface")
        self.surface = surface
        self.registry = registry
        self._host_actions = dict(host_actions or {})
        if self._host_actions.keys() - HOST_ACTIONS[surface].keys():
            raise ValueError("action: unknown host action")
        self._enabled = frozenset(IMPORT_IDS if enabled is None else enabled)
        self.last_error = None

    def dispatch(self, action, args):
        # Only fixed import actions reach the registry; sync/LAN stay host-owned.
        provider, _, failure, _ = action_spec(self.surface, action)
        checked = validate_arguments(self.surface, action, args)
        self.last_error = None
        try:
            if action in HOST_ACTIONS[self.surface]:
                result = self._host_actions[action](*checked)
                return CONTRACTS[self.surface].validate_output(action, result)
            if provider not in self._enabled:
                raise ValueError("provider_disabled")
            descriptors = tuple(
                d for d in self.registry.descriptors() if d.id == provider
            )
            if (
                descriptors != (_descriptor(provider),)
                or type(descriptors[0].api_version) is not int
            ):
                raise ValueError("provider_incompatible")
            adapter = self.registry.get(provider)
            if (
                not isinstance(adapter, HostActionAdapter)
                or adapter.provider_id != provider
            ):
                raise ValueError("provider_unavailable")
            return _validate_import_result(action, adapter.call(action, checked))
        except Exception as error:
            self.last_error = {"action": action, "error_type": type(error).__name__}
            return deepcopy(failure)


def _legacy_adapter(legacy, provider_id):
    # Bind host compatibility methods lazily; plugin factories receive no WebUI.
    return HostActionAdapter(
        provider_id,
        {
            action: getattr(legacy, action)
            for action, row in IMPORT_ACTIONS.items()
            if row[0] == provider_id and hasattr(legacy, action)
        },
    )


def compatibility_dispatcher(legacy, surface, registry=None, enabled=None):
    # Keep shipped implementations usable until their individual provider migrations.
    if registry is None:
        registry = PluginRegistry(
            tuple(_descriptor(provider) for provider in IMPORT_IDS),
            {
                provider: partial(_legacy_adapter, legacy, provider)
                for provider in IMPORT_IDS
            },
            discovered_entry_points=(),
        )
    return HostDispatcher(
        surface,
        registry,
        {
            action: getattr(legacy, action)
            for action in HOST_ACTIONS[surface]
            if hasattr(legacy, action)
        },
        enabled,
    )
