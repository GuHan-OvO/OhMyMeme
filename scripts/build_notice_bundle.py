#!/usr/bin/env python3
# pyright: basic
"""Build and verify the byte-exact Todo14 notice bundle."""

import argparse
import base64
import csv
import hashlib
import json
import os
import sys
import tempfile
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_RESEARCH = Path(
    ".omo/evidence/pluginized-recomposition-parity/"
    "todo-14-license-source-research.json"
)
HELPER_RESEARCH = Path(
    ".omo/evidence/pluginized-recomposition-parity/"
    "todo-14-helper-local-provenance.json"
)
FINAL_ENV_EVIDENCE = Path(
    ".omo/evidence/pluginized-recomposition-parity/"
    "todo-14-final-environment-installation.json"
)
CURL_CFFI_OFFICIAL_RECORD_SHA256 = (
    "be2f44dc3d91b1a64381a7fc7f663e11eff79a901b52dd3f3f57a140fb06481f"
)
BUNDLE = Path("THIRD-PARTY-NOTICES")
INDEX = BUNDLE / "license-evidence.json"
MATRIX = Path("docs/plugin-license-matrix.json")
STAGING = "fixtures/plugin-parity/frozen-staging"
BUNDLE_VERSION = "todo14-final-2026-09-18-r4"
HASH_POLICY_ID = "todo14-path-policy-v1"
HASH_MODE_TEXT = "utf8-lf-normalized"
HASH_MODE_RAW = "raw-bytes"
APPROVED_SPDX = {
    "boto3": "Apache-2.0",
    "botocore": "Apache-2.0",
    "bottle": "MIT",
    "cryptography": "Apache-2.0 OR BSD-3-Clause",
    "curl-cffi": "MIT",
    "keyboard": "MIT",
    "pillow": "MIT-CMU",
    "pydantic": "MIT",
    "pypinyin": "MIT",
    "pyinstaller": "GPL-2.0-or-later WITH Bootloader-exception",
    "pyperclip": "BSD-3-Clause",
    "pystray": "LGPL-3.0-only",
    "pywebview": "BSD-3-Clause",
    "tgcrypto": "LGPL-3.0-or-later",
}
EVIDENCE_EXPRESSIONS = {
    "boto3": (
        "Apache-2.0",
        "The retained full LICENSE is Apache License, Version 2.0; this is "
        "release evidence, not a synthesized PEP 639 metadata field.",
    ),
    "botocore": (
        "Apache-2.0",
        "The retained full LICENSE.txt is Apache License, Version 2.0; this "
        "is release evidence, not a synthesized PEP 639 metadata field.",
    ),
    "bottle": (
        "MIT",
        "The retained full LICENSE contains the MIT permission and notice text.",
    ),
    "cryptography": (
        "Apache-2.0 OR BSD-3-Clause",
        "The installed PEP 639 License-Expression and all retained license "
        "files state this expression.",
    ),
    "curl-cffi": (
        "MIT",
        "The installed PEP 639 License-Expression and retained distribution "
        "LICENSE state MIT; it does not separately license an internal DLL.",
    ),
    "keyboard": (
        "MIT",
        "The retained legacy-installed LICENSE.txt contains the MIT text.",
    ),
    "pillow": (
        "MIT-CMU",
        "The installed PEP 639 License-Expression is MIT-CMU and the complete "
        "1617-line Pillow LICENSE is retained without reduction.",
    ),
    "pydantic": (
        "MIT",
        "The installed PEP 639 License-Expression and retained LICENSE state MIT.",
    ),
    "pypinyin": (
        "MIT",
        "The retained legacy License field and installed LICENSE.txt state MIT.",
    ),
    "pyinstaller": (
        "GPL-2.0-or-later WITH Bootloader-exception",
        "The retained COPYING.txt states the Bootloader Exception and its "
        "file-specific Apache-2.0 and MIT scopes.",
    ),
    "pyperclip": (
        "BSD-3-Clause",
        "The retained full LICENSE.txt has the three redistribution clauses, "
        "including the no-endorsement clause.",
    ),
    "pystray": (
        "LGPL-3.0-only",
        "The retained COPYING.LGPL is the complete GNU Lesser General Public "
        "License Version 3 text. This does not add a PEP 639 field to legacy "
        "metadata.",
    ),
    "pywebview": (
        "BSD-3-Clause",
        "The retained full LICENSE is headed BSD 3-Clause License and contains "
        "all three clauses.",
    ),
    "tgcrypto": (
        "LGPL-3.0-or-later",
        "The retained NOTICE says LGPL version 3 or, at the recipient option, "
        "any later version; COPYING and COPYING.lesser are retained.",
    ),
}
GMSSL_SOURCE_ARCHIVE = {
    "pypi_json_url": "https://pypi.org/pypi/gmssl/3.2.2/json",
    "filename": "gmssl-3.2.2.linux-x86_64.tar.gz",
    "url": (
        "https://files.pythonhosted.org/packages/06/29/"
        "008dc5071656ac522282f87accc837643be1fa9e7d021f890c1cdf390714/"
        "gmssl-3.2.2.linux-x86_64.tar.gz"
    ),
    "sha256": "f3d8c8c75dd34cd169f129c017f67fdd80cce2c67a13f9a0e3b1c58f8de6351e",
    "inspection": (
        "Downloaded via TLS from the PyPI JSON sdist URL during Todo14. The "
        "verified archive contains no LICENSE, COPYING, NOTICE, or other full "
        "license text; its PKG-INFO only says License: BSD and describes a "
        "class BSD license. No precise BSD expression is asserted."
    ),
}
NATIVE_WHEEL_SPECS = (
    {
        "id": "cryptography-wheel",
        "distribution": "cryptography",
        "artifact_paths": (
            ".venv/Lib/site-packages/cryptography/hazmat/bindings/_rust.pyd",
        ),
        "artifact_globs": ("cryptography/**/*.pyd",),
        "sbom_paths": (
            ".venv/Lib/site-packages/cryptography-50.0.1.dist-info/" "sboms/sbom.json",
            ".venv/Lib/site-packages/cryptography-50.0.1.dist-info/"
            "sboms/cryptography-rust.cyclonedx.json",
        ),
        "extra_evidence_paths": (),
        "record_sha256": (
            "8190d6f8072ba1df8308f7390c84b23db40466f30d79287ffa6e0465702986eb"
        ),
        "wheel_metadata_sha256": (
            "9efe56059dbb731c438c822ac4132a2b670e174db90f09059aa246a8bdd2a575"
        ),
        "source_wheel": {
            "filename": "cryptography-50.0.1-cp311-abi3-win_amd64.whl",
            "url": (
                "https://files.pythonhosted.org/packages/42/8b/"
                "cb12b1b60c91b074ca6bf0fdd59aa8f10d8bc5f73af8faece86ef0421b37/"
                "cryptography-50.0.1-cp311-abi3-win_amd64.whl"
            ),
            "sha256": (
                "aed8db4f6d71c51efb89530e12d9464e7bf2923d46c3205dc794a2a93f8c0648"
            ),
            "pypi_json_url": "https://pypi.org/pypi/cryptography/50.0.1/json",
        },
        "component_status": "distribution-covered",
        "limitation": (
            "This is distribution-level evidence for the shipped cryptography "
            "wheel. Its SBOM is retained, but this record does not assert an "
            "individual upstream-library link map or apply to wechat_keyfinder."
        ),
    },
    {
        "id": "pillow-wheel",
        "distribution": "pillow",
        "artifact_paths": (
            ".venv/Lib/site-packages/PIL/_avif.cp312-win_amd64.pyd",
            ".venv/Lib/site-packages/PIL/_imaging.cp312-win_amd64.pyd",
            ".venv/Lib/site-packages/PIL/_imagingcms.cp312-win_amd64.pyd",
            ".venv/Lib/site-packages/PIL/_imagingft.cp312-win_amd64.pyd",
            ".venv/Lib/site-packages/PIL/_imagingmath.cp312-win_amd64.pyd",
            ".venv/Lib/site-packages/PIL/_imagingmorph.cp312-win_amd64.pyd",
            ".venv/Lib/site-packages/PIL/_imagingtk.cp312-win_amd64.pyd",
            ".venv/Lib/site-packages/PIL/_webp.cp312-win_amd64.pyd",
        ),
        "artifact_globs": ("PIL/**/*.pyd",),
        "sbom_paths": (
            ".venv/Lib/site-packages/pillow-12.3.0.dist-info/sboms/"
            "pillow-12.3.0.cdx.json",
        ),
        "extra_evidence_paths": (),
        "record_sha256": (
            "7b196b1c727044decb06abf90984be2a3f99443f0027b1a6ee99307a23b0800e"
        ),
        "wheel_metadata_sha256": (
            "ad1e507ec59c665de6adae409920fb15dd1dcd0c59de51c2a43a3bd0891f0cae"
        ),
        "source_wheel": {
            "filename": "pillow-12.3.0-cp312-cp312-win_amd64.whl",
            "url": (
                "https://files.pythonhosted.org/packages/45/89/"
                "da2f7971a317f83d807fdd4065c0af40208e59e692cc43d315a71a0e96d1/"
                "pillow-12.3.0-cp312-cp312-win_amd64.whl"
            ),
            "sha256": (
                "a2b55dd6b2a4c4b7d87ffa56bdb33fdc5fdb9a462173861a7bc097f17d91cb09"
            ),
            "pypi_json_url": "https://pypi.org/pypi/Pillow/12.3.0/json",
        },
        "component_status": "distribution-covered",
        "limitation": (
            "This is distribution-level evidence for the shipped Pillow wheel. "
            "The complete Pillow LICENSE and wheel SBOM are retained, but this "
            "record does not claim a per-library static or dynamic link map."
        ),
    },
    {
        "id": "curl-cffi-wheel",
        "distribution": "curl-cffi",
        "artifact_paths": (
            ".venv/Lib/site-packages/curl_cffi/_wrapper.pyd",
            ".venv/Lib/site-packages/curl_cffi.libs/"
            "libcurl-impersonate-1172405423dc93b7aeed557f2a2ca325.dll",
        ),
        "artifact_globs": ("curl_cffi/**/*.pyd", "curl_cffi.libs/**/*.dll"),
        "sbom_paths": (),
        "extra_evidence_paths": (
            ".venv/Lib/site-packages/curl_cffi-0.16.3.dist-info/DELVEWHEEL",
        ),
        "local_evidence": "curl-cffi",
        "source_wheel": {
            "filename": "curl_cffi-0.16.3-cp310-abi3-win_amd64.whl",
            "url": (
                "https://files.pythonhosted.org/packages/9b/72/"
                "1732a24ef4a2aeba994b80ec163debe8deda403c07e4abbc0443bca078b8/"
                "curl_cffi-0.16.3-cp310-abi3-win_amd64.whl"
            ),
            "sha256": (
                "fe87b66e324ed7318166698e02169f3208dbda32b872a27d2bc61a9c19b335eb"
            ),
            "pypi_json_url": "https://pypi.org/pypi/curl-cffi/0.16.3/json",
        },
        "component_status": "distribution-covered-unmapped",
        "limitation": (
            "The DLL and wrapper are covered by the exact installed curl-cffi "
            "wheel RECORD and the retained full distribution LICENSE. No exact "
            "curl-impersonate source revision or separate component notice set is "
            "bound to this DLL, so it is not asserted to be independently MIT."
        ),
    },
)
ORDER = (
    "boto3",
    "botocore",
    "bottle",
    "cryptography",
    "curl-cffi",
    "gmssl",
    "keyboard",
    "pillow",
    "pydantic",
    "pypinyin",
    "pyinstaller",
    "pyperclip",
    "pystray",
    "pywebview",
    "tgcrypto",
)


def _duplicates(pairs):
    # Prevent a second JSON key from replacing the evidence it is supposed to bind.
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _json(raw):
    # Evidence JSON is strict: NaN and duplicate keys are not accepted.
    def reject(value):
        raise ValueError(f"invalid JSON constant: {value}")

    return json.loads(raw, object_pairs_hook=_duplicates, parse_constant=reject)


def _sha256(data):
    # Hash raw bytes rather than decoded text so copied notices stay byte-exact.
    return hashlib.sha256(data).hexdigest()


def _utf8_lf_bytes(data):
    # Normalize only controlled UTF-8 text before source and staging comparisons.
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("utf8-lf-normalized hash requires UTF-8 text") from error
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def matrix_hash_mode(name):
    # Select the documented path policy without weakening raw artifact provenance.
    name = str(name).replace("\\", "/")
    canonical_manifests = {
        "config/plugin-manifest.json",
        f"{STAGING}/ohmymeme/config/plugin-manifest.json",
    }
    if name in canonical_manifests:
        return HASH_MODE_RAW
    if (
        name in {"LICENSE", "NOTICE"}
        or name.startswith("LICENSES/")
        or name.startswith("THIRD-PARTY-NOTICES/")
        or name.startswith(".venv/")
        or "/.dist-info/licenses/" in name
        or name.endswith("/RECORD")
        or name.endswith((".dll", ".exe", ".lib", ".obj", ".pdb", ".pyd"))
    ):
        return HASH_MODE_RAW
    if name.startswith(f"{STAGING}/"):
        if name.endswith((".py", "/METADATA", "/entry_points.txt", "/top_level.txt")):
            return HASH_MODE_TEXT
        if name == f"{STAGING}/staging-manifest.json":
            return HASH_MODE_TEXT
        return HASH_MODE_RAW
    if name in {"requirements.txt", "requirements-dev.txt", "environment.yml"}:
        return HASH_MODE_TEXT
    if name.endswith(
        (".py", ".cpp", ".h", ".hpp", ".toml", ".json", ".md", ".yml", ".yaml")
    ):
        return HASH_MODE_TEXT
    if name.endswith("/CMakeLists.txt"):
        return HASH_MODE_TEXT
    return HASH_MODE_RAW


def matrix_hash(name, data):
    # Hash a matrix input according to its path-specific evidence policy.
    mode = matrix_hash_mode(name)
    if mode == HASH_MODE_TEXT:
        return _sha256(_utf8_lf_bytes(data))
    return _sha256(data)


def matrix_hash_policy(input_hash_modes=None):
    # Describe the source/staging and artifact hash modes carried by Todo14 evidence.
    policy = {
        "id": HASH_POLICY_ID,
        "hash_mode": "path-policy-v1",
        "text_hash_mode": HASH_MODE_TEXT,
        "raw_hash_mode": HASH_MODE_RAW,
        "canonical_manifest_hash_mode": HASH_MODE_RAW,
        "canonical_manifest_path": "config/plugin-manifest.json",
    }
    if input_hash_modes is None:
        return policy
    return {**policy, "input_hash_modes": dict(sorted(input_hash_modes.items()))}


def _path(root, name):
    # Every referenced source and bundle file must remain inside this worktree.
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise ValueError(f"unsafe path: {name!r}")
    if name.startswith("/") or any(part in ("", ".", "..") for part in name.split("/")):
        raise ValueError(f"unsafe path: {name!r}")
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"path outside worktree: {name}")
    return path


def _source_url(row):
    # Preserve the version-bound upstream source recorded by the prior research.
    source = row.get("official_sources", {}).get("source_url")
    if not isinstance(source, str) or not source:
        raise ValueError("research source URL is missing")
    return source


def _metadata_license(metadata):
    # Keep PEP 639 and legacy metadata distinct instead of inferring a new field.
    expression = metadata.get("license_expression")
    if isinstance(expression, str) and expression:
        return "License-Expression", expression
    license_value = metadata.get("license")
    if isinstance(license_value, str) and license_value:
        return "License", license_value
    raise ValueError("research metadata license field is missing")


def _research(root):
    # Load the two existing local research records; this command never performs I/O.
    source_path = _path(root, SOURCE_RESEARCH.as_posix())
    helper_path = _path(root, HELPER_RESEARCH.as_posix())
    if not source_path.is_file() or not helper_path.is_file():
        raise ValueError("Todo14 research record missing")
    source = _json(source_path.read_bytes())
    helper = _json(helper_path.read_bytes())
    if (
        source.get("task")
        != "pluginized-recomposition-parity/Todo14/license-source-research"
    ):
        raise ValueError("research task identity drift")
    if (
        helper.get("task")
        != "pluginized-recomposition-parity/Todo14/helper-local-provenance-wave5"
    ):
        raise ValueError("helper research task identity drift")
    return source, helper


def _final_environment_evidence(root, evidence_path, spec):
    # Load explicit final-installation evidence without accepting stale records.
    path = _path(root, Path(evidence_path).as_posix())
    evidence = _json(path.read_bytes())
    if set(evidence) != {
        "schema_version",
        "task",
        "research_only",
        "curl_cffi",
        "supersedes",
        "distribution_overrides",
        "additional_distributions",
    }:
        raise ValueError("final environment: malformed installation evidence")
    curl_cffi = evidence["curl_cffi"]
    if not isinstance(curl_cffi, dict) or set(curl_cffi) != {
        "distribution",
        "version",
        "source_wheel",
        "official_wheel_record_sha256",
        "installed",
        "assessment",
    }:
        raise ValueError("curl-cffi: malformed local installation evidence")
    if (
        evidence["schema_version"] != 1
        or evidence["task"]
        != "pluginized-recomposition-parity/Todo14/final-environment-installation"
        or evidence["research_only"] is not True
        or curl_cffi["distribution"] != "curl-cffi"
        or curl_cffi["version"] != "0.16.3"
        or curl_cffi["source_wheel"] != spec["source_wheel"]
        or curl_cffi["official_wheel_record_sha256"] != CURL_CFFI_OFFICIAL_RECORD_SHA256
    ):
        raise ValueError("curl-cffi: local installation identity/source wheel drift")
    installed = curl_cffi["installed"]
    if not isinstance(installed, dict) or set(installed) != {
        "metadata",
        "record",
        "wheel_metadata",
        "launcher",
        "artifacts",
        "evidence_files",
    }:
        raise ValueError("curl-cffi: local installation fields missing")
    for name in ("metadata", "record", "wheel_metadata"):
        value = installed[name]
        if not isinstance(value, dict) or set(value) != {"source_path", "sha256"}:
            raise ValueError(f"curl-cffi: malformed local {name} evidence")
        if not isinstance(value["source_path"], str) or not isinstance(
            value["sha256"], str
        ):
            raise ValueError(f"curl-cffi: local {name} path/hash missing")
    launcher = installed["launcher"]
    if (
        not isinstance(launcher, dict)
        or set(launcher)
        != {
            "source_path",
            "record_path",
            "sha256",
        }
        or not all(isinstance(launcher[key], str) for key in launcher)
    ):
        raise ValueError("curl-cffi: malformed local launcher evidence")
    for name in ("artifacts", "evidence_files"):
        values = installed[name]
        if not isinstance(values, list) or not values:
            raise ValueError(f"curl-cffi: local {name} missing")
        expected = {"source_path", "sha256"}
        if name == "artifacts":
            expected.add("record_path")
        for value in values:
            if not isinstance(value, dict) or set(value) != expected:
                raise ValueError(f"curl-cffi: malformed local {name} entry")
            if not all(isinstance(value[key], str) for key in expected):
                raise ValueError(f"curl-cffi: local {name} path/hash missing")
    supersedes = evidence["supersedes"]
    if not isinstance(supersedes, dict) or set(supersedes) != {
        "wave5_license_source_research",
        "wave5_bundle_version",
        "wave5_record_sha256",
        "wave5_launcher_record",
    }:
        raise ValueError("curl-cffi: superseded Wave5 evidence missing")
    wave5 = supersedes["wave5_license_source_research"]
    if not isinstance(wave5, dict) or set(wave5) != {"path", "sha256"}:
        raise ValueError("curl-cffi: malformed superseded Wave5 research")
    if (
        not isinstance(wave5["path"], str)
        or not isinstance(wave5["sha256"], str)
        or _sha256(_path(root, wave5["path"]).read_bytes()) != wave5["sha256"]
        or supersedes["wave5_bundle_version"] != "todo14-wave5-2026-09-18-r3"
        or not isinstance(supersedes["wave5_record_sha256"], str)
        or supersedes["wave5_record_sha256"] == installed["record"]["sha256"]
        or not isinstance(supersedes["wave5_launcher_record"], str)
        or not supersedes["wave5_launcher_record"].startswith(
            "../../Scripts/curl-cffi.exe,sha256="
        )
    ):
        raise ValueError("curl-cffi: superseded Wave5 evidence drift")
    assessment = curl_cffi["assessment"]
    if (
        not isinstance(assessment, dict)
        or set(assessment)
        != {
            "status",
            "dll_license_status",
        }
        or assessment
        != {
            "status": "legitimate-installed-record-rewrite",
            "dll_license_status": "NOASSERTION",
        }
    ):
        raise ValueError("curl-cffi: local installation assessment drift")
    if not isinstance(evidence["distribution_overrides"], dict):
        raise ValueError("final environment: distribution overrides missing")
    if not isinstance(evidence["additional_distributions"], dict):
        raise ValueError("final environment: additional distributions missing")
    return evidence


def _final_distribution_overrides(root, evidence, research, candidates):
    # Apply only hash-verified final-environment replacements for stale packages.
    overrides = evidence["distribution_overrides"]
    if set(overrides) != {"boto3", "botocore"}:
        raise ValueError("final environment: unexpected distribution overrides")
    rows = {}
    current_candidates = list(candidates)
    for name in ("boto3", "botocore"):
        override = overrides[name]
        base = research["approved_candidates"].get(name)
        if (
            not isinstance(override, dict)
            or base is None
            or set(override)
            != {
                "version",
                "metadata",
                "files",
                "official_sources",
                "supersedes",
            }
        ):
            raise ValueError(f"{name}: malformed final environment override")
        version = override["version"]
        metadata = override["metadata"]
        files = override["files"]
        official = override["official_sources"]
        supersedes = override["supersedes"]
        prefix = f".venv/Lib/site-packages/{name}-{version}.dist-info/"
        if (
            not isinstance(version, str)
            or not isinstance(metadata, dict)
            or set(metadata) != {"path", "sha256"}
            or metadata["path"] != prefix + "METADATA"
            or not isinstance(metadata["sha256"], str)
            or _sha256(_path(root, metadata["path"]).read_bytes()) != metadata["sha256"]
        ):
            raise ValueError(f"{name}: final metadata evidence drift")
        old_prefix = str(Path(base["metadata"]["path"]).parent).replace("\\", "/")
        old_files = [
            item
            for item in candidates
            if item.get("source_path", "").startswith(old_prefix + "/")
        ]
        if not isinstance(files, list) or len(files) != len(old_files):
            raise ValueError(f"{name}: final notice evidence differs from Wave5")
        expected_notices = {
            (item["suggested_bundle_path"], item["role"]) for item in old_files
        }
        actual_notices = set()
        for item in files:
            if not isinstance(item, dict) or set(item) != {
                "source_path",
                "suggested_bundle_path",
                "role",
                "sha256",
            }:
                raise ValueError(f"{name}: malformed final notice evidence")
            if (
                not isinstance(item["source_path"], str)
                or not item["source_path"].startswith(prefix)
                or not isinstance(item["sha256"], str)
                or _sha256(_path(root, item["source_path"]).read_bytes())
                != item["sha256"]
            ):
                raise ValueError(f"{name}: final notice source hash drift")
            actual_notices.add((item["suggested_bundle_path"], item["role"]))
        if actual_notices != expected_notices or len(actual_notices) != len(files):
            raise ValueError(f"{name}: final notice scope differs from Wave5")
        if not isinstance(official, dict) or set(official) != {
            "pypi_json_url",
            "source_url",
            "source_ref",
            "wheel",
        }:
            raise ValueError(f"{name}: official final source evidence missing")
        wheel = official["wheel"]
        if (
            official["pypi_json_url"] != f"https://pypi.org/pypi/{name}/{version}/json"
            or official["source_url"] != base["official_sources"]["source_url"]
            or official["source_ref"] != version
            or not isinstance(wheel, dict)
            or set(wheel) != {"filename", "url", "sha256"}
            or wheel["filename"] != f"{name}-{version}-py3-none-any.whl"
            or not isinstance(wheel["url"], str)
            or not wheel["url"].startswith("https://files.pythonhosted.org/")
            or not isinstance(wheel["sha256"], str)
            or len(wheel["sha256"]) != 64
        ):
            raise ValueError(f"{name}: official final source evidence drift")
        if not isinstance(supersedes, dict) or supersedes != {
            "wave5_version": base["version"],
            "wave5_metadata_sha256": base["metadata"]["sha256"],
        }:
            raise ValueError(f"{name}: superseded Wave5 evidence drift")
        row = dict(base)
        row["version"] = version
        row["metadata"] = {**base["metadata"], **metadata}
        row["official_sources"] = {
            **base["official_sources"],
            "pypi_metadata": official["pypi_json_url"],
            "source_ref": version,
        }
        rows[name] = row
        current_candidates = [
            item
            for item in current_candidates
            if not item.get("source_path", "").startswith(old_prefix + "/")
        ]
        current_candidates.extend(files)
    return rows, current_candidates


def _final_additional_distributions(root, evidence):
    # Bind newly declared current dependencies without rewriting Wave5 research.
    additions = evidence["additional_distributions"]
    if set(additions) != {"pypinyin"}:
        raise ValueError("final environment: unexpected additional distributions")
    row = additions["pypinyin"]
    if not isinstance(row, dict) or set(row) != {
        "version",
        "metadata",
        "files",
        "official_sources",
    }:
        raise ValueError("pypinyin: malformed final environment evidence")
    version = row["version"]
    metadata = row["metadata"]
    prefix = f".venv/Lib/site-packages/pypinyin-{version}.dist-info/"
    if (
        version != "0.55.0"
        or not isinstance(metadata, dict)
        or set(metadata) != {"path", "sha256", "license"}
        or metadata["path"] != prefix + "METADATA"
        or metadata["license"] != "MIT"
        or not isinstance(metadata["sha256"], str)
        or _sha256(_path(root, metadata["path"]).read_bytes()) != metadata["sha256"]
    ):
        raise ValueError("pypinyin: final metadata evidence drift")
    metadata_text = _path(root, metadata["path"]).read_text(encoding="utf-8")
    if any(
        field not in "\n" + metadata_text
        for field in (
            "\nName: pypinyin\n",
            f"\nVersion: {version}\n",
            "\nLicense: MIT\n",
            "\nLicense-File: LICENSE.txt\n",
        )
    ):
        raise ValueError("pypinyin: final metadata fields drift")
    files = row["files"]
    expected_file = {
        "source_path": prefix + "licenses/LICENSE.txt",
        "suggested_bundle_path": "THIRD-PARTY-NOTICES/pypinyin/LICENSE.txt",
        "role": "license",
    }
    if not isinstance(files, list) or len(files) != 1:
        raise ValueError("pypinyin: final license evidence missing")
    item = files[0]
    if (
        not isinstance(item, dict)
        or set(item) != {*expected_file, "sha256"}
        or any(item[key] != value for key, value in expected_file.items())
        or not isinstance(item["sha256"], str)
        or _sha256(_path(root, item["source_path"]).read_bytes()) != item["sha256"]
    ):
        raise ValueError("pypinyin: final license evidence drift")
    official = row["official_sources"]
    if (
        not isinstance(official, dict)
        or set(official) != {"pypi_json_url", "source_url", "source_ref"}
        or official
        != {
            "pypi_json_url": f"https://pypi.org/pypi/pypinyin/{version}/json",
            "source_url": "https://github.com/mozillazg/python-pinyin",
            "source_ref": version,
        }
    ):
        raise ValueError("pypinyin: final source evidence drift")
    return (
        {
            "pypinyin": {
                "version": version,
                "metadata": metadata,
                "official_sources": official,
            }
        },
        files,
    )


def _entry(name, row, candidates, approved):
    # Bind one distribution to its exact metadata and license or notice bytes.
    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError(f"{name}: research metadata missing")
    metadata_path = metadata.get("path")
    metadata_sha256 = metadata.get("sha256")
    if not isinstance(metadata_path, str) or not isinstance(metadata_sha256, str):
        raise ValueError(f"{name}: research metadata path/hash missing")
    prefix = str(Path(metadata_path).parent).replace("\\", "/") + "/"
    files = [
        item for item in candidates if item.get("source_path", "").startswith(prefix)
    ]
    if name != "gmssl" and not files:
        raise ValueError(f"{name}: research license/notice candidates missing")
    field, license_value = _metadata_license(metadata)
    bundle_name = name
    evidence_expression, evidence_basis = EVIDENCE_EXPRESSIONS.get(
        name,
        (
            "NOASSERTION",
            "No complete license text establishes a precise evidence expression.",
        ),
    )
    entry = {
        "name": name,
        "version": row.get("version"),
        "approval": "approved" if approved else "frozen-disabled",
        "spdx": APPROVED_SPDX[name] if approved else "NOASSERTION",
        "evidence_expression": evidence_expression if approved else "NOASSERTION",
        "evidence_expression_basis": evidence_basis,
        "source_url": _source_url(row),
        "metadata": {
            "source_path": metadata_path,
            "bundle_path": f"THIRD-PARTY-NOTICES/{bundle_name}/METADATA",
            "sha256": metadata_sha256,
            "license_field": field,
            "license": license_value,
        },
        "files": [
            {
                "source_path": item["source_path"],
                "bundle_path": item["suggested_bundle_path"],
                "role": item["role"],
                "sha256": item["sha256"],
            }
            for item in files
        ],
    }
    if name == "gmssl":
        entry["source_archive"] = GMSSL_SOURCE_ARCHIVE
    return entry


def _site_packages_path(name):
    # RECORD paths are relative to the installed site-packages directory.
    prefix = ".venv/Lib/site-packages/"
    if not name.startswith(prefix):
        raise ValueError(f"native evidence is outside site-packages: {name}")
    return name[len(prefix) :]


def _record_hashes(record_bytes):
    # Decode wheel RECORD hashes without treating the unrecorded RECORD file as data.
    rows = {}
    try:
        reader = csv.reader(StringIO(record_bytes.decode("utf-8")))
        for row in reader:
            if len(row) != 3 or not row[0]:
                raise ValueError("malformed RECORD row")
            if row[0] in rows:
                raise ValueError(f"duplicate RECORD path: {row[0]}")
            rows[row[0]] = row[1]
    except (csv.Error, UnicodeDecodeError) as error:
        raise ValueError(f"malformed RECORD: {error}") from error
    return rows


def _record_digest(value):
    # Wheel RECORD uses URL-safe base64 SHA-256 values without padding.
    if not value.startswith("sha256="):
        raise ValueError(f"unsupported RECORD digest: {value!r}")
    encoded = value.removeprefix("sha256=")
    try:
        return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).hex()
    except ValueError as error:
        raise ValueError(f"malformed RECORD SHA-256: {value!r}") from error


def _native_distributions(root, research, helper_research, rows, final_evidence):
    # Bind native payloads to one exact wheel, RECORD, notices, and optional SBOMs.
    candidate_rows = {row["name"]: row for row in rows}
    native_hashes = helper_research["local_hashes"]["native_and_sbom"]
    site_packages = root / ".venv" / "Lib" / "site-packages"
    local_evidence = {"curl-cffi": final_evidence["curl_cffi"]["installed"]}
    result = []
    for spec in NATIVE_WHEEL_SPECS:
        name = spec["distribution"]
        candidate = candidate_rows.get(name)
        if candidate is None or candidate["approval"] != "approved":
            raise ValueError(f"{name}: approved distribution evidence missing")
        metadata_source = candidate["metadata"]["source_path"]
        dist_info = str(Path(metadata_source).parent).replace("\\", "/")
        record_source = f"{dist_info}/RECORD"
        wheel_source = f"{dist_info}/WHEEL"
        installed_evidence = local_evidence.get(name)
        if installed_evidence is not None:
            metadata = installed_evidence["metadata"]
            record_evidence = installed_evidence["record"]
            wheel_evidence = installed_evidence["wheel_metadata"]
            if metadata != {
                "source_path": metadata_source,
                "sha256": candidate["metadata"]["sha256"],
            }:
                raise ValueError(
                    f"{name}: local metadata evidence differs from research"
                )
            if record_evidence["source_path"] != record_source:
                raise ValueError(f"{name}: local RECORD path differs from metadata")
            if wheel_evidence["source_path"] != wheel_source:
                raise ValueError(f"{name}: local WHEEL path differs from metadata")
            evidence_artifacts = installed_evidence["artifacts"]
            evidence_files = installed_evidence["evidence_files"]
            if {item["source_path"] for item in evidence_artifacts} != set(
                spec["artifact_paths"]
            ) or len(evidence_artifacts) != len(spec["artifact_paths"]):
                raise ValueError(f"{name}: local native artifact evidence differs")
            if {item["source_path"] for item in evidence_files} != set(
                spec["extra_evidence_paths"]
            ) or len(evidence_files) != len(spec["extra_evidence_paths"]):
                raise ValueError(f"{name}: local native evidence differs")
            for item in evidence_artifacts:
                if item["record_path"] != _site_packages_path(item["source_path"]):
                    raise ValueError(f"{name}: local artifact RECORD path differs")
            launcher = installed_evidence["launcher"]
            if (
                launcher["source_path"] != ".venv/Scripts/curl-cffi.exe"
                or launcher["record_path"] != "../../Scripts/curl-cffi.exe"
            ):
                raise ValueError(f"{name}: local launcher path differs")
            expected_hashes = {
                item["source_path"]: item["sha256"]
                for item in evidence_artifacts + evidence_files
            }
            record_sha256 = record_evidence["sha256"]
            wheel_metadata_sha256 = wheel_evidence["sha256"]
        else:
            expected_hashes = native_hashes
            record_sha256 = spec["record_sha256"]
            wheel_metadata_sha256 = spec["wheel_metadata_sha256"]
        record_bytes = _path(root, record_source).read_bytes()
        wheel_bytes = _path(root, wheel_source).read_bytes()
        if _sha256(record_bytes) != record_sha256:
            raise ValueError(f"{name}: installed RECORD hash differs from evidence")
        if _sha256(wheel_bytes) != wheel_metadata_sha256:
            raise ValueError(f"{name}: installed WHEEL hash differs from evidence")
        wheel_text = wheel_bytes.decode("utf-8")
        tag = next(
            (
                line.removeprefix("Tag: ")
                for line in wheel_text.splitlines()
                if line.startswith("Tag: ")
            ),
            "",
        )
        filename = spec["source_wheel"]["filename"]
        if not tag or not filename.endswith("-" + tag + ".whl"):
            raise ValueError(
                f"{name}: PyPI wheel filename does not match installed WHEEL"
            )
        record = _record_hashes(record_bytes)
        if installed_evidence is not None:
            launcher = installed_evidence["launcher"]
            launcher_hash = _sha256(_path(root, launcher["source_path"]).read_bytes())
            if (
                launcher_hash != launcher["sha256"]
                or _record_digest(record.get(launcher["record_path"], ""))
                != launcher_hash
            ):
                raise ValueError(f"{name}: RECORD does not map local launcher")
        artifacts = []
        expected_paths = set(spec["artifact_paths"])
        actual_paths = {
            ".venv/Lib/site-packages/" + path.relative_to(site_packages).as_posix()
            for pattern in spec["artifact_globs"]
            for path in site_packages.glob(pattern)
            if path.is_file()
        }
        if actual_paths != expected_paths:
            raise ValueError(
                f"{name}: unmapped native file set: "
                f"expected {sorted(expected_paths)!r}, "
                f"got {sorted(actual_paths)!r}"
            )
        for source_path in spec["artifact_paths"]:
            expected_hash = expected_hashes.get(source_path)
            if not expected_hash:
                raise ValueError(f"{name}: research hash missing for {source_path}")
            actual_hash = _sha256(_path(root, source_path).read_bytes())
            if actual_hash != expected_hash:
                raise ValueError(f"{name}: native artifact hash differs from research")
            record_path = _site_packages_path(source_path)
            digest = record.get(record_path)
            if not digest or _record_digest(digest) != actual_hash:
                raise ValueError(f"{name}: RECORD does not map {record_path}")
            artifacts.append(
                {
                    "source_path": source_path,
                    "record_path": record_path,
                    "sha256": actual_hash,
                }
            )
        sbom_files = []
        for source_path in spec["sbom_paths"]:
            expected_hash = expected_hashes.get(source_path)
            if (
                not expected_hash
                or _sha256(_path(root, source_path).read_bytes()) != expected_hash
            ):
                raise ValueError(f"{name}: SBOM hash differs from research")
            record_path = _site_packages_path(source_path)
            if _record_digest(record.get(record_path, "")) != expected_hash:
                raise ValueError(f"{name}: RECORD does not map SBOM {record_path}")
            sbom_files.append(
                {
                    "source_path": source_path,
                    "bundle_path": (
                        f"THIRD-PARTY-NOTICES/{name}/SBOM/{Path(source_path).name}"
                    ),
                    "sha256": expected_hash,
                }
            )
        evidence_files = []
        for source_path in spec["extra_evidence_paths"]:
            expected_hash = expected_hashes.get(source_path)
            if (
                not expected_hash
                or _sha256(_path(root, source_path).read_bytes()) != expected_hash
            ):
                raise ValueError(f"{name}: native evidence hash differs from research")
            record_path = _site_packages_path(source_path)
            if _record_digest(record.get(record_path, "")) != expected_hash:
                raise ValueError(f"{name}: RECORD does not map evidence {record_path}")
            evidence_files.append(
                {
                    "source_path": source_path,
                    "bundle_path": (
                        f"THIRD-PARTY-NOTICES/{name}/{Path(source_path).name}"
                    ),
                    "sha256": expected_hash,
                }
            )
        result.append(
            {
                "id": spec["id"],
                "scope": "shipped_distribution",
                "distribution": name,
                "version": candidate["version"],
                "evidence_expression": candidate["evidence_expression"],
                "source_wheel": spec["source_wheel"],
                "record": {
                    "source_path": record_source,
                    "bundle_path": f"THIRD-PARTY-NOTICES/{name}/RECORD",
                    "sha256": record_sha256,
                },
                "wheel_metadata": {
                    "source_path": wheel_source,
                    "bundle_path": f"THIRD-PARTY-NOTICES/{name}/WHEEL",
                    "sha256": wheel_metadata_sha256,
                    "tag": tag,
                },
                "artifacts": artifacts,
                "sbom_files": sbom_files,
                "evidence_files": evidence_files,
                "notice_paths": [item["bundle_path"] for item in candidate["files"]],
                "component_status": spec["component_status"],
                "limitation": spec["limitation"],
            }
        )
    return result


def _owned_source_mapping(root, research):
    # Record local GPL source scope separately from the runtime helper executable.
    helper = research["wechat_helper"]
    local = helper["local_source"]
    history = helper["local_git_history"]
    root_license = dict(local["root_license"])
    root_license["sha256"] = _sha256(_path(root, "LICENSE").read_bytes())
    source_paths = [
        "src/wechat_keyfinder/CMakeLists.txt",
        "src/wechat_keyfinder/wechat_keyfinder.cpp",
    ]
    return {
        "id": "wechat-keyfinder-local-source",
        "scope": "local-source-only",
        "license_expression": "GPL-3.0-only",
        "root_license": root_license,
        "source_paths": source_paths,
        "source_hashes": {
            name: matrix_hash(name, _path(root, name).read_bytes())
            for name in source_paths
        },
        "local_tag": history["local_tag"],
        "local_tag_commit": history["local_tag_commit"],
        "binary_binding": "not-reproducible",
        "limitation": (
            "The root GPL text and local tag map only the repository-local C++ "
            "source. They do not attest that the separately fetched helper "
            "executable was built from this source or from any particular OpenSSL."
        ),
    }


def _external_components(root, research, helper_research):
    # Separate disabled runtimes and helper build inputs from shipped wheels.
    unresolved = research["unresolved"]
    helper = research["wechat_helper"]
    return [
        {
            "id": "gmssl-distribution",
            "scope": "external_runtime_not_shipped",
            "owner": "source.douyin",
            "excluded_from_targets": ["pyinstaller", "nuitka"],
            "source_path": ".venv/Lib/site-packages/gmssl-3.2.2.dist-info/METADATA",
            "sha256": helper_research["local_hashes"]["distribution_metadata"]["gmssl"],
            "notice_status": "NOASSERTION",
            "source_archive": GMSSL_SOURCE_ARCHIVE,
            "reason": (
                unresolved["gmssl"]["reason"]
                + " The exact PyPI source archive was additionally verified and "
                "contained no complete license text."
            ),
        },
        {
            "id": "wechat-helper-openssl",
            "scope": "helper_build_input_not_reproducible",
            "owner": "source.wechat",
            "excluded_from_targets": ["pyinstaller", "nuitka"],
            "source_path": "src/wechat_keyfinder/CMakeLists.txt",
            "sha256": matrix_hash(
                "src/wechat_keyfinder/CMakeLists.txt",
                _path(root, "src/wechat_keyfinder/CMakeLists.txt").read_bytes(),
            ),
            "notice_status": "NOASSERTION",
            "reason": unresolved["openssl_for_wechat_helper"]["reason"],
        },
        {
            "id": "wechat-keyfinder-downloaded-binary",
            "scope": "external_runtime_not_shipped",
            "owner": "source.wechat",
            "excluded_from_targets": ["pyinstaller", "nuitka"],
            "url": helper["binary"]["url"],
            "sha256": helper["binary"]["sha256_declared_in_source"],
            "source_binding": helper["source_binary_binding"],
            "notice_status": "unverified_external_binary",
            "reason": unresolved["wechat_helper_binary_binding"]["reason"],
        },
    ]


def expected_index(root=ROOT, local_evidence_path=FINAL_ENV_EVIDENCE):
    # Build the canonical index only from the recorded local research evidence.
    research, helper_research = _research(root)
    curl_spec = next(
        spec for spec in NATIVE_WHEEL_SPECS if spec["distribution"] == "curl-cffi"
    )
    final_evidence = _final_environment_evidence(root, local_evidence_path, curl_spec)
    candidates = helper_research["local_hashes"]["notice_copy_candidates"]
    approved = research["approved_candidates"]
    final_rows, candidates = _final_distribution_overrides(
        root, final_evidence, research, candidates
    )
    additional_rows, additional_candidates = _final_additional_distributions(
        root, final_evidence
    )
    final_rows.update(additional_rows)
    candidates.extend(additional_candidates)
    unresolved = research["unresolved"]
    rows = []
    for name in ORDER:
        if name in approved or name in final_rows:
            rows.append(
                _entry(
                    name,
                    final_rows[name] if name in final_rows else approved[name],
                    candidates,
                    True,
                )
            )
        elif name == "gmssl":
            rows.append(_entry(name, unresolved[name], candidates, False))
        else:
            raise ValueError(f"research package missing: {name}")
    payload = {
        "schema_version": 2,
        "bundle_version": BUNDLE_VERSION,
        "research": [
            SOURCE_RESEARCH.as_posix(),
            HELPER_RESEARCH.as_posix(),
            Path(local_evidence_path).as_posix(),
        ],
        "hash_policy": matrix_hash_policy(),
        "local_evidence": {
            "path": Path(local_evidence_path).as_posix(),
            "sha256": _sha256(
                _path(root, Path(local_evidence_path).as_posix()).read_bytes()
            ),
        },
        "distributions": rows,
        "native_distributions": _native_distributions(
            root, research, helper_research, rows, final_evidence
        ),
        "external_components": _external_components(root, research, helper_research),
        "owned_source_mapping": _owned_source_mapping(root, research),
    }
    payload["content_sha256"] = _sha256(
        json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )
    return payload


def _generated_text(index):
    # Render release-facing facts without converting legacy metadata into PEP 639.
    mapping = index["owned_source_mapping"]
    lines = [
        "# Third-Party Notices",
        "",
        "This versioned directory is a byte-exact Todo14 evidence bundle from the",
        "recorded local distribution evidence. Legal texts, notices, metadata,",
        "RECORD files, WHEEL files, and SBOMs are copied without editing.",
        "It does not contain a downloaded helper executable, a PYD, a DLL, or a",
        "static library. See ../NOTICE for the release scope and limitations.",
        "",
        f"Bundle version: {index['bundle_version']}",
        f"Content SHA-256: {index['content_sha256']}",
        "",
    ]
    notice = [
        "OhMyMeme NOTICE",
        "===============",
        "",
        "This is an engineering evidence index, not legal advice or a license grant.",
        f"Notice bundle version: {index['bundle_version']}",
        f"Notice bundle content SHA-256: {index['content_sha256']}",
        "",
        "Python distribution evidence and frozen scope",
        "-------------------------------------------",
    ]
    for row in index["distributions"]:
        notice.extend(
            [
                "",
                (
                    f"{row['name']} {row['version']} "
                    f"[{row['approval']}; evidence_expression: "
                    f"{row['evidence_expression']}]"
                ),
                f"  source: {row['source_url']}",
                (
                    f"  metadata: {row['metadata']['bundle_path']} "
                    f"sha256={row['metadata']['sha256']} "
                    f"({row['metadata']['license_field']}: "
                    f"{row['metadata']['license']})"
                ),
                f"  expression basis: {row['evidence_expression_basis']}",
            ]
        )
        for item in row["files"]:
            notice.append(f"  notice: {item['bundle_path']} sha256={item['sha256']}")
        if not row["files"]:
            notice.append("  notice: none; distribution is not frozen-shipped")
        source_archive = row.get("source_archive")
        if source_archive:
            notice.extend(
                [
                    (
                        f"  inspected PyPI archive: {source_archive['filename']} "
                        f"sha256={source_archive['sha256']}"
                    ),
                    f"  archive URL: {source_archive['url']}",
                    f"  archive result: {source_archive['inspection']}",
                ]
            )
    notice.extend(["", "Shipped native wheel evidence", "----------------------------"])
    for row in index["native_distributions"]:
        notice.extend(
            [
                "",
                f"{row['id']} [{row['scope']}; {row['component_status']}]",
                (
                    f"  distribution: {row['distribution']} {row['version']}; "
                    f"evidence_expression: {row['evidence_expression']}"
                ),
                (
                    f"  PyPI wheel: {row['source_wheel']['filename']} "
                    f"sha256={row['source_wheel']['sha256']}"
                ),
                f"  PyPI JSON: {row['source_wheel']['pypi_json_url']}",
                (
                    f"  RECORD: {row['record']['bundle_path']} "
                    f"sha256={row['record']['sha256']}"
                ),
                (
                    f"  WHEEL: {row['wheel_metadata']['bundle_path']} "
                    f"sha256={row['wheel_metadata']['sha256']}; "
                    f"tag={row['wheel_metadata']['tag']}"
                ),
            ]
        )
        for artifact in row["artifacts"]:
            notice.append(
                f"  native artifact: {artifact['record_path']} "
                f"sha256={artifact['sha256']}"
            )
        for artifact in row["sbom_files"] + row["evidence_files"]:
            notice.append(
                f"  wheel evidence: {artifact['bundle_path']} "
                f"sha256={artifact['sha256']}"
            )
        for path in row["notice_paths"]:
            notice.append(f"  distribution notice: {path}")
        notice.append(f"  limitation: {row['limitation']}")
    notice.extend(
        [
            "",
            "Frozen release scope",
            "--------------------",
            "PyInstaller and Nuitka include LICENSE, NOTICE, LICENSES/, and",
            "THIRD-PARTY-NOTICES/ in the ohmymeme application data directory.",
            "The record-bound curl-cffi wheel remains in scope. gmssl is excluded",
            "from both frozen targets because no complete license text was found;",
            "source.douyin therefore keeps its provider_unavailable sentinel.",
            "",
            "Non-shipped external runtime and helper boundaries",
            "--------------------------------------------------",
        ]
    )
    for component in index["external_components"]:
        targets = ", ".join(component["excluded_from_targets"])
        notice.extend(
            [
                f"{component['id']} [{component['scope']}]",
                f"  owner: {component['owner']}; excluded from: {targets}",
                f"  evidence: {component.get('sha256', 'no conveyed artifact')}",
                f"  reason: {component['reason']}",
            ]
        )
    notice.extend(
        [
            "",
            "Local helper source mapping",
            "---------------------------",
            (
                f"{mapping['id']} [{mapping['scope']}; "
                f"{mapping['license_expression']}]"
            ),
            (
                f"  root LICENSE sha256={mapping['root_license']['sha256']}; "
                f"local tag {mapping['local_tag']} "
                f"({mapping['local_tag_commit']})"
            ),
        ]
    )
    for path, digest in mapping["source_hashes"].items():
        notice.append(f"  source: {path} sha256={digest}")
    notice.extend([f"  limitation: {mapping['limitation']}", ""])
    licenses_readme = "\n".join(
        [
            "# Owned Source License Inventory",
            "",
            "GPL-3.0-only.txt is a byte-exact copy of the repository root LICENSE.",
            "wechat-keyfinder-local-source.json maps that GPL declaration to the",
            "repository-local CMakeLists.txt and C++ source at local tag 0.6.3.",
            "It expressly does not bind the separately fetched helper executable.",
            "QQNT and ABogus retain their upstream attribution in source headers and",
            "are not relabeled as project-owned source by this inventory.",
            "",
        ]
    )
    mapping_text = (
        json.dumps(mapping, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )
    return {
        "THIRD-PARTY-NOTICES/README.md": "\n".join(lines),
        "NOTICE": "\n".join(notice),
        "LICENSES/README.md": licenses_readme,
        "LICENSES/wechat-keyfinder-local-source.json": mapping_text,
    }


def _expected_files(root, index):
    # Return all generated or copied bytes that must be present for a valid bundle.
    files = {}
    for row in index["distributions"]:
        metadata = row["metadata"]
        source = _path(root, metadata["source_path"])
        files[metadata["bundle_path"]] = source.read_bytes()
        for item in row["files"]:
            source = _path(root, item["source_path"])
            files[item["bundle_path"]] = source.read_bytes()
    for row in index["native_distributions"]:
        for item in (
            row["record"],
            row["wheel_metadata"],
            *row["sbom_files"],
            *row["evidence_files"],
        ):
            source = _path(root, item["source_path"])
            files[item["bundle_path"]] = source.read_bytes()
    files[INDEX.as_posix()] = (
        json.dumps(index, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    files.update(
        {name: value.encode("utf-8") for name, value in _generated_text(index).items()}
    )
    root_license = _path(root, "LICENSE").read_bytes()
    files["LICENSES/GPL-3.0-only.txt"] = root_license
    return files


def _validate_index(index, expected):
    # The index must exactly match current research instead of being self-approving.
    errors = []
    if not isinstance(index, dict):
        return ["notice_bundle.index: expected object"]
    for field in (
        "schema_version",
        "bundle_version",
        "content_sha256",
        "research",
        "hash_policy",
        "local_evidence",
        "distributions",
        "native_distributions",
        "external_components",
        "owned_source_mapping",
    ):
        if field not in index:
            errors.append(f"notice_bundle.index.{field}: missing required field")
    if index.get("schema_version") != 2:
        errors.append("notice_bundle.index.schema_version: expected 2")
    if index.get("bundle_version") != BUNDLE_VERSION:
        errors.append("notice_bundle.index.bundle_version: unexpected version")
    expected_content = expected["content_sha256"]
    if index.get("content_sha256") != expected_content:
        errors.append(
            "notice_bundle.index.content_sha256: research-derived digest mismatch"
        )
    for field in (
        "research",
        "hash_policy",
        "local_evidence",
        "distributions",
        "native_distributions",
        "external_components",
        "owned_source_mapping",
    ):
        if index.get(field) != expected.get(field):
            errors.append(
                f"notice_bundle.index.{field}: research-derived record mismatch"
            )
    return errors


def check_bundle(
    root=ROOT,
    index_path=INDEX,
    local_evidence_path=FINAL_ENV_EVIDENCE,
    files=None,
):
    # Verify source, bundle, index, and owned records without running a provider.
    errors = []
    expected_index_value = expected_index(root, local_evidence_path)

    def read_output(name):
        # Validate planned output bytes before they are published to the worktree.
        if files is not None:
            if name not in files:
                raise OSError(f"planned bundle file missing: {name}")
            return files[name]
        return _path(root, name).read_bytes()

    index_name = Path(index_path).as_posix()
    try:
        index = _json(read_output(index_name))
    except (OSError, ValueError) as error:
        return [f"notice_bundle.index: {error}"]
    errors.extend(_validate_index(index, expected_index_value))
    expected_files = _expected_files(root, expected_index_value)
    for name, expected_bytes in sorted(expected_files.items()):
        try:
            actual = read_output(name)
        except (OSError, ValueError) as error:
            errors.append(f"notice_bundle.file[{name}]: missing: {error}")
            continue
        if actual != expected_bytes:
            errors.append(f"notice_bundle.file[{name}]: byte/hash mismatch")
    if files is not None:
        actual_bundle = {
            name for name in files if name.startswith(BUNDLE.as_posix() + "/")
        }
    else:
        bundle_root = root / BUNDLE
        actual_bundle = (
            {
                path.relative_to(root).as_posix()
                for path in bundle_root.rglob("*")
                if path.is_file()
            }
            if bundle_root.is_dir()
            else None
        )
    if actual_bundle is not None:
        expected_bundle = {
            name for name in expected_files if name.startswith(BUNDLE.as_posix() + "/")
        }
        for name in sorted(actual_bundle - expected_bundle):
            errors.append(f"notice_bundle.file[{name}]: unexpected bundle file")
    else:
        errors.append("notice_bundle: THIRD-PARTY-NOTICES directory missing")
    for row in expected_index_value["distributions"]:
        metadata = row["metadata"]
        source = _path(root, metadata["source_path"])
        if _sha256(source.read_bytes()) != metadata["sha256"]:
            errors.append(f"notice_bundle.metadata[{row['name']}]: source hash drift")
        for item in row["files"]:
            source = _path(root, item["source_path"])
            if _sha256(source.read_bytes()) != item["sha256"]:
                errors.append(
                    f"notice_bundle.file[{item['bundle_path']}]: source hash drift"
                )
    return errors


def _materialize_matrix(root, matrix_path, local_evidence_path, index, files):
    # Derive matrix facts from the same uncommitted bundle snapshot.
    try:
        from scripts import plugin_license_matrix
    except ModuleNotFoundError:
        import plugin_license_matrix

    return plugin_license_matrix.materialize_matrix(
        root=root,
        matrix_path=matrix_path,
        local_evidence_path=local_evidence_path,
        notice_index=index,
        bundle_files=files,
    )


def _stage_files(root, files):
    # Fsync every replacement before any visible output is published.
    staged = []
    try:
        for name, data in sorted(files.items()):
            destination = _path(root, name)
            if destination.exists():
                if not destination.is_file():
                    raise ValueError(f"generated output is not a file: {name}")
                if destination.read_bytes() == data:
                    continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
            staged.append((temporary, destination))
    except BaseException:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
        raise
    return staged


def _replace_staged(staged, final_destination=None):
    # Publish the matrix last so it never leads a partially published notice bundle.
    if final_destination is not None:
        staged = [item for item in staged if item[1] != final_destination] + [
            item for item in staged if item[1] == final_destination
        ]
    try:
        for temporary, destination in staged:
            os.replace(temporary, destination)
    except BaseException:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
        raise


def write_bundle(root=ROOT, local_evidence_path=FINAL_ENV_EVIDENCE, matrix_path=MATRIX):
    # Materialize all bundle and matrix facts from one current source/staging snapshot.
    index = expected_index(root, local_evidence_path)
    files = _expected_files(root, index)
    matrix_name = Path(matrix_path).as_posix()
    if matrix_name in files:
        raise ValueError(f"matrix path overlaps generated bundle output: {matrix_name}")
    matrix = _materialize_matrix(root, matrix_name, local_evidence_path, index, files)
    files[matrix_name] = (
        json.dumps(matrix, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    _replace_staged(_stage_files(root, files), _path(root, matrix_name))
    return index


def _write_report(path, report):
    # Replace reports atomically so a previous PASS cannot survive a failed check.
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
    # Run the generator or verifier with a report suitable for Todo14 evidence.
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    parser.add_argument("--index", type=Path, default=INDEX)
    parser.add_argument("--matrix", type=Path, default=MATRIX)
    parser.add_argument("--local-evidence", type=Path, default=FINAL_ENV_EVIDENCE)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    report = {"schema_version": 1, "status": "REJECTED", "errors": []}
    try:
        if args.write:
            index = write_bundle(
                local_evidence_path=args.local_evidence, matrix_path=args.matrix
            )
        else:
            index = expected_index(local_evidence_path=args.local_evidence)
        errors = check_bundle(
            index_path=args.index, local_evidence_path=args.local_evidence
        )
        report.update(
            {
                "bundle_version": index["bundle_version"],
                "content_sha256": index["content_sha256"],
                "index_path": args.index.as_posix(),
                "matrix_path": args.matrix.as_posix(),
                "matrix_sha256": (
                    matrix_hash(
                        args.matrix.as_posix(),
                        _path(ROOT, args.matrix.as_posix()).read_bytes(),
                    )
                    if _path(ROOT, args.matrix.as_posix()).is_file()
                    else ""
                ),
                "hash_policy": index["hash_policy"],
                "local_evidence_path": args.local_evidence.as_posix(),
                "index_sha256": (
                    _sha256(_path(ROOT, args.index.as_posix()).read_bytes())
                    if _path(ROOT, args.index.as_posix()).is_file()
                    else ""
                ),
                "distributions": index["distributions"],
                "external_components": index["external_components"],
                "errors": errors,
            }
        )
        if not errors:
            report["status"] = "PASS"
    except (KeyError, OSError, TypeError, ValueError) as error:
        report["errors"] = [str(error) or type(error).__name__]
    _write_report(args.report, report)
    for error in report["errors"]:
        print(error, file=sys.stderr)
    print(f"{report['status']}: notice bundle")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
