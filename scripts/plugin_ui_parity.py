# pyright: basic

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from offline_fixture_runner import (
    FixturePolicyError,
    FixtureRunner,
    load_json,
    require_offline_flags,
)
from plugin_parity_json import validate_schema

ROOT = Path(__file__).resolve().parents[1]
PARITY_SCHEMA = ROOT / "schemas" / "plugin" / "parity-report.schema.json"
VARIANCE = ["ids", "timestamps", "temporary paths", "thread ordering"]
RUN_ID_SCHEMA = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
SETTINGS_SELECTORS = ("#titlebar", "#settings-nav", "#settings-content", "#toast")
MAIN_SELECTORS = ("#titlebar", "#search-wrap", "#tagbar", "#meme-grid", "#toast")


# Serialize reports and source projection snapshots in one stable JSON representation.
def canonical_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


# Hash an exact input file or source tree without editable-install residue.
def sha256_path(path):
    path = Path(path)
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(path.read_bytes())
        return digest.hexdigest()
    if not path.is_dir():
        raise ValueError(f"hash input missing: {path}")
    for item in sorted(
        candidate
        for candidate in path.rglob("*")
        if candidate.is_file()
        and "__pycache__" not in candidate.parts
        and not any(part.endswith(".egg-info") for part in candidate.parts)
    ):
        digest.update(item.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


# Render a worktree-relative path for portable screenshot evidence.
def relative_path(path):
    path = Path(path).resolve()
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


# Fail closed when the local fixed Chromium capture program is unavailable.
def require_local_capture_script(path=None):
    script = Path(path or ROOT / "scripts" / "plugin_ui_capture.mjs")
    if not script.is_file():
        raise ValueError("chromium blocker: missing local Chromium capture script")
    return script.resolve()


# Verify the named report schema is the fixed Todo17 schema document.
def validate_parity_schema(schema=None):
    schema = schema or load_json(PARITY_SCHEMA)
    errors = []
    if (
        schema.get("$id")
        != "https://ohmymeme.local/schemas/plugin/parity-report.schema.json"
    ):
        errors.append("parity schema.$id: unexpected value")
    if schema.get("properties", {}).get("schema_version", {}).get("const") != 1:
        errors.append("parity schema.schema_version: expected 1")
    if "run_id" not in schema.get("required", []):
        errors.append("parity schema.run_id: missing required field")
    if schema.get("properties", {}).get("run_id") != RUN_ID_SCHEMA:
        errors.append("parity schema.run_id: expected sha256 string schema")
    return errors


# Validate a report against the closed Todo17 schema before semantic checks.
def validate_report_schema(value):
    schema = load_json(PARITY_SCHEMA)
    return validate_parity_schema(schema) + validate_schema(value, schema, schema, "")


# Fingerprint every current host/UI input so stale browser reports are rejected.
def capture_inputs(fixture_root, policy_path):
    paths = {
        "schemas/plugin/parity-report.schema.json": PARITY_SCHEMA,
        "scripts/offline_fixture_runner.py": ROOT
        / "scripts"
        / "offline_fixture_runner.py",
        "scripts/plugin_ui_capture.mjs": ROOT / "scripts" / "plugin_ui_capture.mjs",
        "scripts/plugin_ui_parity.py": ROOT / "scripts" / "plugin_ui_parity.py",
        "src/ohmymeme/presentation/desktop/api/ui_contributions.py": ROOT
        / "src"
        / "ohmymeme"
        / "presentation"
        / "desktop"
        / "api"
        / "ui_contributions.py",
        "src/ohmymeme/presentation/frontend/main/app/MainWindow.vue": ROOT
        / "src"
        / "ohmymeme"
        / "presentation"
        / "frontend"
        / "main"
        / "app"
        / "MainWindow.vue",
        "src/webui/plugin-ui.json": ROOT / "src" / "webui" / "plugin-ui.json",
        "src/webui/settings.css": ROOT / "src" / "webui" / "settings.css",
        "src/webui/settings.html": ROOT / "src" / "webui" / "settings.html",
        "src/webui/settings.js": ROOT / "src" / "webui" / "settings.js",
        relative_path(policy_path): Path(policy_path),
    }
    return {name: sha256_path(path) for name, path in sorted(paths.items())}


# Bind each capture or comparison to its complete, exact input-hash set.
def report_run_id(kind, variant, inputs):
    return hashlib.sha256(
        canonical_bytes({"kind": kind, "variant": variant, "input_hashes": inputs})
    ).hexdigest()


# Read template selector facts from actual host sources, never a DOM fixture.
def source_selector_facts():
    main = (
        ROOT
        / "src"
        / "ohmymeme"
        / "presentation"
        / "frontend"
        / "main"
        / "app"
        / "MainWindow.vue"
    )
    settings = ROOT / "src" / "webui" / "settings.html"
    main_text = main.read_text(encoding="utf-8")
    settings_text = settings.read_text(encoding="utf-8")

    def present(source, selectors):
        return [
            selector
            for selector in selectors
            if re.search(rf"\bid\s*=\s*['\"]{re.escape(selector[1:])}['\"]", source)
        ]

    return {
        "main": {
            "selectors": present(main_text, MAIN_SELECTORS),
            "source": relative_path(main),
        },
        "settings": {
            "selectors": present(settings_text, SETTINGS_SELECTORS),
            "source": relative_path(settings),
        },
    }


# Keep screenshot binaries outside fixtures while retaining report-relative paths.
def screenshot_directory(output):
    output = Path(output).resolve()
    return (
        ROOT
        / ".omo"
        / "evidence"
        / "pluginized-recomposition-parity"
        / "todo-17-ui"
        / output.stem
    )


# Load the production host projection instead of sending a static plugin UI fixture.
def host_projection():
    from ohmymeme.presentation.desktop.api.ui_contributions import load_ui_projection

    return load_ui_projection(ROOT / "src" / "webui" / "plugin-ui.json")


# Write the actual validated host projection into the runner-owned browser fixture root.
def write_host_projection(runner, projection):
    path = runner.workspace() / "plugin-ui.json"
    path.write_bytes(canonical_bytes(projection) + b"\n")
    return path, hashlib.sha256(canonical_bytes(projection)).hexdigest()


# Run the fixed local Node capture under the runner's exact subprocess allowance.
def run_browser_capture(runner, projection_path, output, viewport, theme):
    script = require_local_capture_script()
    node = shutil.which("node")
    if not node:
        raise ValueError("chromium blocker: managed node executable is unavailable")
    screenshots = screenshot_directory(output)
    command = [
        node,
        str(script),
        "--repo-root",
        str(ROOT),
        "--projection-file",
        str(projection_path),
        "--screenshots-dir",
        str(screenshots),
        "--viewport",
        viewport,
        "--theme",
        theme,
    ]
    probe = runner.policy.get("external_probe")
    if probe and probe["kind"] == "browser-request":
        command.extend(("--external-probe", json.dumps(probe, sort_keys=True)))
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    try:
        value = json.loads(completed.stdout or "")
    except json.JSONDecodeError as error:
        raise ValueError(
            "chromium blocker: local capture emitted invalid JSON: "
            + completed.stderr.strip()
        ) from error
    if not isinstance(value, dict):
        raise ValueError("chromium blocker: local capture expected object")
    if completed.returncode != 0:
        detail = str(value.get("error", "chromium capture failed"))
        if "Executable doesn't exist" in detail:
            detail = (
                "chromium blocker: local Chromium is unavailable; run "
                "npm exec playwright install chromium"
            )
        raise ValueError(detail)
    return value, screenshots


# Capture the real settings host with validated projection and local Chromium.
def capture_ui(args):
    fixture_root = Path(args.fixture_root)
    policy_path = Path(args.policy or fixture_root / "offline-policy.json")
    screenshots = screenshot_directory(args.output)
    runner = FixtureRunner(
        ROOT,
        fixture_root,
        "ui",
        output_paths=(args.output,),
        write_roots=(screenshots,),
        workspace_parent=ROOT / ".omo" / "evidence" / "pluginized-recomposition-parity",
        policy_path=policy_path,
    )
    runner.workspace()
    errors = validate_parity_schema()
    try:
        inputs = capture_inputs(fixture_root, policy_path)
    except (OSError, ValueError) as error:
        inputs = {}
        errors.append(str(error))
    browser = {}
    projection = {}
    projection_hash = ""
    try:
        with runner:
            runner.probe_denials()
            projection = host_projection()
            projection_path, projection_hash = write_host_projection(runner, projection)
            browser, _screenshots = run_browser_capture(
                runner, projection_path, args.output, args.viewport, args.theme
            )
    except (
        FixturePolicyError,
        OSError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        errors.append(str(error))
    finally:
        workspace_removed = runner.cleanup()
    probe = runner.policy.get("external_probe")
    blocked = browser.get("external_requests", []) if isinstance(browser, dict) else []
    if probe and probe["kind"] == "browser-request":
        observed = any(
            item.get("target") == probe["target"] and item.get("verdict") == "blocked"
            for item in blocked
            if isinstance(item, dict)
        )
        if not observed:
            errors.append(
                f"provider {probe['provider']} policy.browser-request: "
                "interception missing"
            )
        elif not probe["must_be_blocked"]:
            errors.append(
                f"provider {probe['provider']} policy.browser-request: "
                "fixture requested prohibited external access"
            )
    if browser.get("console_errors"):
        errors.append("ui.console: browser emitted error output")
    expected_labels = [
        item["label"]
        for item in projection.get("contributions", [])
        if item.get("provider", "").startswith("source.")
    ]
    labels = browser.get("labels", []) if isinstance(browser, dict) else []
    for label in expected_labels:
        if label not in labels:
            errors.append(f"ui.host_projection.label: missing {label}")
    interception = runner.summary()
    errors.extend(interception["policy_errors"])
    interception["trace"].extend(blocked)
    report = {
        "schema_version": 1,
        "kind": "ui_capture",
        "variant": args.variant,
        "verdict": "pass" if not errors else "changes_requested",
        "allowed_variance": VARIANCE,
        "errors": sorted(set(errors)),
        "input_hashes": inputs,
        "capture_inputs": inputs,
        "run_id": report_run_id("ui_capture", args.variant, inputs),
        "interception": interception,
        "viewport": args.viewport,
        "theme": args.theme,
        "browser": browser.get("browser", {"name": args.browser}),
        "browser_diagnostics": {
            "console_errors": browser.get("console_errors", []),
            "external_requests": blocked,
        },
        "ui": {
            "settings": browser.get("settings", {}),
            "plugin_labels": labels,
            "source_selectors": source_selector_facts(),
            "host_projection": {
                "sha256": projection_hash,
                "source": "ui_contributions.load_ui_projection",
                "contribution_count": len(projection.get("contributions", [])),
            },
        },
        "cleanup": {
            "workspace_removed": workspace_removed,
            **browser.get("cleanup", {"browser_closed": False, "server": "unknown"}),
        },
    }
    report["capture_sha256"] = hashlib.sha256(canonical_bytes(report)).hexdigest()
    return report


# Validate one measured UI capture, optionally checking its screenshot bytes on disk.
def validate_ui_capture(
    value, expected_variant=None, expected_inputs=None, verify_files=False
):
    errors = validate_report_schema(value)
    if not isinstance(value, dict):
        return errors + ["ui: expected object"]
    required = {
        "schema_version",
        "kind",
        "variant",
        "verdict",
        "allowed_variance",
        "errors",
        "input_hashes",
        "capture_inputs",
        "interception",
        "viewport",
        "theme",
        "browser",
        "ui",
        "run_id",
        "capture_sha256",
    }
    for field in sorted(required - set(value)):
        errors.append(f"ui.{field}: missing")
    if type(value.get("schema_version")) is not int or value.get("schema_version") != 1:
        errors.append("ui.schema_version: expected 1")
    if value.get("kind") != "ui_capture":
        errors.append("ui.kind: expected ui_capture")
    if value.get("variant") not in ("baseline", "recomposed"):
        errors.append("ui.variant: expected baseline or recomposed")
    if expected_variant and value.get("variant") != expected_variant:
        errors.append(f"ui.variant: expected {expected_variant}")
    if value.get("verdict") not in ("pass", "changes_requested"):
        errors.append("ui.verdict: unexpected")
    if value.get("allowed_variance") != VARIANCE:
        errors.append("ui.allowed_variance: unexpected fields")
    if value.get("viewport") != "960x640":
        errors.append("ui.viewport: expected 960x640")
    if value.get("theme") != "dark":
        errors.append("ui.theme: expected dark")
    if value.get("browser", {}).get("name") != "chromium":
        errors.append("ui.browser: expected chromium")
    if value.get("input_hashes") != value.get("capture_inputs"):
        errors.append("ui.input_hashes: differs from capture_inputs")
    if value.get("run_id") != report_run_id(
        "ui_capture", value.get("variant"), value.get("capture_inputs")
    ):
        errors.append("ui.run_id: stale or tampered input identity")
    copied = dict(value)
    reported = copied.pop("capture_sha256", None)
    if reported != hashlib.sha256(canonical_bytes(copied)).hexdigest():
        errors.append("ui.capture_sha256: stale or tampered output")
    if expected_inputs is not None and value.get("capture_inputs") != expected_inputs:
        errors.append("ui.capture_inputs: stale output/hash mismatch")
    settings = value.get("ui", {}).get("settings", {})
    regions = settings.get("regions") if isinstance(settings, dict) else None
    if not isinstance(regions, list):
        errors.append("ui.settings.regions: expected array")
        regions = []
    if [item.get("selector") for item in regions if isinstance(item, dict)] != list(
        SETTINGS_SELECTORS
    ):
        errors.append("ui.settings.regions: fixed selector order mismatch")
    for region in regions:
        if not isinstance(region, dict):
            errors.append("ui.settings.region: expected object")
            continue
        selector = region.get("selector")
        if not isinstance(region.get("dom_sha256"), str) or not re.fullmatch(
            r"[a-f0-9]{64}", region.get("dom_sha256", "")
        ):
            errors.append(f"selector {selector} dom_sha256: invalid")
        rect = region.get("rect")
        if not isinstance(rect, dict) or any(
            type(rect.get(field)) is not int for field in ("x", "y", "width", "height")
        ):
            errors.append(f"selector {selector} rect: invalid")
        screenshot = region.get("screenshot", {})
        if not isinstance(screenshot, dict):
            errors.append(f"selector {selector} screenshot: expected object")
            continue
        if not screenshot.get("stable"):
            errors.append(f"selector {selector} screenshot: flaky")
        if verify_files:
            for path_field, hash_field in (
                ("path", "sha256"),
                ("repeat_path", "repeat_sha256"),
            ):
                relative = screenshot.get(path_field)
                if (
                    not isinstance(relative, str)
                    or Path(relative).is_absolute()
                    or ".." in Path(relative).parts
                ):
                    errors.append(
                        f"selector {selector} screenshot.{path_field}: unsafe"
                    )
                    continue
                actual = ROOT / relative
                if not actual.is_file():
                    errors.append(
                        f"selector {selector} screenshot.{path_field}: missing"
                    )
                elif sha256_path(actual) != screenshot.get(hash_field):
                    errors.append(
                        f"selector {selector} screenshot.{path_field}: stale hash"
                    )
    return sorted(set(errors))


# Compare two independently measured settings captures without a static UI replacement.
def compare_ui(baseline, recomposed, fixture_root, policy_path):
    expected_inputs = capture_inputs(fixture_root, policy_path)
    errors = validate_ui_capture(
        baseline, "baseline", expected_inputs, verify_files=True
    )
    errors.extend(
        validate_ui_capture(
            recomposed, "recomposed", expected_inputs, verify_files=True
        )
    )
    if baseline.get("verdict") != "pass":
        errors.extend(
            baseline.get("errors", ["ui_baseline.verdict: capture did not pass"])
        )
    if recomposed.get("verdict") != "pass":
        errors.extend(
            recomposed.get("errors", ["ui_recomposed.verdict: capture did not pass"])
        )
    baseline_ui = baseline.get("ui", {})
    recomposed_ui = recomposed.get("ui", {})
    if baseline_ui.get("plugin_labels") != recomposed_ui.get("plugin_labels"):
        errors.append("ui.plugin_labels: mismatch")
    if baseline_ui.get("host_projection") != recomposed_ui.get("host_projection"):
        errors.append("ui.host_projection: mismatch")
    if baseline_ui.get("settings", {}).get("document_sha256") != recomposed_ui.get(
        "settings", {}
    ).get("document_sha256"):
        errors.append("ui.settings.document_sha256: mismatch")
    baseline_regions = {
        row.get("selector"): row
        for row in baseline_ui.get("settings", {}).get("regions", [])
        if isinstance(row, dict)
    }
    recomposed_regions = {
        row.get("selector"): row
        for row in recomposed_ui.get("settings", {}).get("regions", [])
        if isinstance(row, dict)
    }
    region_results = []
    for selector in SETTINGS_SELECTORS:
        expected = baseline_regions.get(selector)
        actual = recomposed_regions.get(selector)
        region_errors = []
        if expected is None or actual is None:
            region_errors.append(f"selector {selector}: missing recomposed region")
        else:
            for field in ("dom_sha256", "rect"):
                if expected.get(field) != actual.get(field):
                    region_errors.append(f"selector {selector} {field}: mismatch")
            for field in ("sha256", "repeat_sha256", "stable"):
                if expected.get("screenshot", {}).get(field) != actual.get(
                    "screenshot", {}
                ).get(field):
                    region_errors.append(
                        f"selector {selector} screenshot.{field}: mismatch"
                    )
        errors.extend(region_errors)
        region_results.append({"selector": selector, "errors": region_errors})
    return {
        "errors": sorted(set(errors)),
        "settings_regions": region_results,
        "interception": recomposed.get("interception", {}),
    }


# Validate the concrete F3 UI selector input without allowing generic selector rules.
def validate_ui_failure_input(value):
    required = {
        "schema_version",
        "kind",
        "provider",
        "field",
        "selector",
        "expected_sha256",
        "policy",
    }
    if not isinstance(value, dict):
        return ["ui failure input: expected object"]
    errors = []
    for field in sorted(required - set(value)):
        errors.append(f"ui failure input.{field}: missing")
    for field in sorted(set(value) - required):
        errors.append(f"ui failure input.{field}: unexpected")
    if type(value.get("schema_version")) is not int or value.get("schema_version") != 1:
        errors.append("ui failure input.schema_version: expected 1")
    if value.get("kind") != "ui_failure_input":
        errors.append("ui failure input.kind: expected ui_failure_input")
    if value.get("provider") != "source.telegram":
        errors.append("ui failure input.provider: expected source.telegram")
    if value.get("field") != "dom_sha256":
        errors.append("ui failure input.field: expected dom_sha256")
    if value.get("selector") != "#settings-content":
        errors.append("ui failure input.selector: expected #settings-content")
    if not isinstance(value.get("expected_sha256"), str) or not re.fullmatch(
        r"[a-f0-9]{64}", value.get("expected_sha256", "")
    ):
        errors.append("ui failure input.expected_sha256: expected sha256")
    if value.get("policy") != "offline.browser-request":
        errors.append("ui failure input.policy: expected offline.browser-request")
    return errors


# Compare the concrete F3 selector against an actual recomposed settings capture.
def ui_failure_errors(capture, failure):
    errors = validate_ui_failure_input(failure)
    if errors:
        return sorted(errors)
    region = next(
        (
            item
            for item in capture.get("ui", {}).get("settings", {}).get("regions", [])
            if isinstance(item, dict) and item.get("selector") == failure["selector"]
        ),
        None,
    )
    actual = region.get("dom_sha256") if region else None
    if actual != failure["expected_sha256"]:
        return [
            f"provider {failure['provider']} field {failure['field']} selector "
            f"{failure['selector']} policy {failure['policy']}: mismatch"
        ]
    return []


# Write deterministic UI output only to an existing caller-selected parent directory.
def write_report(path, report):
    path = Path(path)
    if not path.parent.is_dir():
        raise ValueError(f"report parent missing: {path.parent}")
    path.write_bytes(canonical_bytes(report) + b"\n")


# Parse fixed Chromium capture/compare forms without arbitrary browser configuration.
def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Capture fixed Chromium UI parity offline"
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--capture", action="store_true")
    modes.add_argument("--compare", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--no-network", action="store_true")
    parser.add_argument("--intercept-external", action="store_true")
    parser.add_argument("--fixture-root", type=Path, required=True)
    parser.add_argument("--policy", type=Path)
    parser.add_argument(
        "--variant", choices=("baseline", "recomposed"), default="baseline"
    )
    parser.add_argument("--browser", default="chromium")
    parser.add_argument("--viewport", default="960x640")
    parser.add_argument("--theme", default="dark")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--recomposed", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    require_offline_flags(args.offline, args.no_network, args.intercept_external)
    if args.browser != "chromium" or args.viewport != "960x640" or args.theme != "dark":
        parser.error("fixed UI runtime is chromium 960x640 dark")
    if args.capture and args.output is None:
        parser.error("--capture requires --output")
    if args.compare and any(
        value is None for value in (args.baseline, args.recomposed, args.report)
    ):
        parser.error("--compare requires --baseline --recomposed --report")
    return args


# Run UI capture/compare and overwrite stale report data each time.
def main(argv=None):
    try:
        args = parse_args(argv)
        fixture_root = Path(args.fixture_root)
        policy_path = Path(args.policy or fixture_root / "offline-policy.json")
        if args.capture:
            report = capture_ui(args)
            write_report(args.output, report)
        else:
            result = compare_ui(
                load_json(args.baseline),
                load_json(args.recomposed),
                fixture_root,
                policy_path,
            )
            input_hashes = {
                "baseline": sha256_path(args.baseline),
                "recomposed": sha256_path(args.recomposed),
            }
            report = {
                "schema_version": 1,
                "kind": "ui_comparison",
                "verdict": "pass" if not result["errors"] else "changes_requested",
                "allowed_variance": VARIANCE,
                "errors": result["errors"],
                "input_hashes": input_hashes,
                "interception": result["interception"],
                "ui_comparison": result,
            }
            report["run_id"] = report_run_id(
                report["kind"], report["verdict"], input_hashes
            )
            report["report_sha256"] = hashlib.sha256(
                canonical_bytes(report)
            ).hexdigest()
            write_report(args.report, report)
    except (FixturePolicyError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    for error in report["errors"]:
        print(f"FAIL: {error}", file=sys.stderr)
    if report["verdict"] != "pass":
        return 1
    print("PASS: deterministic Chromium UI parity")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
