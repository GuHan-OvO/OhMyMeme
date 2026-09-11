# pyright: basic

import re
import runpy
from pathlib import Path

json_module = runpy.run_path(str(Path(__file__).with_name("plugin_parity_json.py")))
load_json = json_module["load_json"]
validate_schema = json_module["validate_schema"]

PROVIDER_IDS = (
    "source.qqnt",
    "source.telegram",
    "source.douyin",
    "source.wechat",
    "sync.ftp",
    "sync.s3",
    "sync.r2",
    "sync.webdav",
    "transport.lan",
)
SOURCE_PATHS = dict.fromkeys(
    ("sync.ftp", "sync.s3", "sync.r2", "sync.webdav"),
    ["src/ohmymeme/services/sync/backends.py"],
) | {
    "source.qqnt": ["src/ohmymeme/integrations/imports/qqnt.py"],
    "source.telegram": ["src/ohmymeme/integrations/imports/telegram.py"],
    "source.douyin": ["src/ohmymeme/integrations/imports/douyin.py"],
    "source.wechat": ["src/ohmymeme/integrations/imports/wechat.py"],
    "transport.lan": [
        "src/ohmymeme/services/lan/protocol.py",
        "src/ohmymeme/services/lan/server.py",
        "src/ohmymeme/services/lan/commands.py",
    ],
}
SOURCE_MARKERS = {
    "source.qqnt": ("get_extract_status",),
    "source.telegram": ("start_tg_import",),
    "source.douyin": ("start_douyin_import",),
    "source.wechat": ("start_wechat_import",),
    "sync.ftp": ("class _FtpBackend",),
    "sync.s3": ("class _S3Backend",),
    "sync.r2": ("class _R2Backend",),
    "sync.webdav": ("class _WebDAVBackend",),
    "transport.lan": ("class LanServer", "def derive_key"),
}
UI_SELECTOR_SOURCES = {
    "#titlebar": (
        "src/ohmymeme/presentation/frontend/main/app/MainWindow.vue",
        "src/webui/settings.html",
    ),
    "#search-wrap": ("src/ohmymeme/presentation/frontend/main/app/MainWindow.vue",),
    "#tagbar": ("src/ohmymeme/presentation/frontend/main/app/MainWindow.vue",),
    "#meme-grid": ("src/ohmymeme/presentation/frontend/main/app/MainWindow.vue",),
    "#toast": (
        "src/ohmymeme/presentation/frontend/main/app/MainWindow.vue",
        "src/webui/settings.html",
    ),
    "#settings-nav": ("src/webui/settings.html",),
    "#settings-content": ("src/webui/settings.html",),
}
TODO_1_REFERENCE_PATHS = [
    "src/ohmymeme/integrations/imports",
    "src/ohmymeme/services/sync",
    "src/ohmymeme/services/lan",
    "src/ohmymeme/core/schemas/bridge.py",
    "scripts/package_smoke.py",
    "README.md",
    "LICENSE",
    "setup.py",
    "src/ohmymeme/presentation/frontend/main/app/MainWindow.vue",
    "src/webui/settings.html",
]


def append_error(errors, condition, message):
    if condition:
        errors.append(message)


def selector_present(selector, source):
    return (
        selector.startswith("#")
        and re.search(rf"\bid\s*=\s*['\"]{re.escape(selector[1:])}['\"]", source)
        is not None
    )


def validate_provider_fixture(provider_id, fixture, schema, repo_root):
    errors = validate_schema(fixture, schema, schema, f"fixtures[{provider_id}]")
    append_error(
        errors,
        fixture.get("id") != provider_id,
        f"fixtures[{provider_id}].id: expected {provider_id}",
    )
    append_error(
        errors,
        fixture.get("state") != schema["x-canonical-states"].get(provider_id),
        f"fixtures[{provider_id}].state: differs from canonical source states",
    )
    source_contents = []
    for relative_path in SOURCE_PATHS[provider_id]:
        source_path = repo_root / relative_path
        if not source_path.is_file():
            errors.append(
                f"fixtures[{provider_id}].source: missing file {relative_path}"
            )
        else:
            source_contents.append(source_path.read_text(encoding="utf-8"))
    for marker in SOURCE_MARKERS.get(provider_id, ()):
        if not any(marker in content for content in source_contents):
            errors.append(
                f"fixtures[{provider_id}].source: missing source marker {marker}"
            )
    return errors


def load_provider_fixtures(repo_root, fixture_root):
    schema = load_json(repo_root / "schemas/plugin-parity/provider-fixture.schema.json")
    fixtures, errors = [], []
    for provider_id in PROVIDER_IDS:
        path = fixture_root / f"{provider_id.replace('.', '-')}.json"
        if not path.is_file():
            errors.append(f"fixtures[{provider_id}]: missing fixture file")
            continue
        fixture = load_json(path)
        errors.extend(
            validate_provider_fixture(provider_id, fixture, schema, repo_root)
        )
        fixtures.append(fixture)
    return fixtures, errors


def validate_ui_baseline(repo_root, relative_path, allowed_variance):
    errors, path = [], Path(relative_path)
    if path.is_absolute():
        return ["ui_baseline: absolute path is not allowed"]
    try:
        resolved = (repo_root / path).resolve()
        resolved.relative_to(repo_root.resolve())
    except ValueError:
        return ["ui_baseline: path escapes repository root"]
    if not resolved.is_file():
        return [f"ui_baseline: missing file {relative_path}"]
    value = load_json(resolved)
    for field in (
        "schema_version",
        "viewport",
        "theme",
        "surface",
        "fixed_runtime",
        "selectors",
        "allowed_variance",
    ):
        append_error(
            errors, field not in value, f"ui_baseline.{field}: missing required field"
        )
    for field, expected in {
        "schema_version": 1,
        "viewport": "960x640",
        "theme": "dark",
        "fixed_runtime": "chromium",
    }.items():
        if value.get(field) != expected:
            errors.append(f"ui_baseline.{field}: expected {expected}")
    surface = "main-window-and-settings-window-fixed-shell"
    selectors_expected = (
        "#titlebar|#search-wrap|#tagbar|#meme-grid|"
        "#toast|#settings-nav|#settings-content"
    ).split("|")
    if value.get("surface") != surface:
        errors.append(f"ui_baseline.surface: expected {surface}")
    selectors = value.get("selectors")
    if selectors != selectors_expected:
        errors.append(
            "ui_baseline.selectors: expected exact fixed main/settings selector list"
        )
    for selector in selectors_expected:
        for source in UI_SELECTOR_SOURCES[selector]:
            source_path = repo_root / source
            if not source_path.is_file():
                errors.append(
                    f"ui_baseline.selector {selector}: missing source {source}"
                )
            elif not selector_present(
                selector, source_path.read_text(encoding="utf-8")
            ):
                errors.append(f"ui_baseline.selector {selector}: missing in {source}")
    if value.get("allowed_variance") != allowed_variance:
        errors.append(
            "ui_baseline.allowed_variance: expected canonical variance fields"
        )
    return errors


def validate_matrix(path, baseline):
    errors = []
    if not path.is_file():
        return [f"matrix: missing file {path}"]
    matrix = load_json(path)
    version = matrix.get("schema_version")
    if type(version) is not int or version != 1:
        errors.append("matrix.schema_version: expected 1")
    providers, baseline_providers = matrix.get("providers"), baseline.get("providers")
    if not isinstance(providers, list):
        return errors + ["matrix.providers: missing or not an array"]
    found = [
        provider.get("id") if isinstance(provider, dict) else None
        for provider in providers
    ]
    append_error(
        errors,
        found != list(PROVIDER_IDS),
        "matrix.providers: canonical provider IDs/order mismatch",
    )
    baseline_items = baseline_providers if isinstance(baseline_providers, list) else []
    expected_by_id = {
        provider.get("id"): provider
        for provider in baseline_items
        if isinstance(provider, dict)
    }
    for index, provider in enumerate(providers):
        if not isinstance(provider, dict):
            continue
        provider_id = provider.get("id")
        expected = expected_by_id.get(provider_id)
        if not isinstance(expected, dict):
            continue
        source_paths = SOURCE_PATHS.get(str(provider_id), [])
        if provider_id == "transport.lan":
            source_paths = source_paths[:2]
        expected_source = "; ".join(source_paths)
        if not isinstance(provider.get("source"), str):
            errors.append(f"matrix.providers[{index}].source: expected string")
        elif provider.get("source") != expected_source:
            errors.append(
                f"matrix.providers[{index}].source: differs from source mapping"
            )
        for matrix_field, baseline_field in (
            ("distribution", "distribution"),
            ("entry_point", "entry_point"),
        ):
            if provider.get(matrix_field) != expected.get(baseline_field):
                errors.append(
                    f"matrix.providers[{index}].{matrix_field}: differs from baseline"
                )
        append_error(
            errors,
            provider.get("license") != expected.get("license_provenance"),
            f"matrix.providers[{index}].license: differs from baseline",
        )
        append_error(
            errors,
            provider.get("variance") != expected.get("allowed_variance"),
            f"matrix.providers[{index}].variance: differs from baseline",
        )
    append_error(
        errors,
        matrix.get("excluded_plugins") != ["adb.qq", "qq.mobile"],
        "matrix.excluded_plugins: must contain only adb.qq and qq.mobile",
    )
    return errors
