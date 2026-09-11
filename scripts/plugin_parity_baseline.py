# pyright: basic

import hashlib
import json
import runpy
import sys
from pathlib import Path

from baseline_contracts import sha256_path

cli_module = runpy.run_path(str(Path(__file__).with_name("plugin_parity_cli.py")))
parse_arguments = cli_module["parse_arguments"]
json_module = runpy.run_path(str(Path(__file__).with_name("plugin_parity_json.py")))
write_json = json_module["write_json"]

schema_module = runpy.run_path(str(Path(__file__).with_name("plugin_parity_schema.py")))
PROVIDER_IDS = schema_module["PROVIDER_IDS"]
load_json = schema_module["load_json"]
load_provider_fixtures = schema_module["load_provider_fixtures"]
validate_schema = schema_module["validate_schema"]
validate_ui_baseline = schema_module["validate_ui_baseline"]
validate_matrix = schema_module["validate_matrix"]
append_error = schema_module["append_error"]

ALLOWED_VARIANCE = ["ids", "timestamps", "temporary paths", "thread ordering"]
TODO_1_REFERENCE_PATHS = schema_module["TODO_1_REFERENCE_PATHS"]


# 校验基线 schema 与九 provider 语义约束。
def validate_baseline(
    value, schema, provider_schema, repo_root, fixture_root, matrix_path, ui_override
):
    errors = validate_schema(value, schema, schema, "")
    fixtures, fixture_errors = load_provider_fixtures(repo_root, fixture_root)
    errors.extend(fixture_errors)
    providers = value.get("providers")
    found = (
        [
            provider.get("id") if isinstance(provider, dict) else None
            for provider in providers
        ]
        if isinstance(providers, list)
        else []
    )
    if found != list(PROVIDER_IDS):
        missing = [
            provider_id for provider_id in PROVIDER_IDS if provider_id not in found
        ]
        unknown = [
            provider_id for provider_id in found if provider_id not in PROVIDER_IDS
        ]
        if missing:
            errors.append(f"providers: missing provider {missing[0]}")
        if unknown:
            errors.append(f"providers: unknown provider {unknown[0]}")
        if len(found) == len(PROVIDER_IDS) and not missing and not unknown:
            errors.append("providers: canonical order mismatch or duplicate provider")
    if isinstance(providers, list):
        fixture_by_id = {fixture.get("id"): fixture for fixture in fixtures}
        for index, provider in enumerate(providers):
            if not isinstance(provider, dict):
                continue
            provider_id = provider.get("id")
            prefix = f"providers[{index}]"
            expected_distribution = ""
            expected_package_root = ""
            if isinstance(provider_id, str):
                expected_category = provider_id.split(".", 1)[0]
                expected_distribution = {
                    "source": f"ohmymeme-plugin-{provider_id.split('.', 1)[1]}",
                    "sync": f"ohmymeme-plugin-sync-{provider_id.split('.', 1)[1]}",
                    "transport": "ohmymeme-plugin-lan",
                }.get(expected_category, "")
                expected_package_root = expected_distribution.replace(
                    "ohmymeme-plugin-", "ohmymeme_plugin_"
                ).replace("-", "_")
                append_error(
                    errors,
                    provider.get("category") != expected_category,
                    f"{prefix}.category: expected {expected_category}",
                )
                if provider.get("entry_point") != (
                    f"ohmymeme.plugins.v1:{provider_id} = "
                    f"{expected_package_root}:create_plugin"
                ):
                    errors.append(
                        f"{prefix}.entry_point: expected canonical entry point"
                    )
            if provider_id in fixture_by_id:
                fixture = fixture_by_id[provider_id]
                for field in (
                    "flow",
                    "state",
                    "paths",
                    "protocols",
                    "package_root",
                    "license_provenance",
                    "allowed_variance",
                ):
                    if provider.get(field) != fixture.get(field):
                        errors.append(
                            f"{prefix}.{field}: differs from provider fixture"
                        )
                append_error(
                    errors,
                    provider.get("state")
                    != provider_schema["x-canonical-states"].get(provider_id),
                    f"{prefix}.state: differs from canonical source states",
                )
                append_error(
                    errors,
                    provider.get("current_identifier")
                    != fixture.get("current_identifier"),
                    f"{prefix}.current_identifier: differs from provider fixture",
                )
                abi = provider.get("abi")
                if isinstance(abi, dict):
                    append_error(
                        errors,
                        abi.get("aliases") != fixture.get("abi_aliases"),
                        f"{prefix}.abi.aliases: differs from provider fixture",
                    )
                    append_error(
                        errors,
                        abi.get("actions")
                        != provider_schema["x-canonical-actions"].get(provider_id),
                        f"{prefix}.abi.actions: differs from canonical source actions",
                    )
            if (
                isinstance(provider_id, str)
                and provider.get("distribution") != expected_distribution
            ):
                errors.append(f"{prefix}.distribution: expected canonical distribution")
            if (
                expected_package_root
                and provider.get("package_root") != expected_package_root
            ):
                errors.append(f"{prefix}.package_root: expected canonical package root")
            if isinstance(provider_id, str) and (
                provider_id.startswith("adb.") or provider_id == "qq.mobile"
            ):
                errors.append(f"{prefix}.id: ADB/QQ is not a plugin provider")
    if value.get("allowed_variance") != ALLOWED_VARIANCE:
        errors.append("allowed_variance: expected canonical variance fields")
    excluded = value.get("excluded_plugins")
    excluded_is_valid = isinstance(excluded, list) and all(
        isinstance(item, str) for item in excluded
    )
    if not excluded_is_valid or sorted(excluded) != ["adb.qq", "qq.mobile"]:
        errors.append("excluded_plugins: must contain only adb.qq and qq.mobile")
    source_references = value.get("source_references")
    if isinstance(source_references, list):
        for relative_path in source_references:
            if (
                isinstance(relative_path, str)
                and not (repo_root / relative_path).exists()
            ):
                errors.append(
                    f"source_references: missing file or directory {relative_path}"
                )
            elif not isinstance(relative_path, str):
                errors.append("source_references: path must be a string")
    ui_path = ui_override or value.get("ui_baseline")
    if isinstance(ui_path, (str, Path)):
        errors.extend(validate_ui_baseline(repo_root, ui_path, ALLOWED_VARIANCE))
    errors.extend(validate_matrix(matrix_path, value))
    return errors


# 生成基线校验报告。
def check_baseline(repo_root, input_path, fixture_root, matrix_override, ui_override):
    value = load_json(input_path)
    schema = load_json(repo_root / "schemas/plugin-parity/baseline.schema.json")
    provider_schema = load_json(
        repo_root / "schemas/plugin-parity/provider-fixture.schema.json"
    )
    matrix_path = matrix_override or repo_root / "docs/plugin-provider-matrix.json"
    errors = validate_baseline(
        value,
        schema,
        provider_schema,
        repo_root,
        fixture_root,
        matrix_path,
        ui_override,
    )
    hash_paths = [
        repo_root / name
        for name in (
            "scripts/plugin_parity_baseline.py",
            "scripts/baseline_contracts.py",
            "scripts/plugin_parity_schema.py",
            "scripts/plugin_parity_json.py",
            "scripts/plugin_parity_cli.py",
            "schemas/plugin-parity/baseline.schema.json",
            "schemas/plugin-parity/provider-fixture.schema.json",
        )
    ] + [input_path, matrix_path]
    ui_hash_path = ui_override or value.get("ui_baseline")
    if ui_hash_path:
        hash_paths.append(repo_root / ui_hash_path)
    hash_paths += [
        fixture_root / f"{provider_id.replace('.', '-')}.json"
        for provider_id in PROVIDER_IDS
    ]
    hash_paths += [
        repo_root / relative_path
        for provider_id in PROVIDER_IDS
        for relative_path in schema_module["SOURCE_PATHS"][provider_id]
    ]
    hash_paths += [
        repo_root / relative_path for relative_path in TODO_1_REFERENCE_PATHS
    ]
    hash_paths = list(dict.fromkeys(path.resolve() for path in hash_paths))
    input_hashes = {
        path.resolve().relative_to(repo_root).as_posix(): sha256_path(path)
        for path in hash_paths
    }
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "schema_version": 1,
        "verdict": "pass" if not errors else "changes_requested",
        "provider_ids": list(PROVIDER_IDS),
        "provider_count": (
            len(value.get("providers", []))
            if isinstance(value.get("providers"), list)
            else 0
        ),
        "input_sha256": hashlib.sha256(canonical).hexdigest(),
        "errors": errors,
        "input_hashes": input_hashes,
    }


# 执行离线校验并返回退出码。
def main():
    try:
        check, fixture_root, report_path, matrix_override, ui_override = (
            parse_arguments(sys.argv[1:])
        )
        repo_root = Path.cwd()
        if check is None:
            raise ValueError("--check: missing required option")
        report = check_baseline(
            repo_root, check, fixture_root, matrix_override, ui_override
        )
        write_json(report_path, report)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    if report.get("verdict") == "changes_requested":
        for error in report.get("errors", []):
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(f"PASS: validated {report['provider_count']} providers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
