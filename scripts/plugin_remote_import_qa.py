import hashlib
import io
import json
import socket
import subprocess
import tempfile
from argparse import ArgumentParser
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import ohmymeme_plugin_douyin as dy
import ohmymeme_plugin_telegram as tg
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

from ohmymeme.app.container import Container
from ohmymeme.core.adapters.fetch_policy import FetchPolicy
from ohmymeme.core.domain import TaskKind
from ohmymeme.core.plugins.policy import PluginPolicy
from ohmymeme.presentation.desktop.window_manager import SettingsApi


def tdata_fixture(root, payloads):
    # Supply real TDEF ciphertext; only the external key-file access is stubbed.
    key, salt = bytes(range(256)), bytes(range(64))
    root.mkdir(parents=True)
    (root / "key_datas").write_bytes(b"declared fixture key")
    cache = root / "user_data/cache"
    cache.mkdir(parents=True)
    for index, payload in enumerate(payloads):
        real_key = hashlib.sha256(key[:128] + salt[:32]).digest()
        iv = hashlib.sha256(key[128:] + salt[32:]).digest()[:16]
        header = bytes(16)
        header += hashlib.sha256(key + salt + header).digest()
        encryptor = Cipher(algorithms.AES(real_key), modes.CTR(iv)).encryptor()
        (cache / str(index)).write_bytes(
            b"TDEF" + salt + encryptor.update(header + payload)
        )
    return key


def image_bytes(format="PNG"):
    # Deterministic legal image bytes exercise actual decoding and admission.
    stream = io.BytesIO()
    Image.new("RGBA", (2, 2), (30, 70, 110, 255)).save(stream, format)
    return stream.getvalue()


class Cookies(dict):
    def set(self, key, value, **kwargs):
        # curl's cookie interface, without any ambient browser cookie source.
        self[key] = value


class CurlFixture:
    def __init__(self, case):
        # All requests are enumerated and recorded, never sent to a network.
        self.case = case
        self.headers = {}
        self.cookies = Cookies()
        self.calls = []
        self.closed = False

    def request(self, method, url, **kwargs):
        # Preserve real signing, pagination and Chrome124 session construction.
        from types import SimpleNamespace

        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        self.calls.append((method, parsed.hostname, parsed.path))
        assert kwargs["allow_redirects"] is False
        assert kwargs["resolve"] == [f"{parsed.hostname}:443:93.184.216.34"]
        status = 200
        if url == dy.API_TTWID and method == "POST":
            body = b"{}"
        elif parsed.path == urlsplit(dy.API_SELF).path:
            assert query.get("a_bogus")
            body = b'{"status_code":0}'
        elif parsed.path == urlsplit(dy.API_STICKER).path:
            assert query.get("a_bogus")
            cursor = query["custom_cursor"][0]
            assert cursor in ("0", "1")
            if self.case == "douyin_403":
                status, body = 403, b"forbidden"
            else:
                more = self.case == "douyin_happy" and cursor == "0"
                body = json.dumps(
                    {
                        "custom_sticker_page_list": {
                            "has_more": more,
                            "next_cursor": 1 if more else 0,
                            "resources": [
                                {
                                    "stickers": [
                                        {
                                            "id_str": cursor,
                                            "animate_url": {
                                                "url_list": ["https://cdn.fixture/meme"]
                                            },
                                        }
                                    ]
                                }
                            ],
                        }
                    }
                ).encode()
        elif url == "https://cdn.fixture/meme" and method == "GET":
            body = b"rejected bytes" if self.case == "rejected_bytes" else image_bytes()
        else:
            raise AssertionError("undeclared curl interaction")
        return SimpleNamespace(
            status_code=status,
            content=body,
            headers={},
            cookies={"ttwid": "fixture"},
            json=lambda: json.loads(body),
            raise_for_status=lambda: None,
        )

    def close(self):
        # Assert that the operation releases the authenticated session.
        self.closed = True
        self.cookies.clear()


class Resolver:
    def resolve(self, hostname, port):
        # Only the fixture's exact hosts are accepted; no DNS fallback exists.
        assert hostname in ("www.douyin.com", "ttwid.bytedance.com", "cdn.fixture")
        assert port == 443
        return ["93.184.216.34"]


def probe(root, cases):
    # Use the production registry/bridge path, not a fixture's claimed outcome.
    observations = {}
    for case in cases:
        container = Container(root / case / "host")
        webui = container.create_webui()
        api = SettingsApi(webui, container.settings)
        outputs = []
        try:
            with ExitStack() as stack:
                flush = PluginPolicy.flush_outputs

                def capture(policy, operation, emitter):
                    # Observe the values emitted after policy serialization.
                    def receive(kind, value):
                        outputs.append((kind, value))
                        emitter(kind, value)

                    flush(policy, operation, receive)

                stack.enter_context(
                    patch.object(PluginPolicy, "flush_outputs", capture)
                )
                if case in ("invalid_tdata", "missing_ffmpeg", "telegram_happy"):
                    path = root / case / "tdata"
                    if case != "invalid_tdata":
                        payloads = (
                            [b"\x1a\x45\xdf\xa3fixture"]
                            if case == "missing_ffmpeg"
                            else [image_bytes("WEBP")] * 21
                        )
                        key = tdata_fixture(path, payloads)

                        def read_key(key_path, passcode):
                            assert Path(key_path) == path / "key_datas"
                            assert passcode == "fixture-passcode"
                            return key

                        stack.enter_context(
                            patch.object(tg, "read_local_key", read_key)
                        )
                    stack.enter_context(
                        patch.object(tg, "_check_ffmpeg", return_value=False)
                    )
                    assert api.start_tg_import(str(path), "fixture-passcode", True) == {
                        "ok": True
                    }
                    container.operations.wait(TaskKind.IMPORT_TELEGRAM, 5)
                    actual = api.get_tg_import_progress()
                    if case == "invalid_tdata":
                        assert actual["error_code"] == "invalid_tdata", actual
                    elif case == "missing_ffmpeg":
                        assert actual["error_code"] == "no_ffmpeg", actual
                    else:
                        assert (
                            actual["status"] == "done" and actual["done"] == 21
                        ), actual
                        assert len(container.db.search()) == 1
                else:
                    session = CurlFixture(case)

                    def create_session(**kwargs):
                        assert kwargs == {"impersonate": "chrome124"}
                        return session

                    stack.enter_context(
                        patch.object(dy.requests, "Session", create_session)
                    )
                    stack.enter_context(
                        patch.object(
                            dy, "_FETCH_POLICY", FetchPolicy(resolver=Resolver())
                        )
                    )
                    assert api.start_douyin_import("sessionid=fixture-cookie") == {
                        "ok": True
                    }
                    container.operations.wait(TaskKind.IMPORT_DOUYIN, 5)
                    actual = api.get_douyin_import_progress()
                    assert session.closed
                    if case == "douyin_403":
                        assert actual["error_code"] == "sign_failed", actual
                    elif case == "rejected_bytes":
                        assert (
                            actual["download_failed"] == 1 and not container.db.search()
                        ), actual
                    else:
                        assert (
                            actual["status"] == "done" and actual["total"] == 2
                        ), actual
                        assert len(container.db.search()) == 1
                    actual["fixture_request_count"] = len(session.calls)
                assert "fixture-passcode" not in repr(actual)
                assert "fixture-cookie" not in repr(actual)
                assert outputs and "fixture-passcode" not in repr(outputs)
                assert "fixture-cookie" not in repr(outputs)
                assert "fixture-passcode" not in repr(container.config.to_dict())
                assert "fixture-cookie" not in repr(container.config.to_dict())
                worker = next(iter(webui._import_workers.values()))
                assert worker.operation._closed
                assert worker.operation.secrets._secret_port._secrets == {}
                assert not list(
                    (container.config.data_dir / "plugin-workspaces").rglob(
                        "operation-*"
                    )
                )
                observations[case] = {
                    key: value
                    for key, value in actual.items()
                    if key not in ("error", "message")
                }
                observations[case]["emitted"] = outputs
        finally:
            container.close()
    return observations


def main():
    # The exact fixture selects bounded real probes; malformed input fails closed.
    parser = ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = {"status": "REJECTED", "observations": {}, "errors": []}
    try:
        fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
        expected = [
            "invalid_tdata",
            "missing_ffmpeg",
            "telegram_happy",
            "douyin_403",
            "rejected_bytes",
            "douyin_happy",
        ]
        if (
            fixture != {"schema_version": 1, "cases": expected}
            or type(fixture["schema_version"]) is not int
        ):
            raise ValueError("fixture: exact schema_version/cases required")
        with tempfile.TemporaryDirectory(
            prefix="ohmm-remote-qa-"
        ) as directory, ExitStack() as stack:
            for owner, name in (
                (socket, "getaddrinfo"),
                (socket, "create_connection"),
                (subprocess, "Popen"),
            ):
                stack.enter_context(
                    patch.object(
                        owner,
                        name,
                        side_effect=AssertionError("undeclared external access"),
                    )
                )
            report["observations"] = probe(Path(directory), fixture["cases"])
        report["status"] = "PASS"
        report["cleanup"] = (
            "sessions closed; operations drained; secrets cleared; "
            "temporary trees removed"
        )
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
