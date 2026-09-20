# pyright: basic

import argparse
import ast
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import tomllib
from email.parser import Parser
from pathlib import Path

try:
    from scripts import build_notice_bundle
except ModuleNotFoundError:
    import build_notice_bundle

ROOT = Path(__file__).resolve().parents[1]
OFFICIAL = (
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
GPL = "GPL-3.0-only"
STAGING = "fixtures/plugin-parity/frozen-staging"
BUNDLE_INDEX = "THIRD-PARTY-NOTICES/license-evidence.json"
MATRIX = "docs/plugin-license-matrix.json"
SCHEMA = "schemas/plugin/license-matrix.schema.json"
FROZEN_NOTICE_TARGETS = (
    "ohmymeme/LICENSE",
    "ohmymeme/NOTICE",
    "ohmymeme/LICENSES",
    "ohmymeme/THIRD-PARTY-NOTICES",
)
OPTIONAL_RUNTIME_EXCLUSIONS = ("gmssl",)
EXTERNAL_HELPER_BINARY_SUFFIXES = {".a", ".dll", ".exe", ".lib", ".obj", ".pdb"}
DEPENDENCY_RULES = {
    "boto3": (
        "1.43.97",
        "https://github.com/boto/boto3",
        "Apache-2.0",
        "License",
        "Apache-2.0",
        "Apache-2.0 permissive notice; compatible with the GPL host as an "
        "aggregate/runtime dependency.",
    ),
    "botocore": (
        "1.43.97",
        "https://github.com/boto/botocore",
        "Apache-2.0",
        "License",
        "Apache-2.0",
        "Apache-2.0 permissive notice; compatible with the GPL host as an "
        "aggregate/runtime dependency.",
    ),
    "bottle": (
        "0.13.4",
        "https://github.com/bottlepy/bottle",
        "MIT",
        "License",
        "MIT",
        "MIT permission and notice preserved in the bundled license text.",
    ),
    "cryptography": (
        "50.0.1",
        "https://github.com/pyca/cryptography",
        "Apache-2.0 OR BSD-3-Clause",
        "License-Expression",
        "Apache-2.0 OR BSD-3-Clause",
        "The package permits either Apache-2.0 or BSD-3-Clause; both license "
        "texts are bundled.",
    ),
    "curl-cffi": (
        "0.16.3",
        "https://github.com/lexiforest/curl_cffi",
        "MIT",
        "License-Expression",
        "MIT",
        "MIT permission and notice preserved in the bundled license text.",
    ),
    "keyboard": (
        "0.13.5",
        "https://github.com/boppreh/keyboard",
        "MIT",
        "License",
        "MIT",
        "MIT permission and notice preserved in the bundled license text.",
    ),
    "pillow": (
        "12.3.0",
        "https://github.com/python-pillow/Pillow",
        "MIT-CMU",
        "License-Expression",
        "MIT-CMU",
        "Pillow's declared MIT-CMU expression is accepted and its complete "
        "license text is bundled.",
    ),
    "pydantic": (
        "2.11.7",
        "https://github.com/pydantic/pydantic",
        "MIT",
        "License-Expression",
        "MIT",
        "MIT permission and notice preserved in the bundled license text.",
    ),
    "pypinyin": (
        "0.55.0",
        "https://github.com/mozillazg/python-pinyin",
        "MIT",
        "License",
        "MIT",
        "MIT permission and notice preserved in the bundled license text.",
    ),
    "pyinstaller": (
        "6.22.3",
        "https://github.com/pyinstaller/pyinstaller",
        "GPLv2-or-later with a special exception which allows to use PyInstaller "
        "to build and distribute non-free programs (including commercial ones)",
        "License",
        "GPL-2.0-or-later WITH Bootloader-exception",
        "PyInstaller's COPYING.txt grants the bootloader exception; the complete "
        "exception text is bundled.",
    ),
    "pyperclip": (
        "1.11.0",
        "https://github.com/asweigart/pyperclip",
        "BSD",
        "License",
        "BSD-3-Clause",
        "The bundled BSD text contains the three-clause no-endorsement condition "
        "and is retained verbatim.",
    ),
    "pystray": (
        "0.19.5",
        "https://github.com/moses-palmer/pystray",
        "LGPLv3",
        "License",
        "LGPL-3.0-only",
        "The retained full COPYING.LGPL is LGPL-3.0-only evidence; legacy "
        "metadata remains License: LGPLv3.",
    ),
    "pywebview": (
        "6.2.1",
        "https://github.com/r0x0r/pywebview",
        "BSD 3-Clause License",
        "License",
        "BSD-3-Clause",
        "The bundled BSD 3-Clause text preserves the binary redistribution notice.",
    ),
    "tgcrypto": (
        "1.2.5",
        "https://github.com/pyrogram/tgcrypto",
        "LGPLv3+",
        "License",
        "LGPL-3.0-or-later",
        "The package declares LGPLv3+ and bundles COPYING, COPYING.lesser and NOTICE.",
    ),
}
DEPENDENCY_LICENSE_FALLBACKS = {"keyboard": ("LICENSE.txt",)}
SOURCE_COMPONENTS = {
    "qqnt-source": (
        "third-party-source",
        GPL,
        ["plugins/source.qqnt/src/ohmymeme_plugin_qqnt/__init__.py"],
        ["https://github.com/VanillaNahida/QQFavoriteExtract", "GNU GPL v3"],
    ),
    "abogus-source": (
        "third-party-source",
        GPL,
        ["plugins/source.douyin/src/ohmymeme_plugin_douyin/abogus.py"],
        [
            "https://github.com/JoeanAmier/TikTokDownloader",
            "https://github.com/Evil0ctal/Douyin_TikTok_Download_API",
            "GNU General Public License v3.0",
        ],
    ),
    "wechat-helper": (
        "helper",
        GPL,
        [
            "src/wechat_keyfinder/CMakeLists.txt",
            "src/wechat_keyfinder/wechat_keyfinder.cpp",
            "src/ohmymeme/integrations/imports/wechat.py",
        ],
        ["wechat_keyfinder", "OpenSSL::Crypto"],
    ),
}


def _duplicates(pairs):
    # Duplicate keys must not hide rejected evidence behind a later value.
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _json(raw):
    # Reject non-JSON constants as well as malformed and duplicate-key documents.
    def reject(value):
        # JSON NaN/Infinity are not valid evidence values.
        raise ValueError(f"invalid JSON constant: {value}")

    return json.loads(raw, object_pairs_hook=_duplicates, parse_constant=reject)


def _schema(value, schema, root, path="matrix"):
    # Execute the small JSON Schema vocabulary used by the committed schema.
    if "$ref" in schema:
        target = root
        for part in schema["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        return _schema(value, target, root, path)
    errors = []
    types = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "boolean": bool,
        "null": type(None),
    }
    expected = schema.get("type")
    if expected:
        allowed = expected if isinstance(expected, list) else [expected]
        if not any(type(value) is types[item] for item in allowed):
            return [f"{path}: expected {expected}"]
    if "const" in schema and (
        type(value) is not type(schema["const"]) or value != schema["const"]
    ):
        errors.append(f"{path}: expected {schema['const']!r}")
    if "enum" in schema and not any(
        type(value) is type(item) and value == item for item in schema["enum"]
    ):
        errors.append(f"{path}: unapproved value {value!r}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: missing required field")
        if len(value) < schema.get("minProperties", 0):
            errors.append(f"{path}: requires evidence entries")
        for key, item in value.items():
            child = properties.get(key, schema.get("additionalProperties", {}))
            if child is False:
                errors.append(f"{path}.{key}: unexpected property")
            elif isinstance(child, dict):
                errors.extend(_schema(item, child, root, f"{path}.{key}"))
    if isinstance(value, list):
        if (
            not schema.get("minItems", 0)
            <= len(value)
            <= schema.get("maxItems", len(value))
        ):
            errors.append(f"{path}: invalid item count {len(value)}")
        if schema.get("uniqueItems") and len(value) != len(
            {json.dumps(item, sort_keys=True) for item in value}
        ):
            errors.append(f"{path}: duplicate item")
        for index, item in enumerate(value):
            errors.extend(
                _schema(item, schema.get("items", {}), root, f"{path}[{index}]")
            )
    if isinstance(value, str):
        if len(value.strip()) < schema.get("minLength", 0):
            errors.append(f"{path}: empty evidence")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: malformed value {value!r}")
    return errors


def _path(root, name):
    # Evidence is repository-local and cannot escape through traversal or symlinks.
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise ValueError(f"evidence path: unsafe {name!r}")
    if name.startswith("/") or any(part in ("", ".", "..") for part in name.split("/")):
        raise ValueError(f"evidence path: unsafe {name!r}")
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or not path.exists():
        raise ValueError(f"evidence path: missing or outside repository: {name}")
    return path


def _difference(actual, expected, path):
    # Compare source-derived facts, not a second fixture or a previous report.
    errors = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(actual) | set(expected)):
            if key not in actual:
                errors.append(f"{path}.{key}: missing source-backed field")
            elif key not in expected:
                errors.append(f"{path}.{key}: unknown source-backed field")
            else:
                errors.extend(_difference(actual[key], expected[key], f"{path}.{key}"))
    elif isinstance(expected, list) and isinstance(actual, list):
        if len(actual) != len(expected):
            errors.append(
                f"{path}: source drift; expected {expected!r}, got {actual!r}"
            )
        else:
            for index, (actual_item, expected_item) in enumerate(zip(actual, expected)):
                errors.extend(
                    _difference(actual_item, expected_item, f"{path}[{index}]")
                )
    elif type(actual) is not type(expected) or actual != expected:
        errors.append(f"{path}: source drift; expected {expected!r}, got {actual!r}")
    return errors


def _entry_point_triplets(text):
    # Parse staged entry point semantics independently from its normalized hash.
    group = None
    entries = []
    errors = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            group = line[1:-1]
            continue
        if group is None or "=" not in line:
            errors.append(f"malformed frozen entry point line {raw_line!r}")
            continue
        name, value = (part.strip() for part in line.split("=", 1))
        if not name or not value:
            errors.append(f"malformed frozen entry point line {raw_line!r}")
            continue
        entries.append((group, name, value))
    return entries, errors


def inspect_sources(
    root=ROOT,
    local_evidence_path=build_notice_bundle.FINAL_ENV_EVIDENCE,
    notice_index=None,
    generated_files=None,
):
    # Parse authored metadata, requirements and frozen files without importing code.
    hashes, hash_modes, errors, declarations, packages, imports = {}, {}, [], {}, [], {}

    def read(name):
        # Bind observations to the documented byte or normalized-text hash mode.
        if generated_files is not None and name in generated_files:
            raw = generated_files[name]
        else:
            path = _path(root, name)
            raw = path.read_bytes()
        hash_modes[name] = build_notice_bundle.matrix_hash_mode(name)
        hashes[name] = build_notice_bundle.matrix_hash(name, raw)
        return raw.decode("utf-8")

    def source_hashes(names):
        # Require the whole declared source set rather than accepting a partial map.
        for name in names:
            read(name)
        return {name: hashes[name] for name in sorted(names)}

    def dependency(owner, source, scope, requirement):
        # Parse only the repository's simple requirement form; unknown syntax blocks.
        if requirement != "OpenSSL::Crypto" and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*"
            r"(?:(?:===|==|!=|~=|>=|<=|>|<)[A-Za-z0-9.*+_-]+)?",
            requirement,
        ):
            raise ValueError(f"{source}: unsupported requirement {requirement!r}")
        match = re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*", requirement)
        if not match:
            raise ValueError(f"{source}: malformed requirement {requirement!r}")
        name = re.sub(r"[-_.]+", "-", match.group()).lower()
        declarations.setdefault(name, []).append(
            {
                "owner": owner,
                "source": source,
                "scope": scope,
                "requirement": requirement,
            }
        )

    includes, runtime = {}, {}
    for name in ("requirements.txt", "requirements-dev.txt"):
        includes[name] = []
        for line in read(name).splitlines():
            item = line.split("#", 1)[0].strip()
            if not item:
                continue
            if item.startswith("-r "):
                includes[name].append(item[3:].strip())
                continue
            scope = "development" if name.endswith("-dev.txt") else "runtime"
            if item.lower().startswith("pyinstaller"):
                scope = "build"
            dependency("host", name, scope, item)
            if name == "requirements.txt":
                runtime[
                    re.sub(r"[-_.]+", "-", re.split(r"[<>=!~\[]", item)[0]).lower()
                ] = item
    environment = read("environment.yml")
    dependency_section = environment.partition("dependencies:")[2]
    conda_items = re.findall(r"^  - ([^#\r\n]+)", dependency_section, re.M)
    pip_items = re.findall(r"^    - ([^#\r\n]+)", dependency_section, re.M)
    if conda_items != ["python=3.12", "pip", "pip:"] or pip_items != [
        "-r requirements-dev.txt"
    ]:
        errors.append("environment.yml: undeclared conda/pip dependency approval")
    includes["environment.yml"] = re.findall(
        r"^\s*-\s+-r\s+(\S+)\s*$", environment, re.M
    )
    if includes != {
        "requirements.txt": [],
        "requirements-dev.txt": ["requirements.txt"],
        "environment.yml": ["requirements-dev.txt"],
    }:
        errors.append("requirements/environment: include chain drift")
    setup_tree = ast.parse(read("setup.py"))
    setup = next(
        node
        for node in ast.walk(setup_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "setup"
    )
    setup_values = {
        item.arg: ast.literal_eval(item.value)
        for item in setup.keywords
        if item.arg
        in (
            "name",
            "license",
            "license_expression",
            "license_files",
            "classifiers",
            "install_requires",
            "extras_require",
        )
    }
    for item in setup_values["install_requires"]:
        dependency("host", "setup.py", "runtime", item)
    for extra, items in setup_values["extras_require"].items():
        for item in items:
            dependency("host", "setup.py", f"extra:{extra}", item)
    if setup_values.get("license") != GPL or any(
        item.startswith("License ::") for item in setup_values["classifiers"]
    ):
        errors.append("host ohmymeme: setup.py license contradicts GPL-3.0 LICENSE")
    if setup_values.get("license_files") != ["LICENSE"]:
        errors.append("host ohmymeme: setup.py license_files must be ['LICENSE']")
    if setup_values.get("license_expression") != GPL:
        errors.append("host ohmymeme: setup.py license_expression must be GPL-3.0-only")
    license_text = read("LICENSE")
    if (
        "GNU GENERAL PUBLIC LICENSE" not in license_text
        or "Version 3, 29 June 2007" not in license_text
    ):
        errors.append("host ohmymeme: LICENSE is not the recorded GPL v3 text")
    host_paths = [
        "LICENSE",
        "NOTICE",
        "LICENSES/GPL-3.0-only.txt",
        "LICENSES/README.md",
        "README.md",
        "setup.py",
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "environment.yml",
        "src/ohmymeme/__init__.py",
        "scripts/build.py",
        "scripts/nuitka/build.py",
        "scripts/package_smoke.py",
    ]
    host = {
        "distribution": setup_values["name"],
        "metadata_source": "setup.py",
        "spdx": GPL,
        "license_files": ["LICENSE"],
        "metadata_license_expression": GPL,
        "metadata_license_files": ["LICENSE"],
        "frozen_license_files": [
            "ohmymeme/LICENSE",
            "ohmymeme/NOTICE",
            "ohmymeme/LICENSES/GPL-3.0-only.txt",
        ],
        "source_hashes": source_hashes(host_paths),
        "requirement_includes": includes,
    }
    for name in (
        "scripts/build.py",
        "scripts/nuitka/build.py",
        "scripts/package_smoke.py",
    ):
        source = read(name)
        for target in FROZEN_NOTICE_TARGETS:
            if target not in source:
                errors.append(
                    f"host ohmymeme: missing frozen notice target {target} in {name}"
                )
    manifest_path = "config/plugin-manifest.json"
    manifest_raw = read(manifest_path)
    manifest = _json(manifest_raw)
    if [row.get("id") for row in manifest["plugins"]] != list(OFFICIAL):
        errors.append(
            "config/plugin-manifest.json: unknown or missing official provider"
        )
    frozen_path = f"{STAGING}/ohmymeme/config/plugin-manifest.json"
    if read(frozen_path) != manifest_raw:
        errors.append("frozen official set: manifest bytes differ from authored source")
    staging_path = f"{STAGING}/staging-manifest.json"
    staging = _json(read(staging_path))
    if [row.get("id") for row in staging["packages"]] != list(OFFICIAL):
        errors.append(
            "frozen official set: unknown distribution or canonical order drift"
        )
    staged_by_id = {row["id"]: row for row in staging["packages"]}
    frozen_rows = []
    actual_projects = sorted(
        path.parent.name for path in (root / "plugins").glob("*/pyproject.toml")
    )
    if actual_projects != sorted(OFFICIAL):
        errors.append(f"plugins: unknown distribution roots {actual_projects!r}")
    for provider in OFFICIAL:
        suffix = (
            provider.replace(".", "-")
            if provider.startswith("sync.")
            else provider.split(".")[1]
        )
        distribution = "ohmymeme-plugin-" + suffix
        module = distribution.replace("-", "_")
        pyproject = f"plugins/{provider}/pyproject.toml"
        data = tomllib.loads(read(pyproject))
        project = data["project"]
        factory = f"{module}:create_plugin"
        if project["name"] != distribution or project["license"] != GPL:
            errors.append(f"{distribution}: pyproject name/SPDX license mismatch")
        if project.get("license-files") != ["LICENSE"]:
            errors.append(
                f"{distribution}: pyproject license-files must be ['LICENSE']"
            )
        if project["entry-points"].get("ohmymeme.plugins.v1") != {provider: factory}:
            errors.append(f"{distribution}: pyproject official entry point drift")
        for item in data["build-system"]["requires"]:
            dependency(provider, pyproject, "build-system", item)
        for item in project.get("dependencies", []):
            dependency(provider, pyproject, "runtime", item)
        for extra, items in project.get("optional-dependencies", {}).items():
            for item in items:
                dependency(provider, pyproject, f"extra:{extra}", item)
        package_root = _path(root, f"plugins/{provider}/src/{module}")
        module_files = sorted(
            path.relative_to(root / f"plugins/{provider}/src").as_posix()
            for path in package_root.rglob("*.py")
            if "__pycache__" not in path.parts
        )
        source_paths = [pyproject, "LICENSE"] + [
            f"plugins/{provider}/src/{name}" for name in module_files
        ]
        imports[provider] = {}
        for name in module_files:
            source_name = f"plugins/{provider}/src/{name}"
            source = read(source_name)
            frozen_source = read(f"{STAGING}/{name}")
            if build_notice_bundle._utf8_lf_bytes(
                source.encode("utf-8")
            ) != build_notice_bundle._utf8_lf_bytes(frozen_source.encode("utf-8")):
                errors.append(f"{distribution}: source hash drift in frozen {name}")
            for node in ast.walk(ast.parse(source)):
                names = []
                if isinstance(node, ast.Import):
                    names = [item.name.split(".")[0] for item in node.names]
                elif (
                    isinstance(node, ast.ImportFrom) and node.module and not node.level
                ):
                    names = [node.module.split(".")[0]]
                for imported in names:
                    imports[provider].setdefault(imported, set()).add(source_name)
            if provider == "source.telegram" and 'shutil.which("ffmpeg")' in source:
                dependency(provider, source_name, "external-executable", "ffmpeg")
        license_files = sorted(
            path.name
            for path in (root / f"plugins/{provider}").iterdir()
            if path.is_file()
            and path.name.upper().startswith(("LICENSE", "COPYING", "NOTICE"))
        )
        row = staged_by_id.get(provider, {})
        dist_info = f"{module}-{project['version']}.dist-info"
        metadata_path = f"{STAGING}/{dist_info}/METADATA"
        entry_path = f"{STAGING}/{dist_info}/entry_points.txt"
        for name in row["metadata_files"]:
            read(f"{STAGING}/{name}")
        actual_metadata = sorted(
            path.relative_to(root / STAGING).as_posix()
            for path in (root / STAGING / dist_info).rglob("*")
            if path.is_file()
        )
        if actual_metadata != row["metadata_files"]:
            errors.append(f"{distribution}: missing/unknown frozen metadata file")
        message = Parser().parsestr(read(metadata_path))
        entries = read(entry_path)
        entry_points, entry_errors = _entry_point_triplets(entries)
        errors.extend(f"{distribution}: {error}" for error in entry_errors)
        for field, expected in (
            ("Name", distribution),
            ("Version", project["version"]),
            ("License-Expression", GPL),
        ):
            if message.get_all(field) != [expected]:
                errors.append(f"{distribution}: frozen metadata {field} mismatch")
        if message.get_all("License-File", []) != ["LICENSE"]:
            errors.append(f"{distribution}: frozen metadata License-File mismatch")
        if f"{dist_info}/licenses/LICENSE" not in actual_metadata:
            errors.append(f"{distribution}: frozen metadata license location missing")
        dependencies = project.get("dependencies", [])
        if message.get_all("Requires-Dist", []) != dependencies:
            errors.append(f"{distribution}: frozen dependency metadata drift")
        expected_entry_points = [("ohmymeme.plugins.v1", provider, factory)]
        if entry_points != expected_entry_points:
            errors.append(f"{distribution}: frozen entry point drift")
        for field, expected in (
            ("distribution", distribution),
            ("license", GPL),
            ("dependencies", dependencies),
            ("module_files", module_files),
            ("license_files", license_files),
        ):
            errors.extend(
                _difference(row.get(field), expected, f"frozen.{distribution}.{field}")
            )
        packages.append(
            {
                "id": provider,
                "distribution": distribution,
                "version": project["version"],
                "pyproject": pyproject,
                "module": module,
                "factory": factory,
                "spdx": GPL,
                "license_files": ["LICENSE"],
                "package_license_files": license_files,
                "frozen_license_files": row.get("license_files"),
                "dependencies": dependencies,
                "source_hashes": source_hashes(source_paths),
            }
        )
        frozen_rows.append(
            {
                "id": provider,
                "distribution": distribution,
                "version": project["version"],
                "spdx": GPL,
                "dependencies": dependencies,
                "module_files": module_files,
                "metadata_files": row["metadata_files"],
                "license_files": row["license_files"],
                "license_files_disabled": row["license_files_disabled"],
                "metadata_sha256": hashes[metadata_path],
                "entry_points_sha256": hashes[entry_path],
                "entry_points": [list(item) for item in entry_points],
            }
        )
    actual_dist_info = sorted(
        path.relative_to(root / STAGING).as_posix()
        for path in (root / STAGING).rglob("*.dist-info")
    )
    expected_dist_info = sorted(
        f"{row['module']}-{row['version']}.dist-info" for row in packages
    )
    if actual_dist_info != expected_dist_info:
        errors.append(
            "frozen official set: unknown distribution directories "
            f"{actual_dist_info!r}"
        )
    try:
        _, helper_research = build_notice_bundle._research(root)
    except (KeyError, OSError, ValueError) as error:
        errors.append(f"wechat-helper: local research unavailable: {error}")
        helper_research = {}
    source_components = []
    for identifier, (kind, spdx, paths, markers) in SOURCE_COMPONENTS.items():
        contents = "\n".join(read(name) for name in paths)
        for marker in markers:
            if marker not in contents:
                errors.append(
                    f"{identifier}.provenance: missing source marker {marker}"
                )
        license_files = ["LICENSE"] if spdx == GPL else []
        source_components.append(
            {
                "id": identifier,
                "kind": kind,
                "spdx": spdx,
                "license_files": license_files,
                "source_hashes": source_hashes(paths),
            }
        )
        if identifier == "wechat-helper":
            try:
                tag = helper_research["git_history"]["local_tag_check"]
                source_components[-1]["source_revision"] = {
                    "tag": tag["tag"],
                    "commit": tag["commit"],
                    "root_license_sha256": hashes["LICENSE"],
                    "source_binding": "local-source-only",
                }
            except KeyError as error:
                errors.append(f"wechat-helper: local source revision missing: {error}")
    cmake_path = "src/wechat_keyfinder/CMakeLists.txt"
    libraries = re.findall(
        r"target_link_libraries\(wechat_keyfinder\s+([^)]*)\)", read(cmake_path)
    )
    if len(libraries) != 1:
        errors.append(
            "wechat-helper: missing or ambiguous CMake dependency declaration"
        )
    for library in libraries[0].split() if libraries else []:
        dependency("wechat-helper", cmake_path, "static-link", library)
    import_names = {"PIL": "pillow", "curl_cffi": "curl-cffi"}
    local = {row["module"] for row in packages} | {"ohmymeme"}
    extras = {}
    for provider, names in imports.items():
        extras[provider] = []
        for name, paths in sorted(names.items()):
            if name in sys.stdlib_module_names or name in local:
                continue
            distribution = import_names.get(name, name)
            extras[provider].append(runtime.get(distribution, distribution))
            if distribution not in runtime:
                for path in sorted(paths):
                    dependency(provider, path, "source-import", distribution)
        if provider == "source.telegram" and "ffmpeg" in declarations:
            extras[provider].append("ffmpeg")
        extras[provider].sort()
    pyinstaller = read("scripts/build.py")
    nuitka = read("scripts/nuitka/build.py")
    for target in FROZEN_NOTICE_TARGETS:
        if target not in pyinstaller or target not in nuitka:
            errors.append(f"release scope: missing frozen data target {target}")
    for module in OPTIONAL_RUNTIME_EXCLUSIONS:
        if (
            module not in pyinstaller
            or 'cmd += ["--exclude-module", module]' not in pyinstaller
        ):
            errors.append(
                "release scope: PyInstaller missing optional runtime exclusion "
                f"{module}"
            )
        if module not in nuitka or '"--nofollow-import-to=" + module' not in nuitka:
            errors.append(
                f"release scope: Nuitka missing optional runtime exclusion {module}"
            )
    for module in ("curl_cffi", "gmssl"):
        if module not in imports.get("source.douyin", {}):
            errors.append(f"source.douyin: expected optional import {module}")
    if "--collect-submodules" not in pyinstaller:
        errors.append(
            "release scope: PyInstaller does not collect official plugin modules"
        )
    if "PIL" not in imports.get("source.telegram", {}):
        errors.append("release scope: source.telegram no longer selects Pillow")
    if "cryptography" not in imports.get("source.telegram", {}):
        errors.append("release scope: source.telegram no longer selects cryptography")
    for package in ("PIL", "cryptography"):
        if "--include-package=" + package not in nuitka:
            errors.append(f"release scope: Nuitka missing native package {package}")
    if "curl_cffi" not in pyinstaller or "curl_cffi" not in nuitka:
        errors.append("release scope: curl-cffi is not explicitly collected")
    helper_root = root / "src" / "wechat_keyfinder"
    helper_payloads = sorted(
        path.relative_to(root).as_posix()
        for path in helper_root.rglob("*")
        if path.is_file() and path.suffix.lower() in EXTERNAL_HELPER_BINARY_SUFFIXES
    )
    if helper_payloads:
        errors.append(
            "release scope: external wechat helper payload is present in source "
            f"tree: {helper_payloads!r}"
        )
    for name, source in (
        ("scripts/build.py", pyinstaller),
        ("scripts/nuitka/build.py", nuitka),
    ):
        if (
            "def _assert_external_helper_not_packaged" not in source
            or "_assert_external_helper_not_packaged()" not in source
        ):
            errors.append(
                f"release scope: {name} lacks the external helper frozen-input guard"
            )
        if "--add-binary" in source:
            errors.append(
                f"release scope: {name} declares an unapproved frozen binary target"
            )
    try:
        notice_bundle = notice_index or build_notice_bundle.expected_index(
            root, local_evidence_path
        )
        read(BUNDLE_INDEX)
    except (KeyError, OSError, ValueError) as error:
        errors.append(f"notice bundle: unavailable: {error}")
        notice_bundle = {
            "bundle_version": "",
            "content_sha256": "",
            "native_distributions": [],
            "external_components": [],
            "owned_source_mapping": {},
        }
    release_components = []
    for component in notice_bundle["external_components"]:
        row = {
            key: component[key]
            for key in (
                "id",
                "scope",
                "owner",
                "excluded_from_targets",
                "notice_status",
                "reason",
            )
        }
        for key in ("source_path", "sha256", "source_binding", "url", "source_archive"):
            if key in component:
                row[key] = component[key]
        row["index_component_sha256"] = hashlib.sha256(
            json.dumps(
                component, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        release_components.append(row)
    release_scope = {
        "targets": [
            {
                "id": "pyinstaller",
                "data_targets": list(FROZEN_NOTICE_TARGETS),
                "excluded_modules": list(OPTIONAL_RUNTIME_EXCLUSIONS),
            },
            {
                "id": "nuitka",
                "data_targets": list(FROZEN_NOTICE_TARGETS),
                "excluded_modules": list(OPTIONAL_RUNTIME_EXCLUSIONS),
            },
        ],
        "feature_availability": [
            {
                "provider": "source.douyin",
                "feature": "import",
                "status": "runtime_unavailable",
                "reason": (
                    "gmssl is excluded because its exact license text is unresolved; "
                    "the descriptor remains and the registry returns its existing "
                    "provider_unavailable sentinel. curl_cffi remains shipped as a "
                    "distribution-covered wheel."
                ),
            },
            {
                "provider": "source.wechat",
                "feature": "encrypted-index-helper",
                "status": "external_runtime_not_shipped",
                "reason": (
                    "The helper is fetched at runtime and is not included in the "
                    "frozen release scope."
                ),
            },
        ],
        "components": release_components,
    }
    return (
        {
            "hash_policy": build_notice_bundle.matrix_hash_policy(hash_modes),
            "host": host,
            "distributions": packages,
            "source_components": source_components,
            "native_distributions": notice_bundle["native_distributions"],
            "owned_source_mapping": notice_bundle["owned_source_mapping"],
            "dependencies": [
                {
                    "name": name,
                    "declarations": sorted(
                        rows,
                        key=lambda item: (
                            item["source"],
                            item["owner"],
                            item["scope"],
                            item["requirement"],
                        ),
                    ),
                }
                for name, rows in sorted(declarations.items())
            ],
            "provider_extras": extras,
            "frozen_official_set": {
                "staging_manifest": staging_path,
                "frozen_manifest": frozen_path,
                "source_manifest": manifest_path,
                "official_ids": list(OFFICIAL),
                "manifest_sha256": hashes[manifest_path],
                "staging_manifest_sha256": hashes[staging_path],
                "shared_license_target": "ohmymeme/LICENSE",
                "distributions": frozen_rows,
            },
            "notice_bundle": {
                "path": BUNDLE_INDEX,
                "index_sha256": hashes.get(BUNDLE_INDEX, ""),
                "content_sha256": notice_bundle["content_sha256"],
                "bundle_version": notice_bundle["bundle_version"],
            },
            "release_scope": release_scope,
        },
        errors,
        hashes,
    )


def _legal(row, prefix, root):
    # Missing provenance, notice or source offer is a release rejection, not a guess.
    errors = []
    if row["spdx"] == "NOASSERTION" or not row["license_files"]:
        errors.append(
            f"{prefix}.spdx/license_files: unresolved license {row['spdx']}; REJECTED"
        )
    for field, status, paths in (
        ("provenance", "verified", "files"),
        ("notice", "present", "files"),
        ("source_offer", "available", "paths"),
    ):
        evidence = row[field]
        if evidence["status"] != status or not evidence[paths]:
            errors.append(f"{prefix}.{field}: missing or unresolved evidence; REJECTED")
        for name in evidence[paths]:
            try:
                path = _path(root, name)
                if field != "source_offer" and name not in (
                    set(row["source_hashes"]) | set(row["license_files"])
                ):
                    raise ValueError(f"unbound evidence, not component source: {name}")
                if field == "source_offer" and name.split("/")[0] in (
                    "fixtures",
                    ".omo",
                    "docs",
                ):
                    raise ValueError(
                        f"fixture/report is not corresponding source: {name}"
                    )
                if field == "provenance":
                    text = path.read_text(encoding="utf-8")
                    if path.suffix == ".json" or not text.strip():
                        raise ValueError(f"provenance is not authored source: {name}")
            except ValueError as error:
                errors.append(f"{prefix}.{field}: {error}")
    for name in row["license_files"]:
        try:
            _path(root, name)
        except ValueError as error:
            errors.append(f"{prefix}.license_files: {error}")
    contents = "\n".join(
        (root / name).read_text(encoding="utf-8")
        for name in row["provenance"]["files"]
        if name in row["source_hashes"] and (root / name).is_file()
    )
    for reference in row["provenance"]["references"]:
        if reference not in contents:
            errors.append(
                f"{prefix}.provenance: reference absent from source: {reference}"
            )
    for name in row["source_hashes"]:
        if name.endswith((".py", ".cpp")) and not any(
            name == offered or name.startswith(offered + "/")
            for offered in row["source_offer"]["paths"]
        ):
            errors.append(f"{prefix}.source_offer: missing corresponding source {name}")
    return errors


def _bundle_row(bundle, name):
    # Find the one research-derived bundle record for a declared distribution.
    rows = [
        item for item in bundle.get("distributions", []) if item.get("name") == name
    ]
    if len(rows) != 1:
        raise ValueError("bundle record missing or duplicated")
    return rows[0]


def _evidence_bytes(root, name, bundle_files=None):
    # Read planned bundle bytes during generation and current files during checks.
    if bundle_files is not None and name.startswith("THIRD-PARTY-NOTICES/"):
        if name not in bundle_files:
            raise ValueError(f"planned notice bundle file missing: {name}")
        return bundle_files[name]
    return _path(root, name).read_bytes()


def _dependency_approval(row, root, prefix, bundle, bundle_files=None):
    # An approval cannot replace the dependency's actual metadata and license files.
    name = row["name"]
    if row["approval"] == "not-shipped" and all(
        item["scope"]
        in ("development", "extra:dev", "build-system", "external-executable")
        for item in row["declarations"]
    ):
        return []
    if name == "ohmymeme-plugin-sync-s3":
        if (
            row["approval"] == "approved"
            and row["spdx"] == GPL
            and row["license_files"] == ["LICENSE"]
            and row["provenance_file"] == "plugins/sync.s3/pyproject.toml"
        ):
            return []
        return [f"{prefix}: official dependency approval/license missing; REJECTED"]
    if name == "gmssl":
        try:
            record = _bundle_row(bundle, name)
            metadata = record["metadata"]
            evidence = row.get("evidence")
            if (
                row["approval"] != "frozen-disabled"
                or row["spdx"] != "NOASSERTION"
                or row["license_files"]
                or row["provenance_file"] != metadata["bundle_path"]
                or record["approval"] != "frozen-disabled"
                or record["spdx"] != "NOASSERTION"
                or record["evidence_expression"] != "NOASSERTION"
                or record["files"]
                or not isinstance(evidence, dict)
                or evidence.get("installed_version") != record["version"]
                or evidence.get("metadata_sha256") != metadata["sha256"]
                or evidence.get("metadata_license") != metadata["license"]
                or evidence.get("metadata_license_field") != metadata["license_field"]
                or evidence.get("evidence_expression") != "NOASSERTION"
                or evidence.get("evidence_expression_basis")
                != record["evidence_expression_basis"]
                or evidence.get("source_archive") != record.get("source_archive")
                or evidence.get("license_file_hashes") != {}
            ):
                raise ValueError("gmssl rejected external-runtime evidence drift")
        except (KeyError, ValueError) as error:
            return [f"{prefix}: {error}; REJECTED"]
        return []
    if name == "openssl":
        if (
            row["approval"] == "rejected"
            and row["spdx"] == "NOASSERTION"
            and not row["license_files"]
            and row["provenance_file"] is None
        ):
            return []
        return [f"{prefix}: helper OpenSSL external-runtime evidence drift; REJECTED"]
    if row["approval"] != "approved" or not row["provenance_file"]:
        return [
            f"{prefix}: missing dependency approval/provenance for {name}; REJECTED"
        ]
    try:
        provenance_file = row["provenance_file"]
        if provenance_file.split("/")[0] in ("fixtures", ".omo", "docs"):
            raise ValueError("fixture/report cannot approve a dependency")
        metadata_bytes = _evidence_bytes(root, provenance_file, bundle_files)
        metadata = Parser().parsestr(metadata_bytes.decode("utf-8"))
        actual_name = re.sub(r"[-_.]+", "-", metadata.get("Name", "")).lower()
        rule = DEPENDENCY_RULES.get(name)
        if rule is None:
            raise ValueError("dependency has no fixed offline compatibility rule")
        version, project_url, metadata_license, license_field, spdx, compatibility = (
            rule
        )
        if actual_name != name or metadata.get("Version") != version:
            raise ValueError("dependency name/version differs from actual METADATA")
        actual_license = metadata.get(license_field, "")
        if license_field == "License":
            actual_license = actual_license.splitlines()[0].strip()
        if actual_license != metadata_license:
            raise ValueError("dependency license field differs from actual METADATA")
        if row["spdx"] != spdx:
            raise ValueError("dependency SPDX differs from fixed compatibility rule")
        bundle_row = _bundle_row(bundle, name)
        if (
            bundle_row["approval"] != "approved"
            or bundle_row["version"] != version
            or bundle_row["spdx"] != spdx
            or bundle_row["evidence_expression"] != spdx
            or bundle_row["source_url"] != project_url
            or bundle_row["metadata"]["bundle_path"] != provenance_file
            or bundle_row["metadata"]["sha256"]
            != hashlib.sha256(metadata_bytes).hexdigest()
            or bundle_row["metadata"]["license_field"] != license_field
            or bundle_row["metadata"]["license"] != metadata_license
        ):
            raise ValueError("bundle metadata evidence drift")
        evidence = row.get("evidence")
        if not isinstance(evidence, dict):
            raise ValueError("missing structured dependency evidence")
        if evidence.get("installed_version") != version:
            raise ValueError("dependency evidence version drift")
        if evidence.get("project_url") != project_url:
            raise ValueError("dependency evidence project URL drift")
        if evidence.get("metadata_license") != metadata_license:
            raise ValueError("dependency evidence license field drift")
        if evidence.get("metadata_license_field") != license_field:
            raise ValueError("dependency evidence license field name drift")
        if evidence.get("evidence_expression") != bundle_row["evidence_expression"]:
            raise ValueError("dependency evidence expression drift")
        if (
            evidence.get("evidence_expression_basis")
            != bundle_row["evidence_expression_basis"]
        ):
            raise ValueError("dependency evidence expression basis drift")
        if (
            evidence.get("spdx") != spdx
            or evidence.get("compatibility_rule") != compatibility
        ):
            raise ValueError("dependency evidence compatibility drift")
        if (
            evidence.get("metadata_sha256")
            != hashlib.sha256(metadata_bytes).hexdigest()
        ):
            raise ValueError("dependency METADATA hash mismatch")
        declared_license_files = metadata.get_all("License-File", [])
        if declared_license_files:
            license_files = [
                (Path(provenance_file).parent / item).as_posix()
                for item in declared_license_files
            ]
        else:
            fallback = DEPENDENCY_LICENSE_FALLBACKS.get(name)
            if fallback is None:
                raise ValueError("missing dependency License-File evidence")
            license_files = [
                (Path(provenance_file).parent / item).as_posix() for item in fallback
            ]
        if not license_files or row["license_files"] != license_files:
            raise ValueError("missing dependency License-File evidence")
        if [item["bundle_path"] for item in bundle_row["files"]] != license_files:
            raise ValueError("bundle license or notice path drift")
        if evidence.get("license_file_hashes") != {
            item: hashlib.sha256(_evidence_bytes(root, item, bundle_files)).hexdigest()
            for item in license_files
        }:
            raise ValueError("dependency license file hash mismatch")
        for name in license_files:
            if not _evidence_bytes(root, name, bundle_files).strip():
                raise ValueError(f"empty dependency license: {name}")
    except (OSError, ValueError) as error:
        return [f"{prefix}: {error}; REJECTED"]
    return []


def _native_distribution_approval(row, root, prefix, bundle, bundle_files=None):
    # A shipped native payload is accepted only as a record-bound distribution.
    errors = []
    records = [
        item
        for item in bundle.get("native_distributions", [])
        if item.get("id") == row.get("id")
    ]
    if len(records) != 1:
        return [f"{prefix}: native distribution evidence missing; REJECTED"]
    expected = records[0]
    if row["scope"] != "shipped_distribution":
        errors.append(f"{prefix}.scope: native distribution is not shipped; REJECTED")
    if row["evidence_expression"] == "NOASSERTION":
        errors.append(
            f"{prefix}.evidence_expression: NOASSERTION shipped runtime; REJECTED"
        )
    source_wheel = row["source_wheel"]
    if not source_wheel["url"].startswith("https://files.pythonhosted.org/"):
        errors.append(f"{prefix}.source_wheel.url: outside direct wheel URL; REJECTED")
    if not source_wheel["pypi_json_url"].startswith("https://pypi.org/pypi/"):
        errors.append(
            f"{prefix}.source_wheel.pypi_json_url: outside PyPI URL; REJECTED"
        )
    if not source_wheel["sha256"] or source_wheel["sha256"] == "0" * 64:
        errors.append(f"{prefix}.source_wheel.sha256: missing wheel hash; REJECTED")
    record = row["record"]
    record_rows = {}
    try:
        bundled_record = _evidence_bytes(root, record["bundle_path"], bundle_files)
        source_record = _path(root, record["source_path"]).read_bytes()
    except (OSError, ValueError) as error:
        errors.append(f"{prefix}.record: {error}; REJECTED")
    else:
        if hashlib.sha256(bundled_record).hexdigest() != record["sha256"]:
            errors.append(
                f"{prefix}.record: bundled RECORD byte/hash mismatch; REJECTED"
            )
        if source_record != bundled_record:
            errors.append(f"{prefix}.record: source/bundle byte mismatch; REJECTED")
        try:
            record_rows = build_notice_bundle._record_hashes(bundled_record)
        except ValueError as error:
            errors.append(f"{prefix}.record: {error}; REJECTED")
    wheel = row["wheel_metadata"]
    try:
        bundled_wheel = _evidence_bytes(root, wheel["bundle_path"], bundle_files)
        source_wheel_bytes = _path(root, wheel["source_path"]).read_bytes()
    except (OSError, ValueError) as error:
        errors.append(f"{prefix}.wheel_metadata: {error}; REJECTED")
    else:
        if hashlib.sha256(bundled_wheel).hexdigest() != wheel["sha256"]:
            errors.append(
                f"{prefix}.wheel_metadata: bundled WHEEL byte/hash mismatch; REJECTED"
            )
        if source_wheel_bytes != bundled_wheel:
            errors.append(
                f"{prefix}.wheel_metadata: source/bundle byte mismatch; REJECTED"
            )
        tags = [
            line.removeprefix("Tag: ")
            for line in bundled_wheel.decode("utf-8").splitlines()
            if line.startswith("Tag: ")
        ]
        if wheel["tag"] not in tags:
            errors.append(f"{prefix}.wheel_metadata.tag: WHEEL tag mismatch; REJECTED")
        if not source_wheel["filename"].endswith("-" + wheel["tag"] + ".whl"):
            errors.append(
                f"{prefix}.source_wheel.filename: WHEEL tag mismatch; REJECTED"
            )

    def check_record_file(item, field):
        # Each copied wheel evidence file must retain its source bytes and RECORD hash.
        item_prefix = f"{prefix}.{field}"
        try:
            bundled = _evidence_bytes(root, item["bundle_path"], bundle_files)
            source = _path(root, item["source_path"]).read_bytes()
        except (OSError, ValueError) as error:
            errors.append(f"{item_prefix}: {error}; REJECTED")
            return
        actual_hash = hashlib.sha256(bundled).hexdigest()
        if actual_hash != item["sha256"]:
            errors.append(f"{item_prefix}: bundled byte/hash mismatch; REJECTED")
        if source != bundled:
            errors.append(f"{item_prefix}: source/bundle byte mismatch; REJECTED")
        try:
            record_path = build_notice_bundle._site_packages_path(item["source_path"])
            digest = build_notice_bundle._record_digest(
                record_rows.get(record_path, "")
            )
        except ValueError as error:
            errors.append(f"{item_prefix}: RECORD mapping missing: {error}; REJECTED")
            return
        if digest != actual_hash:
            errors.append(f"{item_prefix}: RECORD hash mismatch; REJECTED")

    if not row["notice_paths"]:
        errors.append(
            f"{prefix}.notice_paths: missing full distribution notice; REJECTED"
        )
    try:
        distribution_record = _bundle_row(bundle, row["distribution"])
    except ValueError as error:
        errors.append(f"{prefix}.notice_paths: {error}; REJECTED")
        distribution_record = {"files": []}
    expected_notices = [item["bundle_path"] for item in distribution_record["files"]]
    if row["notice_paths"] != expected_notices:
        errors.append(f"{prefix}.notice_paths: distribution bundle mismatch; REJECTED")
    for item in row["notice_paths"]:
        try:
            if not _evidence_bytes(root, item, bundle_files).strip():
                raise ValueError(f"empty distribution notice: {item}")
        except (OSError, ValueError) as error:
            errors.append(f"{prefix}.notice_paths: {error}; REJECTED")
    if not row["artifacts"]:
        errors.append(f"{prefix}.artifacts: missing native artifact mapping; REJECTED")
    for index, artifact in enumerate(row["artifacts"]):
        if not artifact["record_path"].endswith((".pyd", ".dll")):
            errors.append(
                f"{prefix}.artifacts[{index}]: non-native RECORD mapping; REJECTED"
            )
        try:
            expected_record_path = build_notice_bundle._site_packages_path(
                artifact["source_path"]
            )
        except ValueError as error:
            errors.append(f"{prefix}.artifacts[{index}]: {error}; REJECTED")
        else:
            if artifact["record_path"] != expected_record_path:
                errors.append(
                    f"{prefix}.artifacts[{index}].record_path: source path mismatch; "
                    "REJECTED"
                )
        try:
            source = _path(root, artifact["source_path"]).read_bytes()
        except (OSError, ValueError) as error:
            errors.append(f"{prefix}.artifacts[{index}]: {error}; REJECTED")
            continue
        actual_hash = hashlib.sha256(source).hexdigest()
        if actual_hash != artifact["sha256"]:
            errors.append(
                f"{prefix}.artifacts[{index}]: source byte/hash mismatch; REJECTED"
            )
        try:
            digest = build_notice_bundle._record_digest(
                record_rows.get(artifact["record_path"], "")
            )
        except ValueError as error:
            errors.append(
                f"{prefix}.artifacts[{index}]: RECORD mapping missing: {error}; "
                "REJECTED"
            )
            continue
        if digest != actual_hash:
            errors.append(
                f"{prefix}.artifacts[{index}]: RECORD hash mismatch; REJECTED"
            )
    for index, item in enumerate(row["sbom_files"]):
        check_record_file(item, f"sbom_files[{index}]")
    for index, item in enumerate(row["evidence_files"]):
        check_record_file(item, f"evidence_files[{index}]")
    if row["component_status"] == "distribution-covered-unmapped" and (
        not row["artifacts"] or not row["notice_paths"] or not record_rows
    ):
        errors.append(
            f"{prefix}.component_status: distribution-covered-unmapped requires "
            "wheel RECORD and full distribution bundle; REJECTED"
        )
    if row != expected:
        errors.append(f"{prefix}: native distribution evidence drift; REJECTED")
    return errors


def _release_component_policy(matrix, facts):
    # Non-shipped helper records must not become a release approval by relabeling.
    errors = []
    components = {
        row.get("id"): row
        for row in matrix.get("release_scope", {}).get("components", [])
        if isinstance(row, dict)
    }
    expected = {
        row["id"]: row for row in facts.get("release_scope", {}).get("components", [])
    }
    for identifier, record in expected.items():
        actual = components.get(identifier)
        if actual is None:
            continue
        if actual.get("scope") != record["scope"]:
            errors.append(
                f"matrix.release_scope.components[{identifier}].scope: external "
                "runtime/build input scope drift; REJECTED"
            )
        if actual.get("excluded_from_targets") != ["pyinstaller", "nuitka"]:
            errors.append(
                f"matrix.release_scope.components[{identifier}].excluded_from_targets: "
                "external component must remain outside frozen targets; REJECTED"
            )
    return errors


def check_matrix(
    matrix,
    schema,
    root=ROOT,
    bundle_index=Path(BUNDLE_INDEX),
    local_evidence_path=build_notice_bundle.FINAL_ENV_EVIDENCE,
    bundle_files=None,
    notice_index=None,
):
    # Source facts and approval evidence are checked before any provider/build work.
    schema_errors = _schema(matrix, schema, schema)
    errors = list(schema_errors)
    if isinstance(matrix, dict):
        if (
            type(matrix.get("schema_version")) is not int
            or matrix.get("schema_version") != 2
        ):
            errors.append("matrix.schema_version: expected integer 2")
        if matrix.get("excluded_distributions") != ["adb.qq", "qq.mobile"]:
            errors.append(
                "matrix.excluded_distributions: expected adb.qq and qq.mobile"
            )
        for row in (
            matrix.get("distributions", [])
            if isinstance(matrix.get("distributions"), list)
            else []
        ):
            if isinstance(row, dict):
                if row.get("id") not in OFFICIAL:
                    errors.append(
                        "matrix.distributions: unknown distribution "
                        f"{row.get('distribution')!r} ({row.get('id')!r})"
                    )
                if row.get("spdx") != GPL:
                    errors.append(
                        f"{row.get('distribution')!r}.spdx: wrong license "
                        f"{row.get('spdx')!r}; expected {GPL}"
                    )
        if isinstance(matrix.get("host"), dict) and matrix["host"].get("spdx") != GPL:
            errors.append(
                "host ohmymeme.spdx: wrong license "
                f"{matrix['host'].get('spdx')!r}; expected {GPL}"
            )
    facts, source_errors, hashes = inspect_sources(
        root, local_evidence_path, notice_index, bundle_files
    )
    errors.extend(source_errors)
    try:
        bundle_name = Path(bundle_index).as_posix()
        bundle_errors = build_notice_bundle.check_bundle(
            root,
            bundle_index,
            local_evidence_path,
            bundle_files,
        )
        errors.extend(f"{error}; REJECTED" for error in bundle_errors)
        bundle_bytes = _evidence_bytes(root, bundle_name, bundle_files)
        bundle = build_notice_bundle._json(bundle_bytes)
    except (OSError, ValueError) as error:
        errors.append(f"notice_bundle.index: {error}; REJECTED")
        bundle = {}
    if schema_errors:
        return errors, facts, hashes
    for field in (
        "hash_policy",
        "host",
        "native_distributions",
        "owned_source_mapping",
        "provider_extras",
        "frozen_official_set",
        "notice_bundle",
        "release_scope",
    ):
        expected = facts[field]
        actual = matrix[field]
        if field == "host":
            actual = {key: actual.get(key) for key in expected}
        errors.extend(_difference(actual, expected, f"matrix.{field}"))
    errors.extend(_legal(matrix["host"], "host ohmymeme", root))
    for field in ("distributions", "source_components", "dependencies"):
        key = "name" if field == "dependencies" else "id"
        rows = matrix[field]
        if [row[key] for row in rows] != [row[key] for row in facts[field]]:
            errors.append(
                f"matrix.{field}: unknown/missing distribution or canonical order drift"
            )
        by_id = {row[key]: row for row in rows}
        for expected in facts[field]:
            identifier = expected[key]
            if identifier not in by_id:
                continue
            row = by_id[identifier]
            prefix = f"matrix.{field}[{identifier}]"
            errors.extend(
                _difference(
                    {name: row.get(name) for name in expected}, expected, prefix
                )
            )
            if field != "dependencies":
                errors.extend(_legal(row, prefix, root))
                if field == "source_components" and row["spdx"] == GPL:
                    paths = SOURCE_COMPONENTS[identifier][2]
                    references = [
                        item
                        for item in SOURCE_COMPONENTS[identifier][3]
                        if item.startswith("https://")
                    ]
                    errors.extend(
                        _difference(
                            row["provenance"]["files"],
                            paths,
                            prefix + ".provenance.files",
                        )
                    )
                    errors.extend(
                        _difference(
                            row["provenance"]["references"],
                            references,
                            prefix + ".provenance.references",
                        )
                    )
                    errors.extend(
                        _difference(
                            row["notice"]["files"],
                            ["LICENSE"] + paths,
                            prefix + ".notice.files",
                        )
                    )
                if (
                    field == "distributions"
                    and row["dependency_approval"]["status"] != "approved"
                ):
                    errors.append(
                        f"{prefix}.dependency_approval: missing approval; REJECTED"
                    )
            else:
                errors.extend(
                    _dependency_approval(row, root, prefix, bundle, bundle_files)
                )
    native_rows = matrix["native_distributions"]
    native_facts = facts["native_distributions"]
    if [row["id"] for row in native_rows] != [row["id"] for row in native_facts]:
        errors.append(
            "matrix.native_distributions: unknown/missing native distribution or "
            "canonical order drift"
        )
    native_by_id = {row["id"]: row for row in native_rows}
    for expected in native_facts:
        identifier = expected["id"]
        row = native_by_id.get(identifier)
        if row is None:
            continue
        prefix = f"matrix.native_distributions[{identifier}]"
        errors.extend(_difference(row, expected, prefix))
        errors.extend(
            _native_distribution_approval(row, root, prefix, bundle, bundle_files)
        )
    errors.extend(_release_component_policy(matrix, facts))
    return errors, facts, hashes


def _materialized_rows(matrix, facts, field, key):
    # Replace only current source-derived row fields while retaining approval text.
    existing = matrix.get(field)
    if not isinstance(existing, list):
        raise ValueError(f"matrix materialization: {field} is not an array")
    rows = {}
    for row in existing:
        if not isinstance(row, dict) or not isinstance(row.get(key), str):
            raise ValueError(f"matrix materialization: malformed {field} row")
        if row[key] in rows:
            raise ValueError(
                f"matrix materialization: duplicate {field} row {row[key]}"
            )
        rows[row[key]] = row
    expected = facts[field]
    expected_ids = [row[key] for row in expected]
    if set(rows) != set(expected_ids):
        raise ValueError(
            f"matrix materialization: {field} approval inventory differs from source"
        )
    return [{**rows[row[key]], **row} for row in expected]


def _write_matrix_file(path, matrix):
    # Publish a complete LF matrix atomically after all source facts are derived.
    data = json.dumps(matrix, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def materialize_matrix(
    root=ROOT,
    matrix_path=Path(MATRIX),
    local_evidence_path=build_notice_bundle.FINAL_ENV_EVIDENCE,
    notice_index=None,
    bundle_files=None,
):
    # Derive a candidate matrix while retaining only source-backed manual policy rows.
    path = _path(root, Path(matrix_path).as_posix())
    matrix = _json(path.read_bytes())
    if not isinstance(matrix, dict):
        raise ValueError("matrix materialization: expected object")
    if notice_index is None:
        notice_index = build_notice_bundle.expected_index(root, local_evidence_path)
    if bundle_files is None:
        bundle_errors = build_notice_bundle.check_bundle(
            root, local_evidence_path=local_evidence_path
        )
    else:
        bundle_errors = build_notice_bundle.check_bundle(
            root,
            local_evidence_path=local_evidence_path,
            files=bundle_files,
        )
    facts, source_errors, _ = inspect_sources(
        root, local_evidence_path, notice_index, bundle_files
    )
    errors = source_errors + [f"notice bundle: {error}" for error in bundle_errors]
    if errors:
        raise ValueError("matrix materialization blocked: " + "; ".join(errors))
    result = copy.deepcopy(matrix)
    host = result.get("host")
    if not isinstance(host, dict):
        raise ValueError("matrix materialization: host is not an object")
    result["hash_policy"] = facts["hash_policy"]
    result["host"] = {**host, **facts["host"]}
    for field, key in (
        ("distributions", "id"),
        ("source_components", "id"),
        ("dependencies", "name"),
    ):
        result[field] = _materialized_rows(result, facts, field, key)
    for field in (
        "native_distributions",
        "owned_source_mapping",
        "provider_extras",
        "frozen_official_set",
        "notice_bundle",
        "release_scope",
    ):
        result[field] = facts[field]
    schema = _json(_path(root, SCHEMA).read_bytes())
    errors, _, _ = check_matrix(
        result,
        schema,
        root,
        Path(BUNDLE_INDEX),
        local_evidence_path,
        bundle_files,
        notice_index,
    )
    if errors:
        raise ValueError("matrix materialization blocked: " + "; ".join(errors))
    return result


def write_matrix(
    root=ROOT,
    matrix_path=Path(MATRIX),
    local_evidence_path=build_notice_bundle.FINAL_ENV_EVIDENCE,
):
    # Publish a fully validated matrix when the existing bundle is already current.
    path = _path(root, Path(matrix_path).as_posix())
    result = materialize_matrix(root, matrix_path, local_evidence_path)
    _write_matrix_file(path, result)
    return _json(path.read_bytes())


def _write_report(path, report):
    # Replace a complete report atomically so an old PASS cannot survive a failure.
    data = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def main(argv=None):
    # Always replace stale success reports with the actual fail-closed result.
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--bundle-index", type=Path, default=Path(BUNDLE_INDEX))
    parser.add_argument(
        "--local-evidence",
        type=Path,
        default=build_notice_bundle.FINAL_ENV_EVIDENCE,
    )
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    report = {
        "schema_version": 2,
        "status": "REJECTED",
        "provider_execution": False,
        "build_execution": False,
        "errors": [],
    }
    try:
        schema_raw, matrix_raw = args.schema.read_bytes(), args.matrix.read_bytes()
        schema, matrix = _json(schema_raw), _json(matrix_raw)
        errors, facts, hashes = check_matrix(
            matrix,
            schema,
            bundle_index=args.bundle_index,
            local_evidence_path=args.local_evidence,
        )
        report.update(
            {
                "errors": errors,
                "observed": facts,
                "input_hashes": hashes,
                "hash_policy": facts.get("hash_policy", {}),
                "input_hash_modes": facts.get("hash_policy", {}).get(
                    "input_hash_modes", {}
                ),
                "schema_sha256": build_notice_bundle.matrix_hash(
                    args.schema.as_posix(), schema_raw
                ),
                "matrix_sha256": build_notice_bundle.matrix_hash(
                    args.matrix.as_posix(), matrix_raw
                ),
                "bundle_index": args.bundle_index.as_posix(),
                "local_evidence_path": args.local_evidence.as_posix(),
                "validator_sha256": build_notice_bundle.matrix_hash(
                    Path(__file__).resolve().relative_to(ROOT).as_posix(),
                    Path(__file__).read_bytes(),
                ),
            }
        )
        if not errors:
            report["status"] = "PASS"
    except Exception as error:
        report["errors"] = [str(error) or type(error).__name__]
    _write_report(args.report, report)
    for error in report["errors"]:
        print(error, file=sys.stderr)
    print(f"{report['status']}: license matrix (providers/build not executed)")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
