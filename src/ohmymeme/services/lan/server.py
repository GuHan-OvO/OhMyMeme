"""LAN v1 UDP/TCP runtime, session lifecycle, and authorization state."""

import hmac
import json
import logging
import os
import platform
import socket
import struct
import sys
import threading
import time

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    HAS_AESGCM = AESGCM is not None
except ImportError:
    HAS_AESGCM = False

from ohmymeme import __version__
from ohmymeme.core.domain import TaskKind
from ohmymeme.services.lan import protocol as lan_protocol
from ohmymeme.services.lan.commands import (
    MAX_FILE_SIZE,
    CommandHandlers,
    _detect_ext,
    _find_meme_file,
    _import_bytes,
    _safe_fname,
)

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
MAX_FRAME = lan_protocol.MAX_FRAME
_HANDSHAKE_TIMEOUT = 10
_IDLE_TIMEOUT = 60
_DEVICE_CONFIRM_TIMEOUT = 60
_IV_LEN = lan_protocol.IV_LEN
_TAG_LEN = lan_protocol.TAG_LEN
_REPLAY_CACHE_LIMIT = 4096
_REPLAY_TTL_SECONDS = 600
_LAN_START_TIMEOUT = 2
_LAN_STOP_TIMEOUT = 15
_STATE_MUTATING_COMMANDS = {"push_manifest", "push_file", "send_config"}

_lan_state = {
    "status": "stopped",
    "port": 0,
    "start_time": 0,
    "clients": [],
    "last_error": "",
    "allow_secret_config": False,
}
_lan_lock = threading.Lock()
_server = None
_confirm_cb = None

__all__ = [
    "AESGCM",
    "HAS_AESGCM",
    "MAX_FILE_SIZE",
    "MAX_FRAME",
    "PROTOCOL_VERSION",
    "LanServer",
    "_DEVICE_CONFIRM_TIMEOUT",
    "_IDLE_TIMEOUT",
    "_REPLAY_CACHE_LIMIT",
    "_REPLAY_TTL_SECONDS",
    "_derive_key",
    "_detect_ext",
    "_find_meme_file",
    "_import_bytes",
    "_safe_fname",
    "confirm_device",
    "get_lan_ip",
    "get_status",
    "set_allow_secret_config",
    "set_confirm_callback",
    "start",
    "stop",
]


class LanServer:
    """管理 LAN v1 的 UDP、TCP、会话及后台线程。"""

    def __init__(
        self,
        sync_service=None,
        coordinator=None,
        config=None,
        database=None,
        build_manifest=None,
        import_service_factory=None,
    ):
        self._lock = threading.Lock()
        self._running = False
        self._starting = False
        self._udp_sock = None
        self._tcp_sock = None
        self._threads = []
        self._session_sockets = set()
        self._port = 0
        self._secret = ""
        self._clients = {}
        self._udp_pktinfo = False
        self._confirm_lock = threading.Lock()
        self._replay_cache = lan_protocol.ReplayCache(
            capacity=_REPLAY_CACHE_LIMIT, ttl_seconds=_REPLAY_TTL_SECONDS
        )
        self._pending_confirms = {}
        self._logger = logger
        self._coordinator = coordinator
        self._config = config
        self._database = database
        self._build_manifest = build_manifest
        self._import_service_factory = import_service_factory
        self._confirm_cb = _confirm_cb
        self._operation_context = None
        self._start_ready = threading.Event()
        self._start_ok = False
        self._stop_deadline = None
        self._commands = CommandHandlers(self, sync_service)

    def start(self, port: int, secret: str) -> bool:
        """绑定 UDP/TCP listener 并启动服务线程。"""
        if not HAS_AESGCM:
            with _lan_lock:
                _lan_state["status"] = "error"
                _lan_state["last_error"] = "缺少 cryptography 依赖"
            return False
        with self._lock:
            if self._running or self._starting:
                return False
            self._starting = True
            self._start_ok = False
            self._start_ready.clear()
        if self._coordinator is not None:
            self._coordinator.start(
                TaskKind.LAN_SERVICE,
                lambda context: self._run_coordinated(context, port, secret),
            )
            if not self._start_ready.wait(timeout=_LAN_START_TIMEOUT):
                self._coordinator.cancel(TaskKind.LAN_SERVICE)
                return False
            return self._start_ok
        return self._start_service(port, secret)

    def _run_coordinated(self, context, port, secret):
        if not self._start_service(port, secret, context):
            return
        try:
            _ = context.wait_cancelled()
        finally:
            deadline = context.shutdown_deadline
            self._stop_resources(
                deadline or self._stop_deadline or time.monotonic() + _LAN_STOP_TIMEOUT
            )
            self._operation_context = None

    def _start_service(self, port, secret, context=None):
        self._port = int(port)
        self._secret = secret or ""
        try:
            self._udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._udp_sock.bind(("0.0.0.0", self._port))
            self._udp_sock.settimeout(0.5)
            self._udp_pktinfo = False
            try:
                self._udp_sock.setsockopt(socket.IPPROTO_IP, socket.IP_PKTINFO, 1)
                self._udp_pktinfo = True
            except (AttributeError, OSError):
                pass
            self._tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._tcp_sock.bind(("0.0.0.0", self._port))
            self._tcp_sock.listen(8)
            self._tcp_sock.settimeout(0.5)
            self._port = self._tcp_sock.getsockname()[1]
        except OSError as error:
            self._cleanup_sockets()
            with _lan_lock:
                _lan_state["status"] = "error"
                _lan_state["last_error"] = f"端口 {self._port} 无法监听: {error}"
            with self._lock:
                self._running = False
            self._complete_start(False)
            return False
        with self._lock:
            self._running = True
            self._operation_context = context
        if context is not None:
            context.register_socket(self._udp_sock)
            context.register_socket(self._tcp_sock)
        self._spawn_thread(self._udp_loop)
        self._spawn_thread(self._tcp_loop)
        with _lan_lock:
            _lan_state["status"] = "running"
            _lan_state["port"] = self._port
            _lan_state["start_time"] = int(time.time())
            _lan_state["last_error"] = ""
        logger.info(f"LAN 服务已启动，端口 {self._port}")
        self._complete_start(True)
        return True

    @property
    def port(self):
        return self._port

    def _complete_start(self, result):
        with self._lock:
            self._starting = False
            self._start_ok = result
        self._start_ready.set()

    def stop(self):
        """停止 listener 并等待现有服务线程。"""
        with self._lock:
            context = self._operation_context
            if not self._running and not self._starting:
                return
            deadline = time.monotonic() + _LAN_STOP_TIMEOUT
            self._stop_deadline = deadline
        if context is not None and self._coordinator is not None:
            self._coordinator.cancel(TaskKind.LAN_SERVICE)
            self._coordinator.wait(
                TaskKind.LAN_SERVICE, max(0.0, deadline - time.monotonic())
            )
            return
        self._stop_resources(deadline)

    def _stop_resources(self, deadline):
        with self._lock:
            if not self._running and not self._starting:
                return
            self._running = False
            self._starting = False
            sessions = tuple(self._session_sockets)
            self._session_sockets.clear()
            threads = tuple(self._threads)
            self._threads.clear()
        with self._confirm_lock:
            pending = tuple(self._pending_confirms.values())
            self._pending_confirms.clear()
        for entry in pending:
            entry["done"].set()
        with _lan_lock:
            if _lan_state.get("pending_confirm") in pending:
                _lan_state.pop("pending_confirm", None)
        for connection in sessions:
            try:
                connection.close()
            except OSError:
                pass
        self._cleanup_sockets()
        current = threading.current_thread()
        for thread in threads:
            if thread is not current:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
        with _lan_lock:
            _lan_state["status"] = "stopped"
            _lan_state["port"] = 0
            _lan_state["start_time"] = 0
            _lan_state["clients"] = []
        logger.info("LAN 服务已停止")

    def _cleanup_sockets(self):
        for sock in (self._udp_sock, self._tcp_sock):
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
        self._udp_sock = None
        self._tcp_sock = None

    def _spawn_thread(self, target):
        with self._lock:
            if not self._running:
                return
            thread = threading.Thread(
                target=target, daemon=self._operation_context is None
            )
            thread.start()
            self._threads.append(thread)

    def _udp_loop(self):
        while self._running:
            try:
                if self._udp_pktinfo:
                    data, ancdata, _, addr = self._udp_sock.recvmsg(2048, 256)
                else:
                    data, addr = self._udp_sock.recvfrom(2048)
                    ancdata = []
            except socket.timeout:
                continue
            except (AttributeError, NotImplementedError):
                self._udp_pktinfo = False
                continue
            except OSError:
                break
            try:
                msg = json.loads(data.decode("utf-8", errors="replace"))
            except Exception:
                continue
            if not isinstance(msg, dict) or msg.get("t") != "discover":
                continue
            reply = {
                "t": "hello",
                "name": platform.node(),
                "os": platform.system(),
                "ver": __version__,
                "need_secret": bool(self._secret),
            }
            try:
                self._send_udp_reply(
                    json.dumps(reply, ensure_ascii=False).encode("utf-8"),
                    addr,
                    self._extract_pktinfo_src(ancdata),
                )
            except OSError:
                pass

    def _extract_pktinfo_src(self, ancdata):
        """提取广播到达的本地接口。"""
        for level, ctype, cdata in ancdata:
            if level != socket.IPPROTO_IP or ctype != socket.IP_PKTINFO:
                continue
            try:
                if sys.platform == "win32":
                    _, ifindex = struct.unpack("4sI", cdata)
                    if not ifindex:
                        return None
                    return ("ifindex", ifindex)
                _, spec_dst, _ = struct.unpack("i4s4s", cdata)
                return ("ip", socket.inet_ntoa(spec_dst))
            except (struct.error, OSError):
                return None
        return None

    def _send_udp_reply(self, data, addr, src):
        """按收到发现报文的接口回发 UDP 响应。"""
        if self._udp_pktinfo and src:
            try:
                kind, value = src
                pktinfo = None
                if sys.platform == "win32" and kind == "ifindex":
                    source_ip = self._win_ifindex_source_ip(value, addr)
                    if source_ip:
                        pktinfo = struct.pack("4sI", socket.inet_aton(source_ip), value)
                elif kind == "ip":
                    pktinfo = struct.pack(
                        "i4s4s", 0, socket.inet_aton(value), b"\x00" * 4
                    )
                if pktinfo:
                    self._udp_sock.sendmsg(
                        [data],
                        [(socket.IPPROTO_IP, socket.IP_PKTINFO, pktinfo)],
                        0,
                        addr,
                    )
                    return
            except (AttributeError, NotImplementedError, struct.error, OSError):
                logger.debug("LAN UDP 回包 sendmsg 失败，退化为 sendto")
        self._udp_sock.sendto(data, addr)

    def _win_ifindex_source_ip(self, ifindex, peer):
        """按 Windows 接口索引解析 UDP 源地址。"""
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

    def _tcp_loop(self):
        while self._running:
            try:
                conn, addr = self._tcp_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self._register_session_socket(conn):
                continue
            self._spawn_thread(
                lambda connection=conn, address=addr: self._handle_conn(
                    connection, address
                )
            )

    def _handle_conn(self, conn, addr):
        try:
            conn.settimeout(_HANDSHAKE_TIMEOUT)
            key = self._handshake(conn)
            if not key:
                return
            conn.settimeout(_IDLE_TIMEOUT)
            self._clients[addr] = time.time()
            self._sync_clients()
            confirmed = threading.Event()
            authorized = threading.Event()
            if self._confirm_cb is None:
                confirmed.set()
                authorized.set()
            try:
                session_id = os.urandom(16).hex()
                self._session_loop(conn, key, confirmed, authorized, session_id)
            finally:
                self._clients.pop(addr, None)
                self._sync_clients()
        except (InvalidTag, OSError, ValueError, json.JSONDecodeError) as error:
            logger.debug(f"LAN conn {addr}: {error}")
        finally:
            with self._lock:
                self._session_sockets.discard(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _register_session_socket(self, conn):
        with self._lock:
            if not self._running:
                conn.close()
                return False
            self._session_sockets.add(conn)
            context = self._operation_context
        if context is not None:
            context.register_socket(conn)
        return True

    def _sync_clients(self):
        now = time.time()
        with _lan_lock:
            _lan_state["clients"] = [
                {"addr": f"{addr[0]}:{addr[1]}", "connected_at": int(connected_at)}
                for addr, connected_at in list(self._clients.items())
                if now - connected_at < _IDLE_TIMEOUT
            ]

    def _handshake(self, conn):
        if not self._secret:
            self._send_plain(conn, {"t": "ok"})
            return lan_protocol.derive_key("")
        nonce = os.urandom(16).hex()
        self._send_plain(conn, {"t": "challenge", "nonce": nonce})
        for _ in range(3):
            msg = self._recv_plain(conn)
            if not isinstance(msg, dict) or msg.get("t") != "proof":
                self._send_plain(conn, {"t": "no"})
                continue
            expected = lan_protocol.proof(self._secret, nonce)
            if not hmac.compare_digest(msg.get("mac", ""), expected):
                self._send_plain(conn, {"t": "no"})
                continue
            self._send_plain(conn, {"t": "ok"})
            return lan_protocol.derive_key(self._secret)
        return None

    def _session_loop(self, conn, key, confirmed, authorized, session_id=""):
        while self._running:
            msg, frame_id = self._recv_frame_with_identity(conn, key)
            if msg is None:
                break
            if not isinstance(msg, dict) or "cmd" not in msg:
                continue
            cmd = msg.get("cmd")
            if cmd == "device_info":
                result = self._cmd_device_info(msg, confirmed, authorized, session_id)
            elif not isinstance(cmd, str):
                result = {"ok": False, "error": f"未知命令: {cmd}"}
            elif not confirmed.is_set():
                if not confirmed.wait(timeout=_DEVICE_CONFIRM_TIMEOUT):
                    result = {"ok": False, "error": "设备未确认"}
                elif not authorized.is_set():
                    result = {"ok": False, "error": "设备未授权"}
                elif self._is_replayed_mutation(cmd, frame_id, session_id):
                    result = {"ok": False, "error": "重复请求"}
                else:
                    result = self._dispatch(msg)
            elif not authorized.is_set():
                result = {"ok": False, "error": "设备未授权"}
            elif self._is_replayed_mutation(cmd, frame_id, session_id):
                result = {"ok": False, "error": "重复请求"}
            else:
                result = self._dispatch(msg)
            if isinstance(result, dict):
                self._send_frame(conn, key, result)

    def _cmd_device_info(
        self, msg: dict, confirmed, authorized, session_id: str = ""
    ) -> dict:
        """完成设备确认并返回授权结果。"""
        if confirmed.is_set():
            if authorized.is_set():
                return {
                    "ok": True,
                    "approved": True,
                    "allow_secret_config": self._allow_secret_config(),
                }
            return {"ok": False, "error": "设备未授权"}
        device = {
            "name": msg.get("name", "未知设备"),
            "model": msg.get("model", ""),
            "os": msg.get("os", ""),
            "ver": msg.get("ver", ""),
        }
        with self._confirm_lock:
            confirm_id = os.urandom(16).hex()
            callback_device = dict(device, _confirm_id=confirm_id)
            entry = {
                "device": callback_device,
                "approved": False,
                "done": threading.Event(),
                "confirm_id": confirm_id,
                "session_id": session_id,
            }
            self._pending_confirms[confirm_id] = entry
            with _lan_lock:
                if "pending_confirm" not in _lan_state:
                    _lan_state["pending_confirm"] = entry
        context = self._operation_context
        if context is not None:
            context.register_temp(entry["done"].set)
        try:
            callback = self._confirm_cb
            if callback:
                try:
                    callback(callback_device)
                except Exception:  # noqa: BLE001, BROAD_EXCEPT_OK
                    logger.warning("device confirm callback error")
                entry["done"].wait(timeout=_DEVICE_CONFIRM_TIMEOUT)
            else:
                entry["approved"] = True
            approved = bool(entry["approved"])
        finally:
            with self._confirm_lock:
                self._pending_confirms.pop(confirm_id, None)
            with _lan_lock:
                if _lan_state.get("pending_confirm") is entry:
                    _lan_state.pop("pending_confirm", None)
        confirmed.set()
        if approved:
            authorized.set()
        return {
            "ok": True,
            "approved": approved,
            "allow_secret_config": self._allow_secret_config(),
        }

    def _allow_secret_config(self) -> bool:
        with _lan_lock:
            return bool(_lan_state["allow_secret_config"])

    def _is_replayed_mutation(
        self, cmd, frame_id, session_id: str = "", direction: str = "inbound"
    ):
        if not isinstance(cmd, str) or cmd not in _STATE_MUTATING_COMMANDS:
            return False
        return self._replay_cache.check_and_record(
            session_id or "legacy", direction, frame_id, cmd
        )

    def _dispatch(self, msg: dict) -> dict:
        return self._commands.dispatch(msg)

    def _cmd_pull_manifest(self) -> dict:
        return self._commands._cmd_pull_manifest()

    def _cmd_push_manifest(self, manifest) -> dict:
        return self._commands._cmd_push_manifest(manifest)

    def _cmd_pull_file(self, filename: str) -> dict:
        return self._commands._cmd_pull_file(filename)

    def _cmd_push_file(self, msg: dict) -> dict:
        return self._commands._cmd_push_file(msg)

    def _import_service(self):
        factory = self._import_service_factory
        if factory is not None:
            return factory()
        from ohmymeme.core.assets import AssetPaths
        from ohmymeme.core.config import get_config
        from ohmymeme.core.database import get_db
        from ohmymeme.core.imports import ImageImportService
        from ohmymeme.core.manifest import build as build_manifest

        config = self._config or get_config()
        database = self._database or get_db()
        builder = self._build_manifest or build_manifest
        return ImageImportService(
            database, AssetPaths(config.data_dir, config.cache_dir), builder
        )

    def set_confirm_callback(self, callback):
        """设置当前服务器的设备确认回调。"""
        previous = self._confirm_cb
        self._confirm_cb = callback
        return previous

    def confirm_device(self, approved: bool, confirm_id: str = ""):
        """完成当前服务器的待确认设备请求。"""
        return self._confirm_device(approved, confirm_id)

    def _confirm_device(self, approved: bool, confirm_id: str = ""):
        if not confirm_id:
            return
        with self._confirm_lock:
            entry = self._pending_confirms.get(confirm_id)
            if entry is None:
                return
            entry["approved"] = bool(approved)
            entry["done"].set()

    def get_status(self):
        """返回当前服务器状态。"""
        return get_status()

    def set_allow_secret_config(self, enabled: bool):
        """设置当前运行期的密钥配置传输策略。"""
        set_allow_secret_config(enabled)

    def get_lan_ip(self):
        """返回当前 LAN 地址。"""
        return get_lan_ip()

    def _cmd_get_config(self) -> dict:
        return self._commands._cmd_get_config()

    def _cmd_send_config(self, config) -> dict:
        return self._commands._cmd_send_config(config)

    def _recv_exact(self, conn, count: int) -> bytes:
        buffer = b""
        while len(buffer) < count:
            chunk = conn.recv(count - len(buffer))
            if not chunk:
                raise OSError("连接关闭")
            buffer += chunk
        return buffer

    def _send_plain(self, conn, obj: dict):
        conn.sendall(lan_protocol.encode_plain(obj))

    def _recv_plain(self, conn) -> dict:
        return lan_protocol.decode_plain(lambda size: self._recv_exact(conn, size))

    def _send_frame(self, conn, key: bytes, obj: dict):
        conn.sendall(lan_protocol.encode_frame(key, obj))

    def _recv_frame(self, conn, key: bytes) -> dict:
        msg, _ = self._recv_frame_with_identity(conn, key)
        return msg

    def _recv_frame_with_identity(self, conn, key: bytes):
        return lan_protocol.decode_frame(lambda size: self._recv_exact(conn, size), key)


def _derive_key(secret: str) -> bytes:
    """由共享密钥派生 AES-GCM 会话密钥。"""
    return lan_protocol.derive_key(secret)


def start(port: int = 17852, secret: str = "") -> bool:
    """启动局域网服务。"""
    global _server
    with _lan_lock:
        if _lan_state["status"] == "running" and _server:
            return True
    _server = LanServer()
    return _server.start(port, secret)


def stop():
    """停止局域网服务。"""
    with _lan_lock:
        if not _server:
            return
    _server.stop()


def get_status() -> dict:
    """返回服务状态。"""
    with _lan_lock:
        return {
            "status": _lan_state["status"],
            "port": _lan_state["port"],
            "start_time": _lan_state["start_time"],
            "clients": list(_lan_state["clients"]),
            "last_error": _lan_state["last_error"],
            "allow_secret_config": bool(_lan_state["allow_secret_config"]),
        }


def set_allow_secret_config(enabled: bool):
    """设置仅当前运行期有效的密钥传输策略。"""
    with _lan_lock:
        _lan_state["allow_secret_config"] = bool(enabled)


def set_confirm_callback(callback):
    """设置设备确认回调并返回之前的回调。"""
    global _confirm_cb
    old = _confirm_cb
    _confirm_cb = callback
    if _server is not None:
        _server._confirm_cb = callback
    return old


def confirm_device(approved: bool, confirm_id: str = ""):
    """提交当前待确认设备的授权决定。"""
    with _lan_lock:
        current = _server
    if current is not None:
        current._confirm_device(approved, confirm_id)


def get_lan_ip() -> str:
    """获取本机局域网 IP。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        return "127.0.0.1"
