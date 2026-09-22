# pyright: basic

"""插件 worker 进程协议：Content-Length 帧与 JSON-RPC 2.0 风格消息。"""

import json

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 16 * 1024 * 1024
HEADER_LIMIT = 256

JSONRPC_VERSION = "2.0"

RPC_INVALID_REQUEST = -32600
RPC_METHOD_NOT_FOUND = -32601
RPC_INVALID_PARAMS = -32602
RPC_INTERNAL_ERROR = -32603
RPC_SERVER_ERROR = -32000

ERROR_REJECT = "reject"
ERROR_PROTOCOL = "protocol_error"
ERROR_UNSUPPORTED = "unsupported"
ERROR_OPERATION_BUSY = "operation_busy"
ERROR_OPERATION_UNKNOWN = "operation_unknown"
ERROR_LIMIT = "limit_exceeded"
ERROR_WORKER_FAILED = "worker_failed"


class ProtocolError(RuntimeError):
    pass


# 编码一个长度前缀的 JSON 帧
def encode_frame(payload):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(body) > MAX_FRAME_BYTES:
        raise ProtocolError("frame too large")
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


# 按指定长度完整读取字节，EOF 抛错
def _read_exact(stream, length):
    chunks = []
    remaining = length
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            raise ProtocolError("unexpected end of stream")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


# 读取一个长度前缀帧，流结束返回 None
def read_frame(stream):
    header = stream.readline()
    if not header:
        return None
    if len(header) > HEADER_LIMIT:
        raise ProtocolError("frame header too long")
    name, _, value = header.partition(b":")
    if name.strip().lower() != b"content-length":
        raise ProtocolError("invalid frame header")
    try:
        length = int(value.strip())
    except ValueError as error:
        raise ProtocolError("invalid frame length") from error
    if length < 0 or length > MAX_FRAME_BYTES:
        raise ProtocolError("frame too large")
    separator = stream.readline()
    if separator not in (b"\r\n", b"\n"):
        raise ProtocolError("invalid frame separator")
    body = _read_exact(stream, length)
    try:
        message = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError("invalid frame payload") from error
    if not isinstance(message, dict):
        raise ProtocolError("frame payload must be an object")
    return message


# 构造请求消息
def build_request(message_id, method, params=None):
    message = {"jsonrpc": JSONRPC_VERSION, "id": message_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


# 构造通知消息
def build_notification(method, params=None):
    message = {"jsonrpc": JSONRPC_VERSION, "method": method}
    if params is not None:
        message["params"] = params
    return message


# 构造成功响应
def build_response(message_id, result):
    return {"jsonrpc": JSONRPC_VERSION, "id": message_id, "result": result}


# 构造错误响应
def build_error(message_id, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": message_id, "error": error}


# 判定消息种类：request / response / notification / invalid
def message_kind(message):
    if not isinstance(message, dict) or message.get("jsonrpc") != JSONRPC_VERSION:
        return "invalid"
    if "method" in message:
        if "id" in message:
            return "request"
        return "notification"
    if "id" in message and ("result" in message or "error" in message):
        return "response"
    return "invalid"


# 从响应中取出结果，错误响应抛 ProtocolError
def message_result(message):
    error = message.get("error")
    if isinstance(error, dict):
        raise ProtocolError(
            "%s: %s" % (error.get("code", "error"), error.get("message", ""))
        )
    return message.get("result")
