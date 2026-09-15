import inspect
import json
from copy import deepcopy

from ohmymeme.core.plugins.manifest import CANONICAL_BY_ID

from .plugin_dispatch import ACTIONS, CONTRACTS, IMPORT_ACTIONS, SYNC_PROVIDERS

# Only identities for existing host screens, never markup, selectors or plugin code.
SCREENS = (
    ("source.qqnt", "电脑版 QQ（QQNT 本地缓存）", "qq", "qqnt", "startQQNTWizard"),
    (
        "source.telegram",
        "Telegram Desktop",
        "telegram",
        "telegram",
        "openTGImportDialog",
    ),
    ("source.douyin", "抖音", "douyin", "douyin", "openDYImportDialog"),
    ("source.wechat", "微信", "wechat", "wechat", "openWechatImportDialog"),
    ("sync.ftp", "FTP", "none", "ftp", "toggleSyncType"),
    ("sync.s3", "S3 兼容存储", "none", "s3", "toggleSyncType"),
    ("sync.r2", "Cloudflare R2", "none", "r2", "toggleSyncType"),
    ("sync.webdav", "WebDAV", "none", "webdav", "toggleSyncType"),
    ("transport.lan", "局域网互联", "none", "lan", "toggleLan"),
)
PROGRESS_FIELDS = {
    "qqnt_get_progress": ("status", "progress", "message", "error", "log", "result"),
    "get_tg_import_progress": (
        "status",
        "progress",
        "message",
        "error",
        "error_code",
        "total",
        "done",
        "imported",
        "rejected",
        "convert_failed",
        "skipped_static",
        "elapsed_s",
    ),
    "get_douyin_import_progress": (
        "status",
        "progress",
        "message",
        "error",
        "error_code",
        "total",
        "done",
        "imported",
        "rejected",
    ),
    "get_wechat_import_progress": (
        "status",
        "progress",
        "message",
        "error",
        "error_code",
        "total",
        "done",
        "imported",
        "failed",
        "rejected",
    ),
    "get_sync_progress": (
        "status",
        "direction",
        "progress",
        "files_done",
        "files_total",
        "bytes_done",
        "bytes_total",
        "current_file",
        "speed",
        "start_time",
        "results",
        "error",
        "failed_items",
    ),
}


def fixed_action_matrix():
    # Read only named host facades and existing Bridge adapters, not provider factories.
    from .main_facade import MainBridgeFacade
    from .settings_facade import SettingsBridgeFacade

    rows = []
    for surface, facade in (
        ("main", MainBridgeFacade),
        ("settings", SettingsBridgeFacade),
    ):
        for action, (providers, names, _, label) in ACTIONS[surface].items():
            parameters = list(
                inspect.signature(getattr(facade, action)).parameters.values()
            )[1:]
            arguments, samples = [], []
            for name, parameter, adapter in zip(
                names,
                parameters,
                CONTRACTS[surface]._specs[action].arguments,
                strict=True,
            ):
                schema = adapter.json_schema()
                required = parameter.default is inspect.Parameter.empty
                argument = {"name": name, "schema": schema, "required": required}
                if not required:
                    argument["default"] = parameter.default
                arguments.append(argument)
                samples.append(
                    parameter.default
                    if not required
                    else {
                        "string": "fixture",
                        "boolean": False,
                        "integer": 1,
                    }[schema["type"]]
                )
            rows.append(
                {
                    "surface": surface,
                    "action": action,
                    "providers": (
                        [providers] if isinstance(providers, str) else list(providers)
                    ),
                    "arguments": arguments,
                    "sample_args": samples,
                    "label": label,
                    "screen": (
                        "imports"
                        if action in IMPORT_ACTIONS
                        else ("lan" if action.startswith("lan_") else "sync")
                    ),
                    "owner": (
                        "host-import-adapter"
                        if action in IMPORT_ACTIONS
                        else "host-service"
                    ),
                    "progress_fields": list(PROGRESS_FIELDS.get(action, ())),
                }
            )
    return {
        "schema_version": 1,
        "actions": rows,
        "sync_providers": dict(SYNC_PROVIDERS),
        "excluded_plugins": ["adb.qq", "qq.mobile"],
    }


def fixed_ui_document(actions=None):
    # Project the validated Todo6 matrix into the nine existing settings screens.
    matrix = fixed_action_matrix()
    if actions is not None:
        errors = exact_errors(actions, matrix, "actions")
        if errors:
            raise ValueError("; ".join(errors))
        matrix = actions
    contributions = []
    for provider, label, icon, screen, handler in SCREENS:
        if provider not in CANONICAL_BY_ID:
            raise ValueError("provider: unknown official identity")
        contributions.append(
            {
                "provider": provider,
                "label": label,
                "icon": icon,
                "screen": screen,
                "handler": handler,
                "actions": [
                    {
                        key: deepcopy(row[key])
                        for key in (
                            "action",
                            "label",
                            "arguments",
                            "progress_fields",
                        )
                    }
                    for row in matrix["actions"]
                    if row["surface"] == "settings" and provider in row["providers"]
                ],
            }
        )
    return {"schema_version": 1, "contributions": contributions}


def exact_errors(value, expected, field):
    # Closed data equality includes every nested key and exact bool/int/float types.
    if type(value) is not type(expected):
        return [f"{field}: unexpected type"]
    errors = []
    if isinstance(expected, dict):
        errors.extend(
            f"{field}.{key}: missing field"
            for key in sorted(expected.keys() - value.keys())
        )
        errors.extend(
            f"{field}.{key}: forbidden field"
            for key in sorted(value.keys() - expected.keys())
        )
        for key in expected.keys() & value.keys():
            errors.extend(exact_errors(value[key], expected[key], f"{field}.{key}"))
    elif isinstance(expected, list):
        if len(value) != len(expected):
            errors.append(f"{field}: fixed item count mismatch")
        for index, (item, target) in enumerate(zip(value, expected)):
            errors.extend(exact_errors(item, target, f"{field}[{index}]"))
    elif value != expected:
        errors.append(f"{field}: differs from fixed host data")
    return sorted(errors)


def project_ui(document, actions=None):
    # No partially accepted records, execution, Config access or plugin loading.
    errors = exact_errors(document, fixed_ui_document(actions), "ui")
    if errors:
        raise ValueError("; ".join(errors))
    return deepcopy(document)


def _unique_pairs(pairs):
    # Reject ambiguous JSON before projection.
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"ui.{key}: duplicate field")
        result[key] = value
    return result


def load_ui_projection(path):
    # The Bottle endpoint serves only this validated bundled host asset.
    document = json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs
    )
    return project_ui(document)
