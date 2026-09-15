import json
import os
import re
import sys
import tempfile
import tomllib
from argparse import ArgumentParser
from pathlib import Path

from plugin_abi_matrix import source_matrices
from plugin_parity_json import load_json, validate_schema, write_json

from ohmymeme.core.plugins.manifest import CANONICAL_BY_ID, ENTRY_POINT_GROUP
from ohmymeme.presentation.desktop.api.ui_contributions import (
    exact_errors,
    fixed_action_matrix,
    fixed_ui_document,
    load_ui_projection,
    project_ui,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas/plugin/ui-contribution.schema.json"
ASSET = ROOT / "src/webui/plugin-ui.json"
RUNTIME = ROOT / "src/webui/settings.js"


def characterize(actions):
    # Capture the original HTML/ABI independently, never the new projection output.
    html = (ROOT / "src/webui/settings.html").read_text("utf-8")
    rows = re.findall(
        r'<button class="import-row" onclick="(\w+)\(\)">(.*?)</button>',
        html,
        re.S,
    )
    source_rows = []
    contributions = []
    for (provider, icon, screen), (handler, body) in zip(
        (
            ("source.qqnt", "qq", "qqnt"),
            ("source.telegram", "telegram", "telegram"),
            ("source.douyin", "douyin", "douyin"),
            ("source.wechat", "wechat", "wechat"),
        ),
        rows[1:],
        strict=True,
    ):
        label = re.search(r'<span class="import-name">(.*?)</span>', body)[1]
        source_rows.append(
            {
                "label": label,
                "handler": handler,
                "svg": re.search(r"<svg.*?</svg>", body, re.S)[0],
            }
        )
        contributions.append(
            {
                "provider": provider,
                "label": label,
                "icon": icon,
                "screen": screen,
                "handler": handler,
            }
        )
    options = re.search(r'<select id="s-sync-type".*?</select>', html, re.S)[0]
    sync_rows = re.findall(r'<option value="(\w+)">(.*?)</option>', options)
    for screen, label in sync_rows:
        contributions.append(
            {
                "provider": "sync." + screen,
                "label": label,
                "icon": "none",
                "screen": screen,
                "handler": "toggleSyncType",
            }
        )
    label = re.search(
        r'data-group="network">\s*<div class="section-title">(.*?)</div>', html
    )[1]
    contributions.append(
        {
            "provider": "transport.lan",
            "label": label,
            "icon": "none",
            "screen": "lan",
            "handler": "toggleLan",
        }
    )
    for contribution in contributions:
        contribution["actions"] = [
            {
                key: row[key]
                for key in ("action", "label", "arguments", "progress_fields")
            }
            for row in actions["actions"]
            if row["surface"] == "settings"
            and contribution["provider"] in row["providers"]
        ]
    return {"schema_version": 1, "contributions": contributions}, {
        "source_rows": source_rows,
        "sync_options": sync_rows,
        "lan_label": label,
        "excluded_mobile_qq": rows[0],
        "action_matrix": actions,
    }


def ui_schema():
    # A closed schema for this fixed document, not a renderer or extensible UI DSL.
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://ohmymeme.local/schemas/plugin/ui-contribution.schema.json",
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "contributions"],
        "properties": {
            "schema_version": {"type": "integer", "const": 1},
            "contributions": {"type": "array", "minItems": 9, "maxItems": 9},
        },
        "const": fixed_ui_document(),
    }


def check(actions_path, fixture_path):
    # Observe both the library projection and the exact asset used by the host route.
    errors, projected, bundled = [], None, None
    actions = load_json(actions_path)
    fixture = load_json(fixture_path)
    schema = load_json(SCHEMA)
    errors.extend(exact_errors(actions, source_matrices()[1], "actions"))
    errors.extend(exact_errors(actions, fixed_action_matrix(), "host_actions"))
    errors.extend(exact_errors(schema, ui_schema(), "schema"))
    errors.extend(
        validate_schema(
            fixture,
            {key: value for key, value in schema.items() if key != "const"},
            schema,
            "ui",
        )
    )
    errors.extend(exact_errors(fixture, schema["const"], "schema.ui"))
    for provider, (_, package, _) in CANONICAL_BY_ID.items():
        metadata = tomllib.loads(
            (ROOT / "plugins" / provider / "pyproject.toml").read_text("utf-8")
        )
        if metadata["project"]["entry-points"][ENTRY_POINT_GROUP] != {
            provider: f"{package}:create_plugin"
        }:
            errors.append(f"metadata.{provider}.entry_point: mismatch")
    try:
        candidate = project_ui(fixture, actions)
    except ValueError as error:
        errors.append(str(error))
    if not errors:
        projected = candidate
        bundled = load_ui_projection(ASSET)
        errors.extend(exact_errors(bundled, projected, "host_asset"))
        entry = ROOT / "src/ohmymeme/presentation/frontend/settings/entry.mjs"
        modules = re.findall(r'"([^"\n]+\.js)"', entry.read_text("utf-8"))
        prefix = (
            "/* Generated host-owned UI data; no plugin code. */\n"
            "const fixedPluginUI = "
        )
        assembled = (
            prefix
            + json.dumps(load_json(ASSET), ensure_ascii=False, separators=(",", ":"))
            + ";\n"
        )
        assembled += "\n".join(
            (entry.parent / name).read_text("utf-8") for name in modules
        )
        if RUNTIME.read_text("utf-8") != assembled:
            errors.append("settings.js: stale assembled runtime")
    return {
        "schema_version": 1,
        "status": "REJECTED" if errors else "PASS",
        "errors": sorted(set(errors)),
        "library_projection": None if errors else projected,
        "host_asset_projection": None if errors else bundled,
        "consumer": (
            "WebUI._setup_bottle /api/plugin-ui -> initSettings -> installPluginUI"
            " -> api/toggleSyncType/progress polls"
        ),
        "execution": {
            "provider_calls": [],
            "bridge_calls": [],
            "dom_calls": [],
            "scope": (
                "validation and JSON projection only; "
                "frontend execution tested separately"
            ),
        },
    }


def _rejected_report(error):
    # Build a fresh invocation report instead of extending any existing file.
    message = str(error)
    return {
        "schema_version": 1,
        "status": "REJECTED",
        "verdict": "changes_requested",
        "errors": [message],
        "exception": {"type": type(error).__name__, "message": message},
        "library_projection": None,
        "host_asset_projection": None,
        "consumer": (
            "WebUI._setup_bottle /api/plugin-ui -> initSettings -> installPluginUI"
            " -> api/toggleSyncType/progress polls"
        ),
        "execution": {
            "provider_calls": [],
            "bridge_calls": [],
            "dom_calls": [],
            "scope": (
                "validation and JSON projection only; "
                "frontend execution tested separately"
            ),
        },
    }


def _write_report(path, report):
    # Replace the target only after the complete structured report is flushed.
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    data = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(data + "\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main(argv=None):
    # Never use fixture verdicts, and replace stale PASS reports on every rejection.
    parser = ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write-host", action="store_true")
    mode.add_argument("--characterize", action="store_true")
    parser.add_argument("--actions", type=Path, required=True)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.check and (args.fixture is None or args.report is None):
        parser.error("--check requires --fixture and --report")
    try:
        if args.characterize:
            fixture, snapshot = characterize(load_json(args.actions))
            write_json(args.fixture, fixture)
            write_json(args.report, snapshot)
            return 0
        if args.write_host:
            actions = load_json(args.actions)
            errors = exact_errors(actions, source_matrices()[1], "actions")
            if errors:
                raise ValueError("; ".join(errors))
            write_json(ASSET, fixed_ui_document(actions))
            write_json(SCHEMA, ui_schema())
            return 0
        report = check(args.actions, args.fixture)
    except Exception as error:
        report = _rejected_report(error)
    report["verdict"] = (
        "approved" if report["status"] == "PASS" else "changes_requested"
    )
    if "exception" in report:
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "verdict": report["verdict"],
                    "exception": report["exception"],
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
    _write_report(args.report, report)
    print(
        json.dumps(
            {"status": report["status"], "errors": report["errors"]}, ensure_ascii=False
        )
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
