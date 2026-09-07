"""Run Todo 18 FetchPolicy and adapter scenarios in isolated roots."""

import argparse
import http.server
import io
import json
import os
import shutil
import tempfile
import threading
from pathlib import Path
from secrets import token_urlsafe

from PIL import Image

from ohmymeme.core.adapters import fetch_policy as fetch_module
from ohmymeme.core.adapters.fetch_policy import FetchPolicy
from ohmymeme.integrations.imports import douyin, qqnt, telegram, wechat
from ohmymeme.services import updates
from scripts.baseline_contracts import canonical_bytes, sha256_path

SCENARIOS = (
    "public-pinned-redirect",
    "unsafe-address-set",
    "local-server-unsafe-dns",
    "local-server-connector",
    "proxy-bypass",
    "trusted-cdn",
    "oversize",
    "bad-image",
    "adapter-matrix",
)
SOURCE_PATHS = (
    "mise.toml",
    "src/ohmymeme/core/adapters/fetch_policy.py",
    "src/ohmymeme/core/adapters/fetch_policy_validation.py",
    "src/ohmymeme/core/adapters/fetch_policy_transport.py",
    "src/ohmymeme/services/updates.py",
    "src/ohmymeme/integrations/imports/adb_qq.py",
    "src/ohmymeme/integrations/imports/qqnt.py",
    "src/ohmymeme/integrations/imports/telegram.py",
    "src/ohmymeme/integrations/imports/douyin.py",
    "src/ohmymeme/integrations/imports/wechat.py",
    "src/ohmymeme/integrations/imports/abogus.py",
    "src/ohmymeme/presentation/desktop/window_manager.py",
    "tests/adapters/importers/test_fetch_policy.py",
    "tests/adapters/importers/test_wechat.py",
    "tests/application/test_storage_settings.py",
    "tests/application/test_updater.py",
    "tests/test_adb_util.py",
    "tests/test_douyin_dl.py",
    "tests/test_qqnt_import_boundary.py",
    "tests/test_tg_stickers.py",
    "scripts/run_task_18_fetch_evidence.py",
    "scripts/build_task_18_evidence.py",
)


class _Response:
    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self.body = body
        self.headers = headers or {}

    def read(self, size=-1):
        if size < 0:
            size = len(self.body)
        chunk, self.body = self.body[:size], self.body[size:]
        return chunk

    def close(self):
        return None


class _Resolver:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def resolve(self, hostname, port):
        self.calls.append((hostname, port))
        return self.answers.pop(0)


class _Connector:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.proxy_state = []

    def open(self, target, address, timeout, headers):
        self.calls.append(
            {
                "host": target.hostname,
                "port": target.port,
                "peer": address.ip,
                "sni": target.hostname,
                "host_header": target.authority,
                "timeout": timeout,
            }
        )
        self.proxy_state.append(
            {name: os.environ.get(name) for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
        )
        return self.responses.pop(0)


class _Redirect:
    def __init__(self, locations):
        self.locations = list(locations)

    def next_url(self, current_url, response):
        return self.locations.pop(0) if self.locations else None


class _CurlResponse:
    def __init__(self, status_code=200, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self.cookies = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http error")


class _CurlSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def _png():
    output = io.BytesIO()
    Image.new("RGBA", (2, 2), (255, 0, 0, 255)).save(output, "PNG")
    return output.getvalue()


def _scenario(run_id, name, evidence_root):
    root = Path(tempfile.mkdtemp(prefix="task-18-", dir=evidence_root))
    row = None
    try:
        body = _png()
        if name == "public-pinned-redirect":
            resolver = _Resolver([["93.184.216.34"], ["93.184.216.35"]])
            connector = _Connector(
                [_Response(302, headers={"Location": "https://cdn.example/a.png"}), _Response(200, body)]
            )
            policy = FetchPolicy(
                resolver=resolver,
                connector=connector,
                redirect=_Redirect(["https://cdn.example/a.png"]),
            )
            result = policy.fetch_bytes("https://public.example/a.png", image=True)
            assertions = [result == body, len(connector.calls) == 2]
            observed = {"resolver": resolver.calls, "connector": connector.calls}
        elif name == "unsafe-address-set":
            resolver = _Resolver([["93.184.216.34", "100.64.0.1"]])
            policy = FetchPolicy(resolver=resolver)
            try:
                policy.fetch_bytes("https://public.example/a.png")
            except Exception as error:
                assertions = [type(error).__name__ == "FetchRejected"]
            else:
                assertions = [False]
            observed = {"resolver": resolver.calls, "rejected": True}
        elif name == "local-server-unsafe-dns":
            class Handler(http.server.BaseHTTPRequestHandler):
                request_count = 0

                def do_GET(self):
                    type(self).request_count += 1
                    self.send_response(200)
                    self.end_headers()

                def log_message(self, format, *args):
                    return None

            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                resolver = _Resolver([["127.0.0.1"]])
                policy = FetchPolicy(resolver=resolver)
                try:
                    policy.fetch_bytes("https://public.example/")
                except Exception as error:
                    assertions = [type(error).__name__ == "FetchRejected"]
                else:
                    assertions = [False]
                observed = {
                    "resolver": resolver.calls,
                    "local_server_requests": Handler.request_count,
                }
                assertions.append(Handler.request_count == 0)
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()
        elif name == "local-server-connector":
            class Handler(http.server.BaseHTTPRequestHandler):
                request_count = 0
                received_host = ""

                def do_GET(self):
                    type(self).request_count += 1
                    type(self).received_host = self.headers.get("Host", "")
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"local-ok")

                def log_message(self, format, *args):
                    return None

            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                port = server.server_address[1]
                target = fetch_module.FetchTarget(
                    "http", "127.0.0.1", port, f"127.0.0.1:{port}", "/"
                )
                address = fetch_module.ResolvedAddress(
                    fetch_module.socket.AF_INET,
                    ("127.0.0.1", port),
                    "127.0.0.1",
                )
                response = fetch_module._DirectConnector().open(
                    target, address, 10, {"Host": "evil.example"}
                )
                try:
                    payload = response.read()
                finally:
                    response.close()
                assertions = [
                    payload == b"local-ok",
                    Handler.request_count == 1,
                    Handler.received_host == f"127.0.0.1:{port}",
                ]
                observed = {
                    "local_server_requests": Handler.request_count,
                    "peer": address.ip,
                    "host": Handler.received_host,
                    "policy": "transport-only; loopback rejected by FetchPolicy",
                }
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()
        elif name == "proxy-bypass":
            resolver = _Resolver([["93.184.216.34"]])
            connector = _Connector([_Response(200, body)])
            policy = FetchPolicy(resolver=resolver, connector=connector)
            saved_proxy = {
                name: os.environ.get(name)
                for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
            }
            os.environ["HTTP_PROXY"] = "redacted"
            os.environ["HTTPS_PROXY"] = "redacted"
            os.environ["ALL_PROXY"] = "redacted"
            try:
                policy.fetch_bytes("https://public.example/a.png", image=True)
                assertions = [
                    connector.proxy_state
                    == [{"HTTP_PROXY": None, "HTTPS_PROXY": None, "ALL_PROXY": None}]
                ]
                observed = {"connector": connector.calls, "proxy_seen": connector.proxy_state}
            finally:
                for name_key, value in saved_proxy.items():
                    if value is None:
                        os.environ.pop(name_key, None)
                    else:
                        os.environ[name_key] = value
        elif name == "trusted-cdn":
            resolver = _Resolver([["93.184.216.34"]])
            connector = _Connector([_Response(200, body)])
            policy = FetchPolicy(resolver=resolver, connector=connector)
            result = policy.fetch_bytes(
                "https://vweixinf.tc.qq.com/a.png",
                trusted_hosts={"vweixinf.tc.qq.com"},
                image=True,
            )
            assertions = [result == body]
            observed = {"resolver": resolver.calls, "connector": connector.calls}
        elif name == "oversize":
            resolver = _Resolver([["93.184.216.34"]])
            connector = _Connector([_Response(200, b"x" * (20 * 1024 * 1024 + 1))])
            policy = FetchPolicy(resolver=resolver, connector=connector)
            try:
                policy.fetch_bytes("https://public.example/a.bin")
            except Exception as error:
                assertions = [type(error).__name__ == "FetchLimitError"]
            else:
                assertions = [False]
            observed = {"resolver": resolver.calls, "connector": connector.calls}
        elif name == "bad-image":
            resolver = _Resolver([["93.184.216.34"]])
            connector = _Connector([_Response(200, b"not-image")])
            policy = FetchPolicy(resolver=resolver, connector=connector)
            try:
                policy.fetch_bytes("https://public.example/a.png", image=True)
            except Exception as error:
                assertions = [type(error).__name__ == "FetchRejected"]
            else:
                assertions = [False]
            observed = {"resolver": resolver.calls, "connector": connector.calls}
        else:
            original_policy = douyin._FETCH_POLICY
            original_update_policy = updates._FETCH_POLICY
            original_wechat_policy = wechat._FETCH_POLICY
            original_qqnt_policy = qqnt._FETCH_POLICY
            nickname_cache = root / "nickname.json"
            try:
                resolver = _Resolver([["93.184.216.34"], ["93.184.216.34"]])
                connector = _Connector([_Response(200, body)])
                policy = FetchPolicy(resolver=resolver, connector=connector)
                updates._FETCH_POLICY = policy
                dest = root / "release.bin"
                updates._try_download(
                    "https://public.example/release.bin",
                    str(dest),
                    None,
                    expected_sha256=__import__("hashlib").sha256(body).hexdigest(),
                )
                douyin._FETCH_POLICY = policy
                session = _CurlSession([_CurlResponse(200, body)])
                douyin._policy_request(session, "GET", "https://public.example/a.png")
                wechat_policy = type(
                    "BytePolicy",
                    (),
                    {"fetch_bytes": lambda self, url, **kwargs: body},
                )()
                wechat._FETCH_POLICY = wechat_policy
                imported = wechat._download_sticker("https://vweixinf.tc.qq.com/a.png")
                qqnt._FETCH_POLICY = wechat_policy
                nickname_cache.write_text("{}", encoding="utf-8")
                nickname = qqnt.get_user_nickname("10001", str(nickname_cache))
                telegram_result = telegram._FETCH_POLICY.validate_bytes(body, image=True)
                assertions = [
                    dest.read_bytes() == body,
                    session.calls[0][2]["proxies"] == {},
                    imported == body,
                    nickname == "",
                    telegram_result == body,
                ]
                observed = {
                    "importers": {
                        "updater_atomic_download": dest.exists(),
                        "douyin_resolve": session.calls[0][2]["resolve"],
                        "wechat_image": imported == body,
                        "qqnt_nickname": nickname == "",
                        "telegram_image": telegram_result == body,
                    },
                    "connector": connector.calls,
                }
            finally:
                douyin._FETCH_POLICY = original_policy
                updates._FETCH_POLICY = original_update_policy
                wechat._FETCH_POLICY = original_wechat_policy
                qqnt._FETCH_POLICY = original_qqnt_policy
        if not all(assertions):
            raise RuntimeError("scenario assertion failed")
        row = {
            "execution_identity": {"run_id": run_id, "row_id": token_urlsafe(18)},
            "executed": True,
            "assertions": assertions,
            "observed": observed,
            "cleanup": {
                "temporary_root_removed": False,
                "temporary_downloads_removed": False,
                "credentials_recorded": False,
                "proxy_values_recorded": False,
                "payloads_recorded": False,
                "payloads_removed": False,
            },
        }
        return row
    finally:
        shutil.rmtree(root, ignore_errors=True)
        if row is not None:
            row["cleanup"]["temporary_root_removed"] = not root.exists()
            row["cleanup"]["temporary_downloads_removed"] = not root.exists()
            row["cleanup"]["payloads_removed"] = not root.exists()


def run_matrix(evidence_root):
    run_id = token_urlsafe(24)
    return {
        "execution_identity": run_id,
        "source_sha256": {
            path: sha256_path(Path(path)) for path in SOURCE_PATHS
        },
        "scenarios": {
            name: _scenario(run_id, name, evidence_root) for name in SCENARIOS
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-root", required=True)
    args = parser.parse_args(argv)
    evidence_root = Path(args.evidence_root).resolve()
    if not evidence_root.is_dir():
        return 2
    print(canonical_bytes(run_matrix(evidence_root)).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
