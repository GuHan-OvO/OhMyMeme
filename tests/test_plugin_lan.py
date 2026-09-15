import importlib
import socket
import threading
from unittest.mock import Mock, patch

import pytest

from ohmymeme.services.lan.server import LanServer


@pytest.mark.parametrize("port,secret,field", [(True, "", "port"), (0, [], "secret")])
def test_lan_config_rejected_before_network(port, secret, field):
    # Construction errors retain the public false sentinel, with an exact field.
    server = LanServer()
    with patch("socket.socket", side_effect=AssertionError("network")) as network:
        assert server.start(port, secret) is False
        assert field in server.get_status()["last_error"]
        network.assert_not_called()


def test_lan_missing_provider_does_not_bind():
    from ohmymeme.core.plugins.registry import PluginRegistry

    server = LanServer(registry=PluginRegistry((), {}, ()))
    with patch("socket.socket", side_effect=AssertionError("network")) as network:
        assert server.start(0, "") is False
        network.assert_not_called()


def test_close_wakes_blocked_byte_read():
    # Events observe entry into recv; closing must wake it without a timeout/sleep.
    module = importlib.import_module("ohmymeme_plugin_lan")
    left, right = socket.socketpair()
    entered, exited = threading.Event(), threading.Event()

    class Wire:
        def recv(self, size):
            entered.set()
            return left.recv(size)

        def shutdown(self, how):
            left.shutdown(how)

        def close(self):
            left.close()

    transport = module.ByteConnection(Wire())

    def read():
        try:
            transport.receive_bytes(1)
        except OSError:
            pass
        finally:
            exited.set()

    thread = threading.Thread(target=read)
    try:
        thread.start()
        assert entered.wait(1)
        transport.close()
        transport.close()
        assert exited.wait(1)
    finally:
        right.close()
        transport.close()
        thread.join(1)
    assert not thread.is_alive()


def test_invalid_approval_mapping_never_loads_provider():
    # Only a host callback or explicit headless None is a valid approval mapping.
    registry = Mock()
    server = LanServer(registry=registry)
    server._confirm_cb = "allow"
    with patch("socket.socket", side_effect=AssertionError("network")) as network:
        assert server.start(0, "") is False
        assert "approval" in server.get_status()["last_error"]
        registry.get.assert_not_called()
        network.assert_not_called()


def test_malformed_proof_is_rejected_without_crashing():
    # The byte provider cannot make an untyped proof bypass host HMAC validation.
    server = LanServer()
    server._secret = "fixture"
    messages = iter([{"t": "proof", "mac": []}] * 3)
    replies = []
    server._recv_plain = lambda conn: next(messages)
    server._send_plain = lambda conn, message: replies.append(message)
    assert server._handshake(None) is None
    assert [item["t"] for item in replies] == ["challenge", "no", "no", "no"]


def test_listener_registration_precedes_blocking_operations():
    # The actual package's listener setup calls the narrow host resource hook first.
    module = importlib.import_module("ohmymeme_plugin_lan")
    from ohmymeme.core.plugins.network_config import LanTransportConfig

    registered = []
    tcp, udp = Mock(), Mock()
    tcp.getsockname.return_value = ("127.0.0.1", 12345)
    tcp.bind.side_effect = lambda address: registered.index(tcp)
    udp.bind.side_effect = lambda address: registered.index(udp)
    with patch.object(module.socket, "socket", side_effect=[tcp, udp]):
        transport = module.create_plugin().create_transport(LanTransportConfig(port=0))
        try:
            transport.open(registered.append)
            assert registered == [tcp, udp]
            udp.bind.assert_called_once_with(("0.0.0.0", 12345))
        finally:
            transport.close()


def test_udp_recvmsg_and_sendmsg_fallback():
    # Unsupported ancillary I/O downgrades only transport, not the wire message.
    module = importlib.import_module("ohmymeme_plugin_lan")
    from ohmymeme.core.plugins.network_config import LanTransportConfig

    transport = module.create_plugin().create_transport(LanTransportConfig())
    wire = Mock()
    transport.udp = wire
    transport.pktinfo = True
    wire.recvmsg.side_effect = NotImplementedError
    wire.recvfrom.return_value = (b'{"t":"discover"}', ("127.0.0.1", 1))
    assert transport.receive_discovery() is None
    assert transport.receive_discovery() == (("127.0.0.1", 1), None)
    transport.pktinfo = True
    wire.sendmsg.side_effect = NotImplementedError
    with patch.object(module.socket, "IP_PKTINFO", 8, create=True):
        transport.send_discovery(b"reply", ("127.0.0.1", 1), ("ip", "127.0.0.1"))
    wire.sendto.assert_called_once_with(b"reply", ("127.0.0.1", 1))


def test_settings_bridge_does_not_coerce_invalid_saved_port(tmp_path):
    # Exercise the normal fixed UI entry, not just direct LanServer.start.
    from ohmymeme.app.container import Container
    from ohmymeme.presentation.desktop.window_manager import SettingsApi

    container = Container(tmp_path)
    try:
        container.config.set("lan_port", True)
        api = SettingsApi(container.create_webui(), container.settings)
        with patch(
            "socket.socket", side_effect=AssertionError("invalid config network")
        ) as network:
            result = api.lan_start()
        assert result["ok"] is False
        assert "lan_port" in result["status"]["last_error"]
        network.assert_not_called()
    finally:
        container.close()
        container.db.close()
