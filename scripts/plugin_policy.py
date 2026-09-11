# pyright: basic

import importlib
import sys
from pathlib import Path

from plugin_parity_json import load_json, validate_schema, write_json

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SCHEMA_PATH = ROOT / "schemas" / "plugin" / "secret-compatibility.schema.json"
manifest = importlib.import_module("ohmymeme.core.plugins.manifest")
policy = importlib.import_module("ohmymeme.core.plugins.policy")


class PolicyValidationError(ValueError):
    def __init__(self, errors):
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


def _parse_arguments(argv):
    matrix = None
    fixture = None
    report = None
    index = 0
    while index < len(argv):
        option = argv[index]
        if option == "--check":
            index += 1
            continue
        if option in ("--matrix", "--fixture", "--report"):
            if index + 1 >= len(argv):
                raise PolicyValidationError((f"{option}: missing value",))
            value = Path(argv[index + 1])
            if option == "--matrix":
                matrix = value
            elif option == "--fixture":
                fixture = value
            else:
                report = value
            index += 2
            continue
        if option == "--help":
            print(
                "usage: plugin_policy.py --check --matrix MATRIX "
                "--fixture FIXTURE --report REPORT"
            )
            raise SystemExit(0)
        raise PolicyValidationError((f"unknown option: {option}",))
    if matrix is None or fixture is None or report is None:
        raise PolicyValidationError(("--matrix, --fixture and --report are required",))
    return matrix, fixture, report


def _expected_settings(provider_id):
    return [
        {"key": key, "legacy_key": legacy_key, "secret": secret}
        for key, (legacy_key, secret) in policy.PLUGIN_SETTINGS[provider_id].items()
    ]


def _validate_matrix(value):
    schema = load_json(SCHEMA_PATH)
    errors = validate_schema(value, schema, schema, "")
    providers = value.get("providers") if isinstance(value, dict) else None
    expected_ids = tuple(item[0] for item in manifest.CANONICAL_PROVIDERS)
    if not isinstance(providers, list):
        return errors
    ids = tuple(row.get("id") if isinstance(row, dict) else None for row in providers)
    if ids != expected_ids:
        errors.append("matrix.providers: expected canonical provider ID order")
    for index, row in enumerate(providers):
        if not isinstance(row, dict):
            continue
        provider_id = row.get("id")
        if provider_id not in policy.PLUGIN_SETTINGS:
            continue
        prefix = f"matrix.providers[{index}]"
        if row.get("namespace") != f"plugins.{provider_id}":
            errors.append(f"{prefix}.namespace: expected plugins.{provider_id}")
        if row.get("settings") != _expected_settings(provider_id):
            errors.append(f"{prefix}.settings: expected runtime configuration mapping")
        for setting_index, setting in enumerate(row.get("settings", ())):
            if isinstance(setting, dict) and type(setting.get("secret")) is not bool:
                errors.append(
                    f"{prefix}.settings[{setting_index}].secret: expected boolean"
                )
    if value.get("lan_export_excluded") != ["lan_secret"]:
        errors.append("matrix.lan_export_excluded: expected ['lan_secret']")
    return errors


def _operation_errors(value, matrix):
    errors = []
    if not isinstance(value, dict):
        return ["policy: expected object"]
    if set(value) != {"operations", "schema_version"}:
        errors.append("policy: expected only operations and schema_version")
    if type(value.get("schema_version")) is not int or value.get("schema_version") != 1:
        errors.append("policy.schema_version: expected 1")
    operations = value.get("operations")
    if not isinstance(operations, list):
        return errors + ["policy.operations: expected array"]
    for index, operation in enumerate(operations):
        prefix = f"policy.operations[{index}]"
        if not isinstance(operation, dict):
            errors.append(f"{prefix}: expected object")
            continue
        for key in sorted(
            set(operation)
            - {
                "capability",
                "config",
                "lan_export",
                "output",
                "provider",
                "temporary_path",
            }
        ):
            errors.append(f"{prefix}.{key}: unexpected property")
        provider_id = operation.get("provider")
        if provider_id not in policy.PLUGIN_SETTINGS:
            errors.append(f"{prefix}.provider: unknown plugin ID {provider_id!r}")
            continue
        allowed = manifest.CANONICAL_BY_ID[provider_id][2]
        config = operation.get("config")
        if config is not None:
            if not isinstance(config, dict):
                errors.append(f"{prefix}.config: expected object")
            elif config.get("provider") != provider_id:
                errors.append(f"{prefix}.config: cross-provider config access")
            elif config.get("key") not in policy.PLUGIN_SETTINGS[provider_id]:
                errors.append(f"{prefix}.config.key: unknown config key")
        capability = operation.get("capability")
        if capability is not None and capability not in allowed:
            errors.append(f"{prefix}.capability: undeclared capability {capability!r}")
        temporary_path = operation.get("temporary_path")
        if temporary_path is not None:
            candidate = (
                Path(temporary_path) if isinstance(temporary_path, str) else None
            )
            if candidate is None or candidate.is_absolute() or ".." in candidate.parts:
                errors.append(f"{prefix}.temporary_path: temporary path traversal")
        output = operation.get("output")
        if output is not None:
            if not isinstance(output, dict):
                errors.append(f"{prefix}.output: expected object")
            else:
                secret_key = output.get("secret_key")
                setting = policy.PLUGIN_SETTINGS[provider_id].get(secret_key)
                if setting is None or not setting[1]:
                    errors.append(f"{prefix}.output.secret_key: unknown secret key")
                elif output.get("value") != "[REDACTED]":
                    errors.append(f"{prefix}.output: secret output must be redacted")
        lan_export = operation.get("lan_export")
        if lan_export is not None:
            excluded = matrix["lan_export_excluded"]
            for key in lan_export if isinstance(lan_export, list) else ():
                if key in excluded:
                    errors.append(
                        f"{prefix}.lan_export: LAN export includes secret key {key!r}"
                    )
            if not isinstance(lan_export, list):
                errors.append(f"{prefix}.lan_export: expected array")
    return errors


def check(matrix_path, fixture_path, report):
    matrix = load_json(matrix_path)
    fixture = load_json(fixture_path)
    errors = _validate_matrix(matrix)
    errors.extend(_operation_errors(fixture, matrix))
    if errors:
        raise PolicyValidationError(errors)
    write_json(
        report,
        {
            "plugin_ids": [item[0] for item in manifest.CANONICAL_PROVIDERS],
            "status": "PASS",
        },
    )
    print("PASS: validated plugin configuration policy")


def main(argv=None):
    report = None
    try:
        matrix, fixture, report = _parse_arguments(
            sys.argv[1:] if argv is None else argv
        )
        check(matrix, fixture, report)
    except (OSError, PolicyValidationError, ValueError) as error:
        errors = getattr(error, "errors", (str(error),))
        if report is not None:
            write_json(report, {"errors": list(errors), "status": "REJECTED"})
        print(f"REJECTED: {errors[0]}", file=sys.stderr)
        for message in errors[1:]:
            print(f"REJECTED: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
