# pyright: basic

import hashlib
import importlib
import sys
from pathlib import Path

from plugin_parity_json import load_json, validate_schema, write_json

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SCHEMA_PATH = ROOT / "schemas" / "plugin" / "plugin-manifest.schema.json"
manifest_module = importlib.import_module("ohmymeme.core.plugins.manifest")
PluginManifestError = manifest_module.PluginManifestError
canonical_document = manifest_module.canonical_document
validate_manifest = manifest_module.validate_manifest


def _parse_arguments(argv):
    manifest = None
    entry_point_group = None
    report = None
    determinism = False
    index = 0
    while index < len(argv):
        option = argv[index]
        if option == "--check-determinism":
            determinism = True
            index += 1
            continue
        if option in (
            "--validate-manifest",
            "--manifest",
            "--entry-point-group",
            "--report",
        ):
            if index + 1 >= len(argv):
                raise ValueError(f"{option}: missing value")
            value = argv[index + 1]
            if option in ("--validate-manifest", "--manifest"):
                if manifest is not None:
                    raise ValueError("manifest: specified more than once")
                manifest = Path(value)
            elif option == "--entry-point-group":
                entry_point_group = value
            else:
                report = Path(value)
            index += 2
            continue
        if option == "--help":
            print(
                "usage: plugin_contract.py --validate-manifest INPUT "
                "--entry-point-group GROUP --report REPORT"
            )
            raise SystemExit(0)
        raise ValueError(f"unknown option: {option}")
    if manifest is None:
        raise ValueError("manifest: missing required option")
    if report is None:
        raise ValueError("--report: missing required option")
    if not determinism and entry_point_group is None:
        raise ValueError("--entry-point-group: missing required option")
    return manifest, entry_point_group, report, determinism


def _load_and_validate(manifest_path, entry_point_group):
    value = load_json(manifest_path)
    schema = load_json(SCHEMA_PATH)
    errors = validate_schema(value, schema, schema, "")
    try:
        descriptors = validate_manifest(value, entry_point_group)
    except PluginManifestError as error:
        errors.extend(error.errors)
        descriptors = ()
    raw = manifest_path.read_bytes()
    if raw != canonical_document(value):
        errors.append(
            "manifest: expected canonical UTF-8 JSON bytes with trailing newline"
        )
    if errors:
        raise PluginManifestError(errors)
    return value, descriptors


def _validate(manifest_path, entry_point_group, report):
    value, descriptors = _load_and_validate(manifest_path, entry_point_group)
    write_json(
        report,
        {
            "api_version": value["api_version"],
            "manifest_sha256": hashlib.sha256(canonical_document(value)).hexdigest(),
            "plugin_ids": [descriptor.id for descriptor in descriptors],
            "status": "PASS",
        },
    )
    print(f"PASS: validated {len(descriptors)} plugin descriptors")


def _check_determinism(manifest_path, report):
    value, _ = _load_and_validate(manifest_path, manifest_module.ENTRY_POINT_GROUP)
    first = canonical_document(value)
    repeat_value, _ = _load_and_validate(
        manifest_path, manifest_module.ENTRY_POINT_GROUP
    )
    second = canonical_document(repeat_value)
    if first != second:
        raise PluginManifestError(("manifest: canonical serialization mismatch",))
    write_json(
        report,
        {
            "manifest_sha256": hashlib.sha256(first).hexdigest(),
            "serialization_sha256": hashlib.sha256(second).hexdigest(),
            "status": "PASS",
        },
    )
    print("PASS: canonical serializations are byte-identical")


def main(argv=None):
    try:
        manifest, entry_point_group, report, determinism = _parse_arguments(
            sys.argv[1:] if argv is None else argv
        )
        if determinism:
            _check_determinism(manifest, report)
        else:
            _validate(manifest, entry_point_group, report)
    except (OSError, ValueError) as error:
        errors = getattr(error, "errors", (str(error),))
        if "report" in locals() and report is not None:
            write_json(report, {"errors": list(errors), "status": "REJECTED"})
        print(f"REJECTED: {errors[0]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
