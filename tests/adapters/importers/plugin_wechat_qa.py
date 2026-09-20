import hashlib
import io
import json
import socket
import sqlite3
import subprocess
import tempfile
from argparse import ArgumentParser
from contextlib import ExitStack, closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from ohmymeme.core.adapters.fetch_policy import FetchPolicy

CASES = [
    "happy",
    "scan_only",
    "missing_account",
    "multiple_accounts",
    "missing_hash",
    "helper_failure",
    "unsafe_cdn",
    "unsafe_redirect",
    "md5_mismatch",
    "rejected_bytes",
    "cancel",
    "provider_absent",
    "provider_disabled",
    "provider_incompatible",
]


def image_bytes():
    # Use decodable bytes, not an accepted magic-prefix stub.
    stream = io.BytesIO()
    Image.new("RGBA", (2, 2), (20, 60, 100, 255)).save(stream, "PNG")
    return stream.getvalue()


def source_database(root, case):
    # The source is a real user SQLite fixture, never the application's database.
    path = root / "wxid_fixture/db_storage/emoticon/emoticon.db"
    path.parent.mkdir(parents=True)
    if case in ("missing_hash", "helper_failure"):
        path.write_bytes(bytes(4096))
        return path
    payload = image_bytes()
    if case == "rejected_bytes":
        payload = b"invalid bytes"
    url = (
        "https://evil.example/meme"
        if case == "unsafe_cdn"
        else "https://vweixinf.tc.qq.com/meme"
    )
    md5 = "0" * 32 if case == "md5_mismatch" else hashlib.md5(payload).hexdigest()
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "CREATE TABLE kNonStoreEmoticonTable "
            "(type, md5, aes_key, cdn_url, encrypt_url, extern_url, extern_md5)"
        )
        conn.execute(
            "INSERT INTO kNonStoreEmoticonTable VALUES (?,?,?,?,?,?,?)",
            (1, md5, "", url, "", "", ""),
        )
        conn.commit()
    if case == "multiple_accounts":
        other = root / "wxid_other/db_storage/emoticon/emoticon.db"
        other.parent.mkdir(parents=True)
        other.write_bytes(path.read_bytes())
    return path


class FixtureHTTP:
    def __init__(self, case):
        # Every allowed connection has an explicit local response.
        self.case = case
        self.calls = []
        self.closed = 0

    def resolve(self, hostname, port):
        # No DNS or ambient proxy is consulted.
        assert hostname == "vweixinf.tc.qq.com" and port == 443
        return ["93.184.216.34"]

    def open(self, target, address, timeout, headers):
        # Redirects run through the real FetchPolicy on every hop.
        from types import SimpleNamespace

        assert target.hostname == "vweixinf.tc.qq.com"
        assert address.ip == "93.184.216.34" and timeout > 0
        self.calls.append(target.hostname + target.request_target)
        body = io.BytesIO(
            b"invalid bytes" if self.case == "rejected_bytes" else image_bytes()
        )

        def close():
            self.closed += 1
            body.close()

        return SimpleNamespace(
            status=302 if self.case == "unsafe_redirect" else 200,
            headers=(
                {"Location": "https://127.0.0.1/private"}
                if self.case == "unsafe_redirect"
                else {}
            ),
            read=body.read,
            close=close,
        )


class FixtureProcess:
    def __init__(self, result, returncode=0):
        # Implements the exact process resource surface without launching anything.
        self.result = result
        self.exit_code = returncode
        self.returncode = None
        self.stdout, self.stderr = io.BytesIO(), io.BytesIO()
        self.waited = False
        self.killed = False
        self.terminated = False

    def poll(self):
        # Resource drain queries the actual fixture state.
        return self.returncode

    def communicate(self, timeout=None):
        # Serialize real helper JSON over the byte protocol.
        assert 0 < timeout <= 0.25
        self.returncode = self.exit_code
        return json.dumps(self.result).encode(), b""

    def terminate(self):
        # A normal fixture process responds to termination.
        self.terminated = True
        self.returncode = -15

    def kill(self):
        # Hung fixtures can reuse the kill fallback.
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        # Waiting is observed, not inferred from calling kill.
        self.waited = True
        return self.returncode


def durable_snapshot(container):
    # Compare public rows plus canonical persisted bytes, excluding operation staging.
    paths = [
        container.config.data_dir / name for name in ("config.json", "meme-index.json")
    ]
    paths += sorted(container.config.cache_dir.rglob("*"))
    return {
        "rows": container.db.search(),
        "files": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths
            if path.is_file()
        },
    }


def probe(root, cases):
    # Real facade -> registry -> create_plugin -> narrow context -> host ImportSink.
    import ohmymeme_plugin_wechat as implementation

    from ohmymeme.app.container import Container
    from ohmymeme.core.domain import TaskKind
    from ohmymeme.core.plugins.registry import PluginRegistry
    from ohmymeme.integrations.imports import wechat
    from ohmymeme.presentation.desktop.api.plugin_dispatch import _descriptor
    from ohmymeme.presentation.desktop.window_manager import SettingsApi

    observations = {}
    for case in cases:
        source = root / case / "source"
        db_path = source_database(source, case)
        source_before = db_path.read_bytes()
        container = Container(root / case / "host")
        processes, submissions, outputs, contexts = [], [], [], []
        try:
            webui = container.create_webui()
            api = SettingsApi(webui, container.settings)
            before = durable_snapshot(container)
            http = FixtureHTTP(case)
            with ExitStack() as stack:
                stack.enter_context(
                    patch.object(
                        implementation,
                        "_FETCH_POLICY",
                        FetchPolicy(resolver=http, connector=http),
                    )
                )
                if case.startswith("provider_"):
                    descriptor = _descriptor("source.wechat")
                    if case == "provider_incompatible":
                        descriptor = descriptor._replace(api_version=99)
                    webui._plugin_action_registry = PluginRegistry(
                        (descriptor,), {}, ()
                    )
                    if case == "provider_disabled":
                        webui._enabled_import_plugins = ()
                    assert api.start_wechat_import(str(source)) == {"ok": False}
                    assert api.inspect_wechat_environment(str(source)) == {}
                    assert api.list_wechat_stickers(str(source)) == {}
                    assert api.get_wechat_import_progress() == {}
                    assert api.cancel_wechat_import() is None
                    assert not getattr(webui, "_import_workers", {})
                    actual = {"ok": False}
                else:
                    helper = root / case / "host-helper"
                    helper.mkdir()
                    (helper / "wechat_keyfinder.exe").write_bytes(b"fixture helper")
                    offsets = helper / "offsets.json"
                    offsets.write_text('{"versions":{}}', encoding="utf-8")
                    stack.enter_context(
                        patch.object(wechat, "_get_wechat_dir", return_value=helper)
                    )
                    stack.enter_context(
                        patch.object(wechat, "_offsets_path", return_value=offsets)
                    )
                    stack.enter_context(
                        patch.dict(
                            wechat._WECHAT_KEYFINDER_SHA256,
                            {
                                "Windows": (
                                    ""
                                    if case == "missing_hash"
                                    else hashlib.sha256(b"fixture helper").hexdigest()
                                )
                            },
                        )
                    )
                    stack.enter_context(
                        patch.dict(
                            "os.environ", {"OHMYMEME_INSECURE_SKIP_HELPER_HASH": "0"}
                        )
                    )

                    def popen(cmd, **kwargs):
                        assert case == "helper_failure", "undeclared helper invocation"
                        assert cmd[1] == "--config" and cmd[3:] == [
                            "--db-path",
                            str(db_path),
                            "--no-snapshot",
                        ]
                        assert Path(cmd[0]).read_bytes() == b"fixture helper"
                        assert Path(cmd[2]).read_bytes() == offsets.read_bytes()
                        assert "operation-" in cmd[0] and str(helper) not in cmd[0]
                        proc = FixtureProcess(
                            {
                                "ok": False,
                                "reason": "key_not_found",
                                "detail": "fixture helper failure",
                            },
                            1,
                        )
                        processes.append(proc)
                        return proc

                    stack.enter_context(
                        patch.object(implementation.subprocess, "Popen", popen)
                    )
                    factory = implementation.create_plugin
                    sink = container.create_import_sink(webui._decode_stego)

                    def submit(requests, cancelled=None):
                        submissions.extend(requests)
                        assert all(
                            "operation-" in str(request.path) for request in requests
                        )
                        return sink.import_batch(requests, cancelled=cancelled)

                    stack.enter_context(
                        patch.object(
                            container,
                            "create_import_sink",
                            return_value=SimpleNamespace(import_batch=submit),
                        )
                    )

                    def create():
                        provider = factory()
                        original = provider.import_media

                        def run(context):
                            contexts.append(context)
                            assert set(context.request) == {
                                "user_root",
                                "download",
                                "account_path",
                            }
                            assert not hasattr(context, "config") and not hasattr(
                                context, "webui"
                            )
                            if case == "cancel":
                                provider.stop()
                            return original(context)

                        provider.import_media = run
                        return provider

                    stack.enter_context(
                        patch.object(implementation, "create_plugin", create)
                    )
                    # Capture actual policy emissions, including errors and logs.
                    from ohmymeme.core.plugins.policy import PluginPolicy

                    flush = PluginPolicy.flush_outputs

                    def emit(policy, operation, receiver):
                        def receive(kind, value):
                            outputs.append((kind, value))
                            receiver(kind, value)

                        flush(policy, operation, receive)

                    stack.enter_context(
                        patch.object(PluginPolicy, "flush_outputs", emit)
                    )
                    chosen = (
                        str(source / "missing")
                        if case == "missing_account"
                        else str(source)
                    )
                    assert api.start_wechat_import(chosen, case != "scan_only") == {
                        "ok": True
                    }
                    container.operations.wait(TaskKind.IMPORT_WECHAT, 5)
                    actual = api.get_wechat_import_progress()
                    worker = webui._import_workers["source.wechat"]
                    assert not worker._active and worker.operation._closed
                    assert not worker.operation._redaction_values
                    assert outputs and any(kind == "progress" for kind, _ in outputs)
                    assert not list(
                        (container.config.data_dir / "plugin-workspaces").rglob(
                            "operation-*"
                        )
                    )
                    assert all(
                        proc.waited and proc.stdout.closed and proc.stderr.closed
                        for proc in processes
                    )
                after = durable_snapshot(container)
                if case == "happy":
                    assert actual["status"] == "done" and actual["imported"] == 1
                    assert len(after["rows"]) == 1 and len(submissions) == 1
                else:
                    assert before == after and not submissions, (case, actual)
                expected_code = {
                    "missing_hash": "no_binary",
                    "helper_failure": "key_not_found",
                    "missing_account": "not_found",
                    "multiple_accounts": "multiple_accounts",
                }.get(case)
                if expected_code:
                    assert actual["error_code"] == expected_code, actual
                if case == "cancel":
                    assert actual["status"] == "cancelled"
                if case in (
                    "unsafe_cdn",
                    "unsafe_redirect",
                    "md5_mismatch",
                    "rejected_bytes",
                ):
                    assert actual["failed"] == 1 and actual["imported"] == 0
                assert db_path.read_bytes() == source_before
                assert http.closed == len(http.calls)
                observations[case] = {
                    "progress": actual,
                    "sink_count": len(submissions),
                    "http_count": len(http.calls),
                    "durable_unchanged": before == after,
                    "emitted": outputs,
                    "helper_count": len(processes),
                    "cleanup": True,
                }
        finally:
            container.close()
    return observations


def main():
    # Inputs only select probes; expected fixture claims are never accepted.
    parser = ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = {"status": "REJECTED", "observations": {}, "errors": []}
    try:
        fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
        if (
            fixture != {"schema_version": 1, "cases": CASES}
            or type(fixture["schema_version"]) is not int
        ):
            raise ValueError("fixture: exact schema_version/cases required")
        from ohmymeme.integrations.imports import wechat

        with tempfile.TemporaryDirectory(
            prefix="ohmm-wechat-qa-"
        ) as directory, ExitStack() as stack:
            for owner, name in (
                (socket, "getaddrinfo"),
                (socket, "create_connection"),
                (socket.socket, "connect"),
                (subprocess, "Popen"),
            ):
                stack.enter_context(
                    patch.object(
                        owner,
                        name,
                        side_effect=AssertionError("undeclared external access"),
                    )
                )
            stack.enter_context(
                patch.object(wechat.platform, "system", return_value="Windows")
            )
            report["observations"] = probe(Path(directory), fixture["cases"])
            baseline_path = args.report.parent / "todo-9-wechat-baseline.json"
            if baseline_path.exists():
                previous = json.loads(baseline_path.read_text(encoding="utf-8"))
                for case, observation in previous["observations"].items():
                    assert {
                        key: report["observations"][case][key]
                        for key in ("progress", "sink_count", "http_count")
                    } == observation, case
                report["baseline_equal"] = True
        report["cleanup"] = {
            "temporary_removed": not Path(directory).exists(),
            "network": "fixture only",
            "processes": "mock helper waited and streams closed; no external processes",
        }
        report["status"] = "PASS"
    except Exception as error:
        report["errors"].append(f"{type(error).__name__}: {error}")
    report["command_exit"] = 0 if report["status"] == "PASS" else 1
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "cases": len(report["observations"]),
                "errors": report["errors"],
                "command_exit": report["command_exit"],
            }
        )
    )
    return report["command_exit"]


if __name__ == "__main__":
    raise SystemExit(main())
