import json
import socket
import subprocess
import tempfile
from argparse import ArgumentParser
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from ohmymeme.app.container import Container
from ohmymeme.core.domain import TaskKind
from ohmymeme.core.plugins.registry import PluginRegistry
from ohmymeme.presentation.desktop.api.plugin_dispatch import _descriptor


def probe(root, cases):
    # Real facade, registry, installed factory, host coordinator and sink.
    results = {}
    container = Container(root / "host")
    try:
        webui = container.create_webui()
        from ohmymeme.presentation.desktop.window_manager import SettingsApi

        api = SettingsApi(webui, container.settings)
        source = root / "userdata/10001/nt_qq/nt_data/Emoji/personal_emoji/Ori"
        source.mkdir(parents=True)
        Image.new("RGB", (2, 2), "red").save(source / "meme.png")
        (source / "corrupt.png").write_bytes(b"bad image")
        (source / "readme.txt").write_bytes(b"external non-image")
        container.config.set("qqnt_userdata_path", str(root / "userdata"))
        for case in cases:
            if case in ("provider_absent", "provider_disabled"):
                isolated = container.create_webui()
                if case == "provider_absent":
                    isolated._plugin_action_registry = PluginRegistry(
                        (_descriptor("source.qqnt"),), {}, ()
                    )
                else:
                    isolated._enabled_import_plugins = ()
                unavailable = SettingsApi(isolated, container.settings)
                actual = unavailable.qqnt_start("10001", str(root / "never"))
                assert actual == {"ok": False} and not (root / "never").exists()
                results[case] = actual
            elif case == "unsafe_staging":
                actual = api.qqnt_start("10001", str(source.parent), False, True)
                assert actual == {"ok": False}
                assert (source / "readme.txt").read_bytes() == b"external non-image"
                results[case] = actual
            else:
                output = (
                    root / "export"
                    if case == "external_export"
                    else container.config.cache_dir
                )
                assert api.qqnt_start("10001", str(output)) == {"ok": True}
                container.operations.wait(TaskKind.IMPORT_QQNT, 5)
                actual = api.qqnt_get_progress()
                assert actual["status"] == "done", actual
                assert actual["result"]["output_dir"] == str(output)
                if case == "external_export":
                    assert actual["result"]["copied"] == 3
                    assert (output / "readme.txt").read_bytes() == b"external non-image"
                    assert container.db.search() == []
                else:
                    assert actual["result"]["copied"] == 1
                    assert actual["result"]["skipped"] == 2
                    assert len(container.db.search()) == 1
                results[case] = {
                    "status": actual["status"],
                    "copied": actual["result"]["copied"],
                    "skipped": actual["result"]["skipped"],
                }
        worker = webui._import_workers["source.qqnt"]
        assert worker.operation._closed
        assert not list(
            (container.config.data_dir / "plugin-workspaces").rglob("operation-*")
        )
        results["cleanup"] = "operation closed; staging removed; coordinator joined"
    finally:
        container.close()
    return results


def main():
    # Fail closed on malformed fixtures; reports are observations, not expected data.
    parser = ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = {"status": "REJECTED", "observations": {}, "errors": []}
    try:
        fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
        expected = [
            "provider_absent",
            "provider_disabled",
            "unsafe_staging",
            "external_export",
            "sink_admission",
        ]
        if (
            fixture != {"schema_version": 1, "cases": expected}
            or type(fixture["schema_version"]) is not int
        ):
            raise ValueError("fixture: exact schema_version/cases required")
        with tempfile.TemporaryDirectory(prefix="ohmm-qqnt-qa-") as directory:
            with patch.object(
                socket, "getaddrinfo", side_effect=AssertionError("offline DNS")
            ), patch.object(
                socket,
                "create_connection",
                side_effect=AssertionError("offline socket"),
            ), patch.object(
                subprocess, "Popen", side_effect=AssertionError("offline process")
            ):
                report["observations"] = probe(Path(directory), fixture["cases"])
        report["status"] = "PASS"
    except Exception as error:
        report["errors"].append(f"{type(error).__name__}: {error}")
    report["command_exit"] = 0 if report["status"] == "PASS" else 1
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=True))
    return report["command_exit"]


if __name__ == "__main__":
    raise SystemExit(main())
