"""Execute Todo19 bridge adversarial probes in a fresh Python process."""

import hashlib
import json
import os
import shutil
import socket
import tempfile
from pathlib import Path
from uuid import uuid4

from ohmymeme.core.schemas.bridge import (
    BRIDGE_CONTRACT,
    BridgeContractError,
    BridgeInitData,
    SettingsPatch,
)
from ohmymeme.presentation.desktop.api.main_facade import MainBridgeFacade

ROOT = Path(__file__).resolve().parents[1]
BOUND_HASH_PATHS = (
    "src/ohmymeme/core/schemas/bridge.py",
    "src/ohmymeme/presentation/desktop/api/facade_base.py",
    "src/ohmymeme/presentation/desktop/api/main_facade.py",
    "src/ohmymeme/presentation/desktop/api/settings_facade.py",
    "src/ohmymeme/presentation/frontend/settings/shared/decoder.js",
    "schemas/bridge/bridge.schema.json",
    "src/ohmymeme/presentation/frontend/main/shared/generated/bridge.ts",
    "src/ohmymeme/presentation/frontend/settings/shared/generated/bridge.ts",
    "tests/contracts/test_pywebview_bridge.py",
    "tests/frontend/bridge_decoder.test.js",
)


def _row(name: str, probe) -> dict:
    row = {
        "execution_identity": {"run_id": RUN_ID, "row_id": f"{RUN_ID}:{name}"},
        "executed": True,
        "assertions": [],
        "status": "pass",
    }
    try:
        row["assertions"] = probe()
    except (
        AssertionError,
        BridgeContractError,
        TypeError,
        ValueError,
        RuntimeError,
    ) as error:
        row["status"] = "fail"
        row["error"] = str(error)
    return row


def _save_settings_valid() -> list[str]:
    checked = BRIDGE_CONTRACT.validate_input(
        "save_settings", ({"hotkey": "Ctrl+Shift+X"},)
    )
    assert isinstance(checked[0], SettingsPatch)
    return ["SettingsPatch"]


def _save_settings_unknown() -> list[str]:
    try:
        BRIDGE_CONTRACT.validate_input("save_settings", ({"unknown": True},))
    except BridgeContractError as error:
        assert error.phase == "input"
        return ["unknown-field-rejected"]
    raise AssertionError("unknown settings field was accepted")


def _malformed_search() -> list[str]:
    try:
        BRIDGE_CONTRACT.validate_output("search_memes", [{"unexpected": True}])
    except BridgeContractError as error:
        assert error.phase == "output"
        return ["search-shape-rejected"]
    raise AssertionError("malformed search result was accepted")


def _malformed_init() -> list[str]:
    try:
        BRIDGE_CONTRACT.validate_output("get_init_data", {"memes": []})
    except BridgeContractError as error:
        assert error.phase == "output"
        return ["init-shape-rejected"]
    raise AssertionError("malformed init result was accepted")


def _boolean_and_void() -> list[str]:
    assert BRIDGE_CONTRACT.validate_output("start_window_drag", True) is True
    assert BRIDGE_CONTRACT.validate_output("refresh_memes", None) is None
    return ["boolean-preserved", "void-preserved"]


def _runtime_error_mapping() -> list[str]:
    class Inner:
        def sync_push(self):
            raise RuntimeError("transport down")

    facade = MainBridgeFacade.__new__(MainBridgeFacade)
    facade._legacy = Inner()
    assert facade.sync_push() == {
        "ok": False,
        "error": "sync failed",
        "failed_files": [],
    }
    assert facade._last_bridge_error.code == "internal_runtime_error"
    return ["runtime-error-structured", "legacy-envelope-preserved"]


def _schema_root() -> list[str]:
    schema = json.loads(
        (ROOT / "schemas/bridge/bridge.schema.json").read_text(encoding="utf-8")
    )
    assert "oneOf" not in schema
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert BridgeInitData.__name__ in schema["$defs"]
    return ["non-overlapping-root", "draft-2020-12"]


def _hashes() -> dict[str, str]:
    return {
        path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        for path in BOUND_HASH_PATHS
    }


def _cleanup_probe() -> dict:
    temporary_root = Path(tempfile.mkdtemp(prefix="ohmymeme-task19-"))
    marker = temporary_root / "marker"
    marker.write_bytes(b"task19")
    server = None
    created = False
    try:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        created = temporary_root.is_dir() and marker.is_file() and server.fileno() >= 0
    finally:
        if server is not None:
            server.close()
        if marker.exists():
            marker.unlink()
        if temporary_root.exists():
            shutil.rmtree(temporary_root)
        cleanup = {
            "temporary_root_created": created,
            "temporary_root_removed": not temporary_root.exists(),
            "marker_removed": not marker.exists(),
            "socket_closed": server is not None and server.fileno() == -1,
        }
        _CLEANUP.update(cleanup)
    return cleanup


RUN_ID = uuid4().hex
_CLEANUP = {}
SCENARIOS = {
    "save-settings-valid": _save_settings_valid,
    "save-settings-unknown": _save_settings_unknown,
    "malformed-search": _malformed_search,
    "malformed-init": _malformed_init,
    "boolean-and-void": _boolean_and_void,
    "runtime-error-mapping": _runtime_error_mapping,
    "schema-root": _schema_root,
}


def main() -> int:
    """Run every bridge matrix probe and emit canonical JSON."""
    challenge = os.environ.get("OHMYMEME_TASK19_CHALLENGE", "")
    if not challenge:
        return 2
    hashes_before = _hashes()
    _cleanup_probe()
    scenarios = {name: _row(name, probe) for name, probe in SCENARIOS.items()}
    hashes_after = _hashes()
    payload = {
        "challenge": challenge,
        "execution_identity": RUN_ID,
        "scenarios": scenarios,
        "hashes_before": hashes_before,
        "hashes_after": hashes_after,
        "cleanup": _CLEANUP,
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "verdict": (
            "pass"
            if hashes_before == hashes_after
            and all(row["status"] == "pass" for row in scenarios.values())
            and all(_CLEANUP.values())
            else "blocked"
        ),
    }
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    return 0 if payload["verdict"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
