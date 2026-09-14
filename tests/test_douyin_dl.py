"""douyin_dl.py 单测 — 纯本地逻辑，不发起网络请求"""

import json
import socket
import subprocess
import unittest
from unittest.mock import Mock

import ohmymeme_plugin_douyin as douyin
import pytest

from ohmymeme.cli.douyin_dl import gen_random_str, gen_verify_fp, sign_url


class TestGenRandomStr(unittest.TestCase):
    def test_default_length(self):
        s = gen_random_str()
        self.assertEqual(len(s), 126)

    def test_custom_length(self):
        s = gen_random_str(50)
        self.assertEqual(len(s), 50)

    def test_alphanumeric(self):
        s = gen_random_str(100)
        self.assertTrue(s.isalnum())


class TestGenVerifyFp(unittest.TestCase):
    def test_format(self):
        fp = gen_verify_fp()
        self.assertTrue(fp.startswith("verify_"))
        # format: verify_<base36>_<36-char-uuid>
        # last 36 chars are the uuid (contains underscores at 8,13,18,23)
        uuid = fp[-36:]
        self.assertEqual(len(uuid), 36)

    def test_contains_4_at_14(self):
        fp = gen_verify_fp()
        uuid = fp[-36:]
        self.assertEqual(uuid[14], "4")

    def test_underscores_in_uuid(self):
        fp = gen_verify_fp()
        uuid = fp[-36:]
        self.assertEqual(uuid[8], "_")
        self.assertEqual(uuid[13], "_")
        self.assertEqual(uuid[18], "_")
        self.assertEqual(uuid[23], "_")

    def test_uuid_segments_lengths(self):
        fp = gen_verify_fp()
        uuid = fp[-36:]
        # XXXXXXXX_XXXX_XXXX_XXXX_XXXXXXXXXXXX
        segs = uuid.split("_")
        self.assertEqual(len(segs), 5)
        self.assertEqual(len(segs[0]), 8)
        self.assertEqual(len(segs[1]), 4)
        self.assertEqual(len(segs[2]), 4)
        self.assertEqual(len(segs[3]), 4)
        self.assertEqual(len(segs[4]), 12)

    def test_unique(self):
        a = gen_verify_fp()
        b = gen_verify_fp()
        self.assertNotEqual(a, b)


class TestSignUrl(unittest.TestCase):
    def test_contains_abogus(self):
        params = {"aid": "1128", "device_platform": "webapp"}
        url = sign_url("https://example.com/api", params)
        self.assertIn("a_bogus=", url)

    def test_contains_params(self):
        params = {"aid": "1128", "device_platform": "webapp"}
        url = sign_url("https://example.com/api", params)
        self.assertIn("aid=1128", url)
        self.assertIn("device_platform=webapp", url)

    def test_abogus_not_empty(self):
        params = {"aid": "1128"}
        url = sign_url("https://example.com/api", params)
        abogus_idx = url.index("a_bogus=") + len("a_bogus=")
        abogus_val = url[abogus_idx:]
        self.assertTrue(len(abogus_val) > 10)

    def test_different_params_different_abogus(self):
        url1 = sign_url("https://example.com/api", {"aid": "1128"})
        url2 = sign_url("https://example.com/api", {"aid": "6383"})
        a1 = url1.split("a_bogus=")[1]
        a2 = url2.split("a_bogus=")[1]
        self.assertNotEqual(a1, a2)


class TestDouyinCompatibility(unittest.TestCase):
    def test_403_sticker_api_keeps_sign_failed_sentinel(self):
        class Response:
            status_code = 403

        class Session:
            def request(self, *_args, **_kwargs):
                return Response()

        original_request = douyin._policy_request
        douyin._policy_request = lambda *_args, **_kwargs: Response()
        try:
            self.assertIsNone(douyin.create_plugin()._fetch_sticker_list(Session()))
        finally:
            douyin._policy_request = original_request


if __name__ == "__main__":
    unittest.main()


def test_douyin_factory_redacts_cookie_exception(tmp_path, monkeypatch):
    # Provider progress and errors use operation secrets, never global settings.
    import ohmymeme_plugin_douyin as implementation

    from ohmymeme.core.plugins.contracts import ImportPluginContext
    from ohmymeme.core.plugins.policy import (
        PluginConfigPort,
        PluginOperation,
        PluginSecretPort,
    )
    from ohmymeme.presentation.desktop.api.plugin_dispatch import _descriptor

    descriptor = _descriptor("source.douyin")
    secret = "sessionid=fixture-cookie-secret"
    emissions = []

    def fail(cookie):
        assert cookie == secret
        raise RuntimeError("rejected " + cookie)

    monkeypatch.setattr(implementation, "_build_session", fail)
    with PluginOperation(
        PluginConfigPort({}), PluginSecretPort({"cookie": secret}), descriptor, tmp_path
    ) as operation:
        context = ImportPluginContext(
            descriptor, None, emissions.append, lambda: False, operation=operation
        )
        provider = implementation.create_plugin()
        assert provider.start(context)
        provider.import_media(context)
        assert provider.get_progress()["error_code"] == "exception"
        assert secret not in repr(provider.get_progress())
        assert "[REDACTED]" in provider.get_progress()["error"]
        logs = operation._outputs._drain_events()
        assert emissions and logs
        assert secret not in repr(emissions) + repr(logs)
        assert "[REDACTED]" in repr(logs)


@pytest.mark.parametrize("failed_key", ["sessionid", "csrftoken"])
def test_douyin_cookie_components_are_redacted_by_real_bridge(
    tmp_path, monkeypatch, request, failed_key
):
    # Reuse the reviewer's real transport and Cookie.set component-value fault.
    from ohmymeme.app.container import Container
    from ohmymeme.core.adapters.fetch_policy import FetchPolicy
    from ohmymeme.core.plugins.policy import PluginPolicy
    from ohmymeme.presentation.desktop.window_manager import SettingsApi
    from scripts.plugin_remote_import_qa import Cookies, CurlFixture, Resolver

    tokens = {"sessionid": "wave3-session-secret", "csrftoken": "wave3-csrf-secret"}
    cookie = "; ".join(f"{key} = {value} " for key, value in tokens.items())
    session = CurlFixture("douyin_happy")
    events = []

    class BrokenCookies(Cookies):
        def set(self, key, value, **kwargs):
            # Fail after the actual parser has selected and trimmed one value.
            super().set(key, value, **kwargs)
            if key == failed_key:
                raise ValueError("Cookie value rejected: " + value)

    session.cookies = BrokenCookies()
    monkeypatch.setattr(douyin.requests, "Session", lambda **kwargs: session)
    monkeypatch.setattr(douyin, "_FETCH_POLICY", FetchPolicy(resolver=Resolver()))
    for owner, name in (
        (socket, "getaddrinfo"),
        (socket.socket, "connect"),
        (subprocess, "Popen"),
    ):
        monkeypatch.setattr(owner, name, Mock(side_effect=AssertionError("offline")))
    flush = PluginPolicy.flush_outputs

    def capture(policy, operation, emitter):
        # Record exactly what the host output port emits, not direct redact calls.
        def receive(kind, value):
            events.append((kind, value))
            emitter(kind, value)

        flush(policy, operation, receive)

    monkeypatch.setattr(PluginPolicy, "flush_outputs", capture)
    container = Container(tmp_path / "host")
    try:
        webui = container.create_webui()
        api = SettingsApi(webui, container.settings)
        assert api.start_douyin_import(cookie) == {"ok": True}
        worker = webui._import_workers["source.douyin"]
        container.operations.wait(worker._kind, 5)
        progress = api.get_douyin_import_progress()
        facts = {
            "failed_key": failed_key,
            "progress": progress,
            "emitted": events,
            "component_leaked": any(
                value in json.dumps([progress, events]) for value in tokens.values()
            ),
            "full_cookie_leaked": cookie in json.dumps([progress, events]),
            "session_closed": session.closed,
            "config_clean": all(
                value not in repr(container.config.to_dict())
                for value in tokens.values()
            ),
            "operation_closed": worker.operation._closed,
            "secrets_cleared": not worker.operation.secrets._secret_port._secrets,
            "workspace_removed": not list(
                (container.config.data_dir / "plugin-workspaces").rglob("operation-*")
            ),
            "sink_rows": len(container.db.search()),
            "http_calls": session.calls,
        }
        request.node.user_properties.append(("review_attack", json.dumps(facts)))
        assert not facts["component_leaked"] and not facts["full_cookie_leaked"]
        assert facts["session_closed"] and facts["config_clean"]
        assert facts["operation_closed"] and facts["secrets_cleared"]
        assert facts["workspace_removed"] and facts["sink_rows"] == 0
        assert progress["error"] == "Cookie value rejected: [REDACTED]"
        assert any(kind == "log" and "[REDACTED]" in value for kind, value in events)
    finally:
        # Red runs must also release their intentionally leaked fixture session.
        if not session.closed:
            session.close()
        container.close()


@pytest.mark.parametrize("phase", ["headers", "ttwid", "fingerprint", "cookie"])
@pytest.mark.parametrize("close_raises", [False, True])
def test_douyin_session_initialization_failure_closes_without_masking(
    monkeypatch, request, phase, close_raises
):
    # Every initialization stage owns the session until it returns successfully.
    from ohmymeme.core.adapters.fetch_policy import FetchPolicy
    from scripts.plugin_remote_import_qa import Cookies, CurlFixture, Resolver

    session = CurlFixture("douyin_happy")
    failure = ValueError("initialization failed: " + phase)
    original_close = session.close

    def close():
        # A secondary close error must not replace the initialization exception.
        original_close()
        if close_raises:
            raise RuntimeError("secondary close failure")

    session.close = Mock(side_effect=close)

    class BrokenCookies(Cookies):
        def set(self, key, value, **kwargs):
            # Exercise both automatic and user-supplied Cookie initialization.
            super().set(key, value, **kwargs)
            if (phase, key) in (("ttwid", "ttwid"), ("cookie", "sessionid")):
                raise failure

    session.cookies = BrokenCookies()
    if phase == "headers":
        session.headers = Mock(update=Mock(side_effect=failure))
    if phase == "fingerprint":
        monkeypatch.setattr(douyin, "_gen_verify_fp", Mock(side_effect=failure))
    monkeypatch.setattr(douyin.requests, "Session", lambda **kwargs: session)
    monkeypatch.setattr(douyin, "_FETCH_POLICY", FetchPolicy(resolver=Resolver()))
    try:
        with pytest.raises(ValueError) as error:
            douyin._build_session("sessionid=fixture-value")
        facts = {
            "phase": phase,
            "close_raises": close_raises,
            "session_closed": session.closed,
            "close_calls": session.close.call_count,
            "original_exception_preserved": error.value is failure,
            "http_calls": session.calls,
        }
        request.node.user_properties.append(("review_attack", json.dumps(facts)))
        assert facts["original_exception_preserved"]
        assert facts["session_closed"] and facts["close_calls"] == 1
    finally:
        original_close()


def test_douyin_session_success_transfers_close_ownership(monkeypatch):
    # A successfully initialized session remains open for the worker to use.
    from ohmymeme.core.adapters.fetch_policy import FetchPolicy
    from scripts.plugin_remote_import_qa import CurlFixture, Resolver

    session = CurlFixture("douyin_happy")
    monkeypatch.setattr(douyin.requests, "Session", lambda **kwargs: session)
    monkeypatch.setattr(douyin, "_FETCH_POLICY", FetchPolicy(resolver=Resolver()))
    try:
        assert douyin._build_session("sessionid=fixture-value") is session
        assert not session.closed
        assert session.cookies["sessionid"] == "fixture-value"
    finally:
        session.close()
