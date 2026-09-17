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
BUNDLE = Path("THIRD-PARTY-NOTICES")
INDEX = BUNDLE / "license-evidence.json"
BUNDLE_VERSION = "todo14-wave5-2026-09-18-r3"
APPROVED_SPDX = {
    "boto3": "Apache-2.0",
    "botocore": "Apache-2.0",
    "bottle": "MIT",
    "cryptography": "Apache-2.0 OR BSD-3-Clause",
    "curl-cffi": "MIT",
    "keyboard": "MIT",
    "pillow": "MIT-CMU",
    "pydantic": "MIT",
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
        "record_sha256": (
            "a8ae7f4581aee9df62397bc6a6793086a26e74506a684572d91bcfb6ac945886"
        ),
        "wheel_metadata_sha256": (
            "1517363ea090e5011be1d9d80dafaf4c645b95893f0bbc1eceffba05d3bcfb89"
        ),
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


def _native_distributions(root, research, helper_research, rows):
    # Bind native payloads to one exact wheel, RECORD, notices, and optional SBOMs.
    candidate_rows = {row["name"]: row for row in rows}
    native_hashes = helper_research["local_hashes"]["native_and_sbom"]
    site_packages = root / ".venv" / "Lib" / "site-packages"
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
        record_bytes = _path(root, record_source).read_bytes()
        wheel_bytes = _path(root, wheel_source).read_bytes()
        if _sha256(record_bytes) != spec["record_sha256"]:
            raise ValueError(f"{name}: installed RECORD hash differs from evidence")
        if _sha256(wheel_bytes) != spec["wheel_metadata_sha256"]:
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
            expected_hash = native_hashes.get(source_path)
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
            expected_hash = native_hashes.get(source_path)
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
            expected_hash = native_hashes.get(source_path)
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
                    "sha256": spec["record_sha256"],
                },
                "wheel_metadata": {
                    "source_path": wheel_source,
                    "bundle_path": f"THIRD-PARTY-NOTICES/{name}/WHEEL",
                    "sha256": spec["wheel_metadata_sha256"],
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


def _owned_source_mapping(research):
    # Record local GPL source scope separately from the runtime helper executable.
    helper = research["wechat_helper"]
    local = helper["local_source"]
    history = helper["local_git_history"]
    return {
        "id": "wechat-keyfinder-local-source",
        "scope": "local-source-only",
        "license_expression": "GPL-3.0-only",
        "root_license": local["root_license"],
        "source_paths": [
            "src/wechat_keyfinder/CMakeLists.txt",
            "src/wechat_keyfinder/wechat_keyfinder.cpp",
        ],
        "source_hashes": {
            "src/wechat_keyfinder/CMakeLists.txt": local["cmake"]["sha256"],
            "src/wechat_keyfinder/wechat_keyfinder.cpp": local["cpp"]["sha256"],
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


def _external_components(research, helper_research):
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
            "sha256": helper_research["local_hashes"]["helper_source"][
                "src/wechat_keyfinder/CMakeLists.txt"
            ],
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


def expected_index(root=ROOT):
    # Build the canonical index only from the recorded local research evidence.
    research, helper_research = _research(root)
    candidates = helper_research["local_hashes"]["notice_copy_candidates"]
    approved = research["approved_candidates"]
    unresolved = research["unresolved"]
    rows = []
    for name in ORDER:
        if name in approved:
            rows.append(_entry(name, approved[name], candidates, True))
        elif name == "gmssl":
            rows.append(_entry(name, unresolved[name], candidates, False))
        else:
            raise ValueError(f"research package missing: {name}")
    payload = {
        "schema_version": 2,
        "bundle_version": BUNDLE_VERSION,
        "research": [SOURCE_RESEARCH.as_posix(), HELPER_RESEARCH.as_posix()],
        "distributions": rows,
        "native_distributions": _native_distributions(
            root, research, helper_research, rows
        ),
        "external_components": _external_components(research, helper_research),
        "owned_source_mapping": _owned_source_mapping(research),
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


def check_bundle(root=ROOT, index_path=INDEX):
    # Verify source, bundle, index, and owned records without running a provider.
    errors = []
    expected_index_value = expected_index(root)
    index_file = _path(root, Path(index_path).as_posix())
    try:
        index = _json(index_file.read_bytes())
    except (OSError, ValueError) as error:
        return [f"notice_bundle.index: {error}"]
    errors.extend(_validate_index(index, expected_index_value))
    expected_files = _expected_files(root, expected_index_value)
    for name, expected_bytes in sorted(expected_files.items()):
        try:
            actual = _path(root, name).read_bytes()
        except OSError as error:
            errors.append(f"notice_bundle.file[{name}]: missing: {error}")
            continue
        if actual != expected_bytes:
            errors.append(f"notice_bundle.file[{name}]: byte/hash mismatch")
    bundle_root = root / BUNDLE
    if bundle_root.is_dir():
        expected_bundle = {
            name for name in expected_files if name.startswith(BUNDLE.as_posix() + "/")
        }
        actual_bundle = {
            path.relative_to(root).as_posix()
            for path in bundle_root.rglob("*")
            if path.is_file()
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


def write_bundle(root=ROOT):
    # Copy only whitelisted text evidence and create deterministic owned-source records.
    index = expected_index(root)
    files = _expected_files(root, index)
    for name, data in files.items():
        destination = _path(root, name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
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
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    report = {"schema_version": 1, "status": "REJECTED", "errors": []}
    try:
        if args.write:
            index = write_bundle()
        else:
            index = expected_index()
        errors = check_bundle(index_path=args.index)
        report.update(
            {
                "bundle_version": index["bundle_version"],
                "content_sha256": index["content_sha256"],
                "index_path": args.index.as_posix(),
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
