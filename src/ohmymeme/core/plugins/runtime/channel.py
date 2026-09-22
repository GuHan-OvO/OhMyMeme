# pyright: basic

"""宿主与 worker 之间的双向 RPC 通道。"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from . import protocol

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0


class RpcError(RuntimeError):
    def __init__(self, code, message, data=None):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message
        self.data = data


class RpcTimeout(RpcError):
    def __init__(self, method, timeout):
        super().__init__("timeout", "%s: no response in %.1fs" % (method, timeout))
        self.method = method


class ChannelClosed(RpcError):
    def __init__(self, reason="channel closed"):
        super().__init__("channel_closed", str(reason))


class RpcChannel:
    """读线程分发响应/通知/请求，写操作串行化。"""

    def __init__(
        self,
        reader,
        writer,
        on_request=None,
        on_notification=None,
        on_close=None,
        name="channel",
    ):
        self.name = name
        self._reader = reader
        self._writer = writer
        self._on_request = on_request
        self._on_notification = on_notification
        self._on_close = on_close
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending = {}
        self._next_id = 0
        self._closed = False
        self._closed_reason = ""
        self._executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix=name + "-req"
        )
        self._reader_thread = threading.Thread(
            target=self._read_loop, name=name + "-read", daemon=True
        )

    # 启动读线程
    def start(self):
        self._reader_thread.start()

    # 发送请求并等待响应
    def call(self, method, params=None, timeout=DEFAULT_TIMEOUT):
        if self._closed:
            raise ChannelClosed(self._closed_reason)
        with self._pending_lock:
            self._next_id += 1
            message_id = self._next_id
            slot = {"event": threading.Event(), "result": None, "error": None}
            self._pending[message_id] = slot
        try:
            self._send(protocol.build_request(message_id, method, params))
        except Exception:
            with self._pending_lock:
                self._pending.pop(message_id, None)
            raise
        if not slot["event"].wait(timeout):
            with self._pending_lock:
                self._pending.pop(message_id, None)
            raise RpcTimeout(method, timeout)
        if slot["error"] is not None:
            raise slot["error"]
        return slot["result"]

    # 发送单向通知；通道已关闭时静默丢弃
    def notify(self, method, params=None):
        if self._closed:
            return
        try:
            self._send(protocol.build_notification(method, params))
        except ChannelClosed:
            pass

    def is_closed(self):
        return self._closed

    # 关闭通道并唤醒所有等待中的调用
    def close(self, reason="channel closed"):
        with self._pending_lock:
            if self._closed:
                return
            self._closed = True
            self._closed_reason = str(reason)
            pending, self._pending = self._pending, {}
        error = ChannelClosed(reason)
        for slot in pending.values():
            slot["error"] = error
            slot["event"].set()
        try:
            self._writer.close()
        except Exception:
            pass
        try:
            self._reader.close()
        except Exception:
            pass
        self._executor.shutdown(wait=False, cancel_futures=True)
        if self._on_close is not None:
            try:
                self._on_close(str(reason))
            except Exception:
                logger.exception("channel on_close failed")

    # 帧写入加锁并立即刷新
    def _send(self, message):
        data = protocol.encode_frame(message)
        with self._write_lock:
            if self._closed:
                raise ChannelClosed(self._closed_reason)
            self._writer.write(data)
            self._writer.flush()

    # 读循环：响应/通知/请求分派，异常即关闭通道
    def _read_loop(self):
        reason = "read loop exited"
        try:
            while True:
                message = protocol.read_frame(self._reader)
                if message is None:
                    reason = "eof"
                    break
                kind = protocol.message_kind(message)
                if kind == "response":
                    self._resolve(message)
                elif kind == "notification":
                    self._dispatch_notification(message)
                elif kind == "request":
                    self._dispatch_request(message)
                else:
                    logger.warning("%s: invalid message", self.name)
        except Exception as error:
            reason = "%s: %s" % (type(error).__name__, error)
        finally:
            self.close(reason)

    # 唤醒等待中的调用槽
    def _resolve(self, message):
        message_id = message.get("id")
        with self._pending_lock:
            slot = self._pending.pop(message_id, None)
        if slot is None:
            return
        error = message.get("error")
        if isinstance(error, dict):
            slot["error"] = RpcError(
                error.get("code", "error"),
                error.get("message", ""),
                error.get("data"),
            )
        else:
            slot["result"] = message.get("result")
        slot["event"].set()

    # 通知回调直接在当前读线程执行
    def _dispatch_notification(self, message):
        if self._on_notification is None:
            return
        try:
            self._on_notification(message.get("method"), message.get("params") or {})
        except Exception:
            logger.exception("%s: notification handler failed", self.name)

    # 入站请求在线程池中执行，避免阻塞读循环
    def _dispatch_request(self, message):
        message_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        if self._on_request is None:
            self._send_error(message_id, protocol.RPC_METHOD_NOT_FOUND, "no handler")
            return

        def handle():
            try:
                result = self._on_request(method, params)
            except RpcError as error:
                self._send_error(message_id, error.code, error.message, error.data)
            except Exception as error:
                logger.exception("%s: request handler failed", self.name)
                self._send_error(message_id, protocol.RPC_INTERNAL_ERROR, str(error))
            else:
                try:
                    self._send(protocol.build_response(message_id, result))
                except ChannelClosed:
                    pass

        try:
            self._executor.submit(handle)
        except RuntimeError:
            self._send_error(message_id, protocol.RPC_INTERNAL_ERROR, "closing")

    # 发送错误响应，忽略已关闭的通道
    def _send_error(self, message_id, code, message, data=None):
        try:
            self._send(protocol.build_error(message_id, code, message, data))
        except ChannelClosed:
            pass
        except Exception:
            logger.exception("%s: failed to send error response", self.name)
