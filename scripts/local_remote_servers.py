"""Deterministic loopback FTP, WebDAV, and S3-compatible test servers."""
# noqa: SIZE_OK

from __future__ import annotations

import base64
from email.message import Message
import hashlib
import hmac
import http.server
import ipaddress
import posixpath
import socket
import socketserver
import ssl
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse, urlsplit


@dataclass(frozen=True, slots=True)
class LocalCertificate:
    certfile: Path
    keyfile: Path
    cafile: Path


def create_certificate(root: Path) -> LocalCertificate:
    """Create a temporary CA and a localhost certificate for explicit local trust."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "OhMyMeme local CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC))
        .not_valid_after(datetime.now(UTC).replace(year=2036))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]
    )
    server = (
        x509.CertificateBuilder()
        .subject_name(server_subject)
        .issuer_name(subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC))
        .not_valid_after(datetime.now(UTC).replace(year=2036))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    ca_path = root / "local-ca.pem"
    cert_path = root / "local-server.pem"
    key_path = root / "local-server.key"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(server.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return LocalCertificate(cert_path, key_path, ca_path)


class _FtpState:
    def __init__(self, username: str, password: str) -> None:
        self.lock = threading.Lock()
        self.files: dict[str, bytes] = {}
        self.directories = {"/"}
        self.request_log: list[dict[str, str | int]] = []
        self.fail_next: str | None = None
        self.username = username
        self.password = password


class _FtpHandler(socketserver.StreamRequestHandler):
    def setup(self) -> None:
        super().setup()
        self.cwd = "/"
        self.username = ""
        self.authenticated = not self.state.username
        self.data_listener: socket.socket | None = None
        self.tls_active = False
        self.prot_private = False

    @property
    def state(self) -> _FtpState:
        return getattr(self.server, "state")

    @property
    def tls_context(self) -> ssl.SSLContext | None:
        return getattr(self.server, "tls_context")

    def send_response(self, code: int, message: str) -> None:
        self.wfile.write(f"{code} {message}\r\n".encode("ascii"))
        self.wfile.flush()

    def handle(self) -> None:
        self.send_response(220, "OhMyMeme local FTP")
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            command, _, argument = raw.decode("utf-8", "replace").strip().partition(" ")
            command = command.upper()
            argument = argument.strip()
            self.state.request_log.append({"command": command, "argument": argument})
            if command == "AUTH" and argument.upper() == "TLS":
                self.send_response(234, "AUTH TLS successful")
                if self.tls_context is None:
                    return
                self.connection = self.tls_context.wrap_socket(self.connection, server_side=True)
                self.rfile = self.connection.makefile("rb")
                self.wfile = self.connection.makefile("wb")
                self.tls_active = True
            elif command == "USER":
                self.username = argument
                self.send_response(331, "password required")
            elif command == "PASS":
                self.authenticated = (
                    not self.state.username
                    or self.username == self.state.username
                    and argument == self.state.password
                )
                self.send_response(230 if self.authenticated else 530, "logged in" if self.authenticated else "authentication failed")
            elif self.state.username and not self.authenticated:
                self.send_response(530, "authentication required")
            elif command in {"PBSZ", "PROT"}:
                self.prot_private = command == "PROT" and argument.upper() == "P"
                self.send_response(200, "ok")
            elif command in {"TYPE", "OPTS", "SYST", "NOOP"}:
                self.send_response(200, "ok")
            elif command == "PWD":
                self.send_response(257, f'"{self.cwd}"')
            elif command == "CWD":
                path = self._path(argument)
                if path in self.state.directories:
                    self.cwd = path
                    self.send_response(250, "directory changed")
                else:
                    self.send_response(550, "directory unavailable")
            elif command == "MKD":
                path = self._path(argument)
                with self.state.lock:
                    self.state.directories.add(path)
                self.send_response(257, f'"{path}" created')
            elif command == "PASV":
                self._pasv()
            elif command == "NLST":
                self._transfer_listing(argument)
            elif command == "STOR":
                self._store(argument)
            elif command == "RETR":
                self._retrieve(argument)
            elif command == "SIZE":
                path = self._path(argument)
                if path not in self.state.files:
                    self.send_response(550, "file unavailable")
                else:
                    self.send_response(213, str(len(self.state.files[path])))
            elif command == "DELE":
                path = self._path(argument)
                with self.state.lock:
                    if path not in self.state.files:
                        self.send_response(550, "file unavailable")
                    else:
                        del self.state.files[path]
                        self.send_response(250, "deleted")
            elif command == "QUIT":
                self.send_response(221, "bye")
                return
            else:
                self.send_response(502, "unsupported")

    def _path(self, raw: str) -> str:
        value = raw if raw.startswith("/") else posixpath.join(self.cwd, raw)
        return posixpath.normpath(value) or "/"

    def _pasv(self) -> None:
        self.data_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.data_listener.bind(("127.0.0.1", 0))
        self.data_listener.listen(1)
        port = self.data_listener.getsockname()[1]
        self.send_response(227, f"Entering Passive Mode (127,0,0,1,{port // 256},{port % 256})")

    def _data_connection(self) -> socket.socket | None:
        listener = self.data_listener
        self.data_listener = None
        if listener is None:
            self.send_response(425, "use PASV first")
            return None
        try:
            connection, _ = listener.accept()
        finally:
            listener.close()
        return connection

    def _secure_data_connection(self, connection: socket.socket) -> socket.socket:
        if self.tls_active and self.prot_private and self.tls_context is not None:
            try:
                return self.tls_context.wrap_socket(connection, server_side=True)
            except (OSError, ssl.SSLError) as error:
                self.state.request_log.append({"error": str(error)})
                connection.close()
                raise
        return connection

    def _transfer_listing(self, argument: str) -> None:
        connection = self._data_connection()
        if connection is None:
            return
        prefix = self._path(argument or self.cwd).rstrip("/") + "/"
        with self.state.lock:
            names = [path[len(prefix) :] for path in sorted(self.state.files) if path.startswith(prefix) and "/" not in path[len(prefix) :]]
        self.send_response(150, "opening data")
        connection = self._secure_data_connection(connection)
        connection.sendall(("\r\n".join(names) + "\r\n").encode("utf-8"))
        connection.close()
        self.send_response(226, "transfer complete")

    def _store(self, argument: str) -> None:
        connection = self._data_connection()
        if connection is None:
            return
        path = self._path(argument)
        self.send_response(150, "opening data")
        connection = self._secure_data_connection(connection)
        data = bytearray()
        while chunk := connection.recv(1024 * 1024):
            data.extend(chunk)
        connection.close()
        if self.state.fail_next == "STOR":
            self.state.fail_next = None
            self.send_response(451, "injected failure")
            return
        with self.state.lock:
            self.state.files[path] = bytes(data)
        self.send_response(226, "transfer complete")

    def _retrieve(self, argument: str) -> None:
        path = self._path(argument)
        with self.state.lock:
            data = self.state.files.get(path)
        if data is None:
            self.send_response(550, "file unavailable")
            return
        connection = self._data_connection()
        if connection is None:
            return
        self.send_response(150, "opening data")
        connection = self._secure_data_connection(connection)
        connection.sendall(data)
        connection.close()
        self.send_response(226, "transfer complete")


class _FtpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True

    def __init__(self, address, state, tls_context):
        self.state = state
        self.tls_context = tls_context
        super().__init__(address, _FtpHandler)


class LocalFtpServer:
    """Loopback FTP/explicit-FTPS server for wire contract tests."""

    def __init__(self, root: Path, tls: bool = False, username: str = "", password: str = "") -> None:
        context = None
        if tls:
            certificate = create_certificate(root)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate.certfile, certificate.keyfile)
            self.certificate = certificate
        else:
            self.certificate = None
        self.state = _FtpState(username, password)
        self.server = _FtpServer(("127.0.0.1", 0), self.state, context)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)


class _DavState:
    def __init__(self, username: str, password: str) -> None:
        self.lock = threading.Lock()
        self.files: dict[str, bytes] = {}
        self.directories = {"/"}
        self.request_log: list[dict[str, str | int]] = []
        self.fail_next: str | None = None
        self.username = username
        self.password = password


class _DavHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> _DavState:
        return getattr(self.server, "state")

    def log_message(self, *_args) -> None:
        return

    def _path(self) -> str:
        return posixpath.normpath(unquote(urlparse(self.path).path)) or "/"

    def _authorized(self, body: bytes = b"") -> bool:
        if not self.state.username:
            return True
        value = self.headers.get("Authorization", "")
        if not value.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(value[6:], validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        return decoded == f"{self.state.username}:{self.state.password}"

    def _reject_unauthorized(self) -> bool:
        if self._authorized():
            return False
        self._reply(401, headers={"WWW-Authenticate": 'Basic realm="local"'})
        return True

    def _reply(self, status: int, data: bytes = b"", content_type: str = "", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        if content_type:
            self.send_header("Content-Type", content_type)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.state.request_log.append(
            {
                "method": self.command,
                "path": self._path(),
                "status": status,
                "bytes": len(data),
            }
        )
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)

    def do_MKCOL(self) -> None:
        if self._reject_unauthorized():
            return
        path = self._path()
        with self.state.lock:
            if path in self.state.directories:
                self._reply(405)
                return
            self.state.directories.add(path)
        self._reply(201)

    def do_PUT(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length)
        if self._reject_unauthorized():
            return
        if self.state.fail_next in {"PUT", "PUT_PERMANENT"}:
            if self.state.fail_next == "PUT":
                self.state.fail_next = None
            self._reply(503)
            return
        with self.state.lock:
            self.state.files[self._path()] = data
        self._reply(201)

    def do_GET(self) -> None:
        if self._reject_unauthorized():
            return
        with self.state.lock:
            data = self.state.files.get(self._path())
        self._reply(200, data) if data is not None else self._reply(404)

    def do_HEAD(self) -> None:
        if self._reject_unauthorized():
            return
        with self.state.lock:
            data = self.state.files.get(self._path())
        self._reply(200, data or b"") if data is not None else self._reply(404)

    def do_DELETE(self) -> None:
        if self._reject_unauthorized():
            return
        with self.state.lock:
            removed = self.state.files.pop(self._path(), None)
        self._reply(204 if removed is not None else 404)

    def do_PROPFIND(self) -> None:
        if self._reject_unauthorized():
            return
        path = self._path().rstrip("/") or "/"
        with self.state.lock:
            exists = path in self.state.directories or path in self.state.files
            children = [
                child for child in sorted((*self.state.directories, *self.state.files))
                if child.startswith(path.rstrip("/") + "/") and "/" not in child[len(path.rstrip("/") + "/") :]
            ]
        if not exists:
            self._reply(404)
            return
        hrefs = [path, *children] if self.headers.get("Depth") == "1" else [path]
        body = "".join(
            f'<D:response><D:href>{quote(href, safe="/%")}</D:href></D:response>'
            for href in hrefs
        )
        xml = (
            '<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:">'
            + body
            + "</D:multistatus>"
        ).encode("utf-8")
        self._reply(207, xml, "application/xml")


class _DavServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, address, state):
        self.state = state
        super().__init__(address, _DavHandler)


class LocalWebDavServer:
    """Loopback WebDAV server supporting the exact backend verbs."""

    def __init__(
        self,
        root: Path,
        https: bool = False,
        username: str = "",
        password: str = "",
    ) -> None:
        self.state = _DavState(username, password)
        self.server = _DavServer(("127.0.0.1", 0), self.state)
        self.certificate = None
        if https:
            self.certificate = create_certificate(root)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(self.certificate.certfile, self.certificate.keyfile)
            self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.scheme = "https" if https else "http"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)


class _S3State:
    def __init__(self, access_key: str, secret_key: str) -> None:
        self.lock = threading.Lock()
        self.files: dict[tuple[str, str], bytes] = {}
        self.uploads: dict[str, tuple[str, str, dict[int, bytes]]] = {}
        self.request_log: list[dict[str, str | int]] = []
        self.fail_next: str | None = None
        self.access_key = access_key
        self.secret_key = secret_key


class _S3Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    _SIGV2_SUBRESOURCES = frozenset(
        {
            "accelerate",
            "acl",
            "cors",
            "defaultObjectAcl",
            "delete",
            "location",
            "logging",
            "object-lock",
            "partNumber",
            "policy",
            "replication",
            "partNumber",
            "requestPayment",
            "response-cache-control",
            "response-content-disposition",
            "response-content-encoding",
            "response-content-language",
            "response-content-type",
            "response-expires",
            "restore",
            "select",
            "select-type",
            "storageClass",
            "tagging",
            "torrent",
            "uploadId",
            "uploads",
            "versionId",
            "versioning",
            "versions",
            "website",
            "analytics",
            "metrics",
            "inventory",
            "notification",
            "encryption",
            "lifecycle",
        }
    )

    @property
    def state(self) -> _S3State:
        return getattr(self.server, "state")

    def log_message(self, *_args) -> None:
        return

    def _bucket_key(self) -> tuple[str, str]:
        parsed = urlparse(self.path)
        path = unquote(parsed.path).strip("/")
        host = self.headers.get("Host", "").split(":", 1)[0]
        virtual_bucket = (
            host.split(".", 1)[0]
            if "." in host and host not in {"127.0.0.1", "localhost"}
            else ""
        )
        if virtual_bucket:
            return virtual_bucket, path
        bucket, _, key = path.partition("/")
        return bucket, key

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", "0")))

    def _authorized(self, body: bytes = b"") -> bool:
        if not self.state.access_key:
            return True
        authorization = self.headers.get("Authorization", "")
        if authorization.startswith("AWS "):
            credential = authorization[4:]
            access, separator, signature = credential.partition(":")
            if not separator or access != self.state.access_key:
                return False
            parsed = urlparse(self.path)
            host = self.headers.get("Host", "").split(":", 1)[0]
            virtual_bucket = (
                host.split(".", 1)[0]
                if "." in host and host not in {"127.0.0.1", "localhost"}
                else ""
            )
            canonical_resource = parsed.path or "/"
            if virtual_bucket:
                canonical_resource = f"/{virtual_bucket}{canonical_resource}"
            from botocore.auth import HmacV1Auth
            from botocore.credentials import Credentials

            headers = Message()
            for name, value in self.headers.items():
                headers[name] = value
            signer = HmacV1Auth(
                Credentials(self.state.access_key, self.state.secret_key)
            )
            split = urlsplit("http://" + host + self.path)
            standard = "\n".join(
                (
                    headers.get("Content-MD5", ""),
                    headers.get("Content-Type", ""),
                    headers.get("Date", ""),
                )
            )
            custom = signer.canonical_custom_headers(headers)
            prefix = self.command.upper() + "\n" + standard + "\n"
            if custom:
                prefix += custom + "\n"
            resources = [
                signer.canonical_resource(
                    split, auth_path=canonical_resource if virtual_bucket else None
                )
            ]
            if self.command.upper() == "GET" and parsed.path.rstrip("/").count("/") == 1:
                resources.append(resources[0] + "/")
            if parsed.query:
                resources.append(
                    (canonical_resource if virtual_bucket else parsed.path)
                    + "?"
                    + unquote(parsed.query)
                )
            signatures_match = any(
                hmac.compare_digest(signer.sign_string(prefix + resource), signature)
                for resource in resources
            )
            if signatures_match:
                return True
            signed_headers = Message()
            for name, value in self.headers.items():
                signed_headers[name] = value
            return hmac.compare_digest(
                signer.get_signature(
                    self.command,
                    split,
                    signed_headers,
                    auth_path=canonical_resource if virtual_bucket else None,
                ),
                signature,
            )
        if authorization.startswith("AWS4-HMAC-SHA256"):
            marker = "Credential="
            start = authorization.find(marker)
            if start < 0:
                return False
            credential = authorization[start + len(marker) :].split(",", 1)[0]
            access, _, scope = credential.partition("/")
            if access != self.state.access_key:
                return False
            signed_marker = "SignedHeaders="
            signature_marker = "Signature="
            signed_start = authorization.find(signed_marker)
            signature_start = authorization.find(signature_marker)
            if signed_start < 0 or signature_start < 0:
                return False
            signed_headers = authorization[signed_start + len(signed_marker) :].split(",", 1)[0]
            signature = authorization[signature_start + len(signature_marker) :].split(",", 1)[0]
            canonical_headers = "".join(
                f"{name}:{' '.join(self.headers.get(name, '').strip().split())}\n"
                for name in signed_headers.split(";")
            )
            parsed = urlparse(self.path)
            canonical_query = "&".join(
                f"{quote(key, safe='-_.~')}={quote(value, safe='-_.~')}"
                for key, value in sorted(parse_qs(parsed.query, keep_blank_values=True).items())
                for value in value
            )
            canonical_uri = quote(unquote(parsed.path or "/"), safe="/-_.~")
            payload_hash = self.headers.get(
                "X-Amz-Content-Sha256", hashlib.sha256(body).hexdigest()
            )
            canonical_request = "\n".join(
                (
                    self.command,
                    canonical_uri,
                    canonical_query,
                    canonical_headers,
                    signed_headers,
                    payload_hash,
                )
            )
            amz_date = self.headers.get("X-Amz-Date", "")
            string_to_sign = "\n".join(
                (
                    "AWS4-HMAC-SHA256",
                    amz_date,
                    scope,
                    hashlib.sha256(canonical_request.encode()).hexdigest(),
                )
            )
            scope_parts = scope.split("/")
            date, region, service_name = scope_parts[:3]
            signing = hmac.new(
                ("AWS4" + self.state.secret_key).encode(), date.encode(), hashlib.sha256
            ).digest()
            signing = hmac.new(signing, region.encode(), hashlib.sha256).digest()
            signing = hmac.new(signing, service_name.encode(), hashlib.sha256).digest()
            signing = hmac.new(signing, b"aws4_request", hashlib.sha256).digest()
            expected = hmac.new(signing, string_to_sign.encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected, signature)
        return False

    def _reject_unauthorized(self, body: bytes = b"") -> bool:
        if self._authorized(body):
            return False
        self._reply(403, b"forbidden")
        return True

    def _reply(self, status: int, data: bytes = b"", content_type: str = "", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        if content_type:
            self.send_header("Content-Type", content_type)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        parsed = urlparse(self.path)
        auth = self.headers.get("Authorization", "")
        signature = "sigv4" if auth.startswith("AWS4-HMAC-SHA256") else "sigv2" if auth.startswith("AWS ") else "none"
        self.state.request_log.append({"method": self.command, "path": parsed.path, "query": parsed.query, "status": status, "signature": signature, "bytes": len(data)})
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)

    def do_HEAD(self) -> None:
        if self._reject_unauthorized():
            return
        bucket, key = self._bucket_key()
        with self.state.lock:
            exists = (bucket, key) in self.state.files
        self._reply(200 if exists else 404)

    def do_GET(self) -> None:
        if self._reject_unauthorized():
            return
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if query.get("list-type") == ["2"]:
            self._list_objects(query)
            return
        bucket, key = self._bucket_key()
        with self.state.lock:
            data = self.state.files.get((bucket, key))
        self._reply(200, data) if data is not None else self._reply(404)

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        body = self._body()
        if self._reject_unauthorized(body):
            return
        if self.state.fail_next in {"PUT", "PUT_PERMANENT"}:
            if self.state.fail_next == "PUT":
                self.state.fail_next = None
            self._reply(503)
            return
        bucket, key = self._bucket_key()
        if "uploadId" in query:
            upload_id = query["uploadId"][0]
            part_number = int(query.get("partNumber", ["1"])[0])
            with self.state.lock:
                upload = self.state.uploads[upload_id]
                upload[2][part_number] = body
            self._reply(200, b"", "", {"ETag": f'"{hashlib.md5(body).hexdigest()}"'})
            return
        with self.state.lock:
            self.state.files[(bucket, key)] = body
        self._reply(200, b"", "", {"ETag": f'"{hashlib.md5(body).hexdigest()}"'})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        bucket, key = self._bucket_key()
        if self._reject_unauthorized():
            return
        if "uploads" in query:
            upload_id = uuid.uuid4().hex
            with self.state.lock:
                self.state.uploads[upload_id] = (bucket, key, {})
            body = f"<InitiateMultipartUploadResult><UploadId>{upload_id}</UploadId></InitiateMultipartUploadResult>".encode()
            self._reply(200, body, "application/xml")
            return
        if "uploadId" in query:
            upload_id = query["uploadId"][0]
            self._body()
            with self.state.lock:
                upload = self.state.uploads.pop(upload_id)
                self.state.files[(upload[0], upload[1])] = b"".join(upload[2][n] for n in sorted(upload[2]))
            self._reply(200, b"<CompleteMultipartUploadResult/>", "application/xml")
            return
        self._reply(400)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if self._reject_unauthorized():
            return
        if "uploadId" in parse_qs(parsed.query, keep_blank_values=True):
            self._reply(204)
            return
        bucket, key = self._bucket_key()
        with self.state.lock:
            removed = self.state.files.pop((bucket, key), None)
        self._reply(204 if removed is not None else 404)

    def _list_objects(self, query: dict[str, list[str]]) -> None:
        bucket, _ = self._bucket_key()
        prefix = query.get("prefix", [""])[0]
        token = query.get("continuation-token", [""])[0]
        with self.state.lock:
            keys = sorted(key for current_bucket, key in self.state.files if current_bucket == bucket and key.startswith(prefix))
        start = int(token) if token.isdigit() else 0
        selected = keys[start : start + 2]
        truncated = start + len(selected) < len(keys)
        body = [
            '<?xml version="1.0" encoding="UTF-8"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
            f"<Name>{bucket}</Name><Prefix>{prefix}</Prefix>",
            f"<KeyCount>{len(selected)}</KeyCount><MaxKeys>2</MaxKeys>",
        ]
        for key in selected:
            body.append(f"<Contents><Key>{key}</Key><ETag>\"{hashlib.md5(key.encode()).hexdigest()}\"</ETag></Contents>")
        body.append(f"<IsTruncated>{str(truncated).lower()}</IsTruncated>")
        if truncated:
            body.append(f"<NextContinuationToken>{start + len(selected)}</NextContinuationToken>")
        body.append("</ListBucketResult>")
        self._reply(200, "".join(body).encode(), "application/xml")


class _S3Server(http.server.ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, address, state):
        self.state = state
        super().__init__(address, _S3Handler)


class LocalS3Server:
    """Loopback S3 HTTP server recording signature, pagination, and multipart wire use."""

    def __init__(self, access_key: str = "", secret_key: str = "") -> None:
        self.state = _S3State(access_key, secret_key)
        self.server = _S3Server(("127.0.0.1", 0), self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
