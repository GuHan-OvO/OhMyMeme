"""FetchPolicy 的直接 socket 与 TLS transport。"""

import http.client
import socket
from collections.abc import Mapping, Sequence

from .fetch_policy import FetchTarget, ResolvedAddress, ResponsePort


class _DefaultResolver:
    def resolve(self, hostname: str, port: int) -> Sequence[ResolvedAddress]:
        infos = socket.getaddrinfo(
            hostname,
            port,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
        )
        addresses: list[ResolvedAddress] = []
        seen: set[tuple[int, tuple[str, int] | tuple[str, int, int, int]]] = set()
        for family, _kind, _proto, _canonname, sockaddr in infos:
            if family not in (socket.AF_INET, socket.AF_INET6):
                continue
            normalized = (family, sockaddr)
            if normalized in seen:
                continue
            seen.add(normalized)
            addresses.append(ResolvedAddress(family, sockaddr, str(sockaddr[0])))
        return addresses


class _HTTPResponse:
    def __init__(
        self,
        response: http.client.HTTPResponse,
        connection: http.client.HTTPConnection,
    ):
        self._response = response
        self._connection = connection
        self.status = response.status
        self.headers = dict(response.headers.items())

    def read(self, size: int = -1) -> bytes:
        return self._response.read(size)

    def close(self) -> None:
        self._response.close()
        self._connection.close()


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, target: FetchTarget, address: ResolvedAddress, timeout: float):
        super().__init__(target.hostname, target.port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        sock = socket.socket(
            self._address.family, socket.SOCK_STREAM, socket.IPPROTO_TCP
        )
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._address.sockaddr)
        except (OSError, TimeoutError):
            sock.close()
            raise
        self.sock = sock


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, target: FetchTarget, address: ResolvedAddress, timeout: float):
        super().__init__(target.hostname, target.port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        sock = socket.socket(
            self._address.family, socket.SOCK_STREAM, socket.IPPROTO_TCP
        )
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._address.sockaddr)
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except (OSError, TimeoutError):
            sock.close()
            raise


class _DirectConnector:
    def open(
        self,
        target: FetchTarget,
        address: ResolvedAddress,
        timeout: float,
        headers: Mapping[str, str],
    ) -> ResponsePort:
        connection: http.client.HTTPConnection
        if target.scheme == "https":
            connection = _PinnedHTTPSConnection(target, address, timeout)
        else:
            connection = _PinnedHTTPConnection(target, address, timeout)
        request_headers = dict(headers)
        request_headers["Host"] = target.authority
        connection.request("GET", target.request_target, headers=request_headers)
        return _HTTPResponse(connection.getresponse(), connection)
