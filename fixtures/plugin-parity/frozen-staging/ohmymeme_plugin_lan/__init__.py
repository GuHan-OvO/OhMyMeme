import json
import logging
import socket
import struct
import sys
import threading

from ohmymeme.core.plugins.network_config import validate_lan_config

logger = logging.getLogger(__name__)


class ByteConnection:
    def __init__(self, connection):
        # This object owns socket bytes only, never a key or a decoded frame.
        self._socket = connection
        self._close_lock = threading.Lock()
        self._closed = False

    def receive_bytes(self, size):
        # A short read is valid; host framing determines the exact byte count.
        return self._socket.recv(size)

    def send_bytes(self, data):
        # The host has already encoded and bounded this protocol message.
        self._socket.sendall(data)

    def settimeout(self, timeout):
        # Deadlines are selected by the host's handshake/session lifecycle.
        self._socket.settimeout(timeout)

    def close(self):
        # shutdown wakes concurrent recv on Windows and POSIX before close.
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._socket.close()


class LanTransport:
    def __init__(self, config):
        # No LanServer, Config, callback, or security state crosses this boundary.
        self.config = validate_lan_config(config)
        self.udp = None
        self.tcp = None
        self.port = config.port
        self.pktinfo = False

    def open(self, register_socket):
        # Register every listener before bind/listen or any blocking receive.
        try:
            self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            register_socket(self.tcp)
            self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.tcp.bind((self.config.host, self.port))
            self.port = self.tcp.getsockname()[1]
            self.tcp.listen(8)
            self.tcp.settimeout(0.5)
            self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            register_socket(self.udp)
            self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.udp.bind((self.config.host, self.port))
            self.udp.settimeout(0.5)
            try:
                self.udp.setsockopt(socket.IPPROTO_IP, socket.IP_PKTINFO, 1)
                self.pktinfo = True
            except (AttributeError, OSError):
                self.pktinfo = False
        except BaseException:
            self.close()
            raise

    def accept(self):
        # The host registers the session before starting handshake reads.
        connection, address = self.tcp.accept()
        return ByteConnection(connection), address

    def receive_discovery(self):
        # UDP discovery parsing contains no session protocol or secret data.
        if self.pktinfo:
            try:
                data, ancillary, _, address = self.udp.recvmsg(2048, 256)
            except (AttributeError, NotImplementedError):
                self.pktinfo = False
                return None
        else:
            data, address = self.udp.recvfrom(2048)
            ancillary = []
        try:
            message = json.loads(data.decode("utf-8", errors="replace"))
        except (ValueError, UnicodeError):
            return None
        if not isinstance(message, dict) or message.get("t") != "discover":
            return None
        return address, self._extract_pktinfo_src(ancillary)

    def _extract_pktinfo_src(self, ancdata):
        # Preserve Linux spec_dst and Windows interface-index layouts.
        for level, ctype, cdata in ancdata:
            if level != socket.IPPROTO_IP or ctype != socket.IP_PKTINFO:
                continue
            try:
                if sys.platform == "win32":
                    _, index = struct.unpack("4sI", cdata)
                    return ("ifindex", index) if index else None
                _, source, _ = struct.unpack("i4s4s", cdata)
                return "ip", socket.inet_ntoa(source)
            except (struct.error, OSError):
                return None
        return None

    def send_discovery(self, data, address, source):
        # Pin replies to the receiving interface whenever the OS supports it.
        if self.pktinfo and source:
            try:
                kind, value = source
                pktinfo = None
                if sys.platform == "win32" and kind == "ifindex":
                    source_ip = self._win_ifindex_source_ip(value, address)
                    if source_ip:
                        pktinfo = struct.pack("4sI", socket.inet_aton(source_ip), value)
                elif kind == "ip":
                    pktinfo = struct.pack(
                        "i4s4s", 0, socket.inet_aton(value), b"\0" * 4
                    )
                if pktinfo:
                    self.udp.sendmsg(
                        [data],
                        [(socket.IPPROTO_IP, socket.IP_PKTINFO, pktinfo)],
                        0,
                        address,
                    )
                    return
            except (AttributeError, NotImplementedError, struct.error, OSError):
                logger.debug("LAN UDP sendmsg unavailable; using sendto")
        self.udp.sendto(data, address)

    def _win_ifindex_source_ip(self, ifindex, peer):
        # UDP connect selects a route without sending a datagram.
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                option = getattr(socket, "IP_UNICAST_IF", 31)
                sock.setsockopt(socket.IPPROTO_IP, option, struct.pack("!I", ifindex))
                sock.connect((peer[0], peer[1]))
                return sock.getsockname()[0]
            finally:
                sock.close()
        except OSError:
            return None

    def close(self):
        # Accept/UDP waits are bounded, and shutdown additionally wakes listeners.
        for sock in (self.tcp, self.udp):
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()


def get_lan_ip():
    # Preserve the legacy route probe without transmitting application bytes.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


class Plugin:
    provider_id = "transport.lan"
    api_version = 1

    def start(self, context):
        # Activation is not a service start; the host owns listener lifecycle.
        pass

    def stop(self):
        # Host resources close byte transports before releasing state leases.
        pass

    def create_transport(self, config):
        # Listeners and connections belong to one host operation, never this factory.
        return LanTransport(config)

    def get_lan_ip(self):
        # The host selects this provider before allowing the route probe.
        return get_lan_ip()


def create_plugin():
    # Fresh factory identity, with no socket/thread at import or construction time.
    return Plugin()
