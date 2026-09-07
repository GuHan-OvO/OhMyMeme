"""统一外部 HTTP 抓取准入、解析、连接 pinning 和内容边界。"""

import hashlib
import os
import socket
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

MAX_FETCH_BYTES = 20 * 1024 * 1024
MAX_URL_LENGTH = 8192
MAX_REDIRECTS = 5
CONNECT_TIMEOUT = 10.0


class FetchError(RuntimeError):
    """外部抓取失败的基类。"""


class FetchRejected(FetchError):
    """请求、地址或内容违反安全策略。"""


class FetchLimitError(FetchRejected):
    """响应超过统一大小边界。"""


class FetchTimeout(FetchError):
    """连接或读取超过总时限。"""


@dataclass(frozen=True, slots=True)
class ResolvedAddress:
    """经过地址解析并可用于直接连接的 socket 地址。"""

    family: int
    sockaddr: tuple[str, int] | tuple[str, int, int, int]
    ip: str


@dataclass(frozen=True, slots=True)
class FetchTarget:
    """已解析且未携带用户凭据的 HTTP 目标。"""

    scheme: str
    hostname: str
    port: int
    authority: str
    request_target: str


class ResponsePort(Protocol):
    status: int
    headers: Mapping[str, str]

    def read(self, size: int = -1) -> bytes: ...

    def close(self) -> None: ...


class ResolverPort(Protocol):
    def resolve(self, hostname: str, port: int) -> Sequence[ResolvedAddress | str]: ...


class ConnectorPort(Protocol):
    def open(
        self,
        target: FetchTarget,
        address: ResolvedAddress,
        timeout: float,
        headers: Mapping[str, str],
    ) -> ResponsePort: ...


class RedirectPort(Protocol):
    def next_url(self, current_url: str, response: ResponsePort) -> str | None: ...


class FetchPolicy:
    """对外部 HTTP 请求执行统一的 DNS、连接、重定向和内容准入。"""

    def __init__(
        self,
        resolver: ResolverPort | None = None,
        connector: ConnectorPort | None = None,
        redirect: RedirectPort | None = None,
        total_timeout: float = 30.0,
    ) -> None:
        self._resolver = resolver or _DefaultResolver()
        self._connector = connector or _DirectConnector()
        self._redirect = redirect or _DefaultRedirect()
        self._total_timeout = total_timeout

    def prepare(
        self, url: str, trusted_hosts: Collection[str] = ()
    ) -> tuple[FetchTarget, ResolvedAddress]:
        target = _parse_target(url, trusted_hosts)
        return target, select_address(self._resolver, target)

    def resolve_for_curl(self, url: str, trusted_hosts: Collection[str] = ()) -> str:
        target, address = self.prepare(url, trusted_hosts)
        return f"{target.hostname}:{target.port}:{address.ip}"

    def fetch(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        trusted_hosts: Collection[str] = (),
    ) -> ResponsePort:
        deadline = time.monotonic() + self._total_timeout
        return self._fetch_with_deadline(
            url, headers=headers, trusted_hosts=trusted_hosts, deadline=deadline
        )

    def _fetch_with_deadline(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None,
        trusted_hosts: Collection[str],
        deadline: float,
    ) -> ResponsePort:
        """从既定总时限开始执行解析、连接和重定向。"""
        current = url
        try:
            original_scheme = urlsplit(url).scheme.lower()
        except ValueError:
            original_scheme = ""
        for redirect_count in range(MAX_REDIRECTS + 1):
            target, address = self.prepare(current, trusted_hosts)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FetchTimeout("fetch total timeout")
            try:
                with proxy_bypass():
                    response = self._connector.open(
                        target,
                        address,
                        min(CONNECT_TIMEOUT, remaining),
                        {**(headers or {}), "Host": target.authority},
                    )
            except (socket.timeout, TimeoutError) as error:
                raise FetchTimeout("fetch connection timeout") from error
            try:
                next_url = self._redirect.next_url(current, response)
            except FetchError:
                response.close()
                raise
            if next_url is None:
                if response.status >= 400:
                    response.close()
                    raise FetchError(f"http status {response.status}")
                return response
            response.close()
            try:
                next_scheme = urlsplit(next_url).scheme.lower()
            except ValueError as error:
                raise FetchRejected("malformed redirect") from error
            if original_scheme == "https" and next_scheme != "https":
                raise FetchRejected("https downgrade redirect rejected")
            if redirect_count == MAX_REDIRECTS:
                raise FetchRejected("too many redirects")
            current = next_url
        raise FetchRejected("redirect loop")

    def fetch_bytes(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        trusted_hosts: Collection[str] = (),
        image: bool = False,
        expected_magic: bytes | tuple[bytes, ...] | None = None,
        max_bytes: int = MAX_FETCH_BYTES,
        expected_sha256: str | None = None,
    ) -> bytes:
        deadline = time.monotonic() + self._total_timeout
        response = self._fetch_with_deadline(
            url,
            headers=headers,
            trusted_hosts=trusted_hosts,
            deadline=deadline,
        )
        try:
            declared = _header(response.headers, "content-length")
            if declared.isdigit() and int(declared) > max_bytes:
                raise FetchLimitError("response exceeds size limit")
            chunks: list[bytes] = []
            total = 0
            while True:
                if time.monotonic() >= deadline:
                    raise FetchTimeout("fetch total timeout")
                chunk = response.read(min(65536, max_bytes - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise FetchLimitError("response exceeds size limit")
                chunks.append(chunk)
            data = b"".join(chunks)
        except (socket.timeout, TimeoutError) as error:
            raise FetchTimeout("fetch read timeout") from error
        finally:
            response.close()
        return self.validate_bytes(
            data,
            image=image,
            expected_magic=expected_magic,
            max_bytes=max_bytes,
            expected_sha256=expected_sha256,
        )

    def validate_bytes(
        self,
        data: bytes,
        *,
        image: bool = False,
        expected_magic: bytes | tuple[bytes, ...] | None = None,
        max_bytes: int = MAX_FETCH_BYTES,
        expected_sha256: str | None = None,
    ) -> bytes:
        """校验已有字节的大小、魔数及可选图片内容。"""
        if len(data) > max_bytes:
            raise FetchLimitError("response exceeds size limit")
        if expected_magic is not None:
            signatures = (
                (expected_magic,)
                if isinstance(expected_magic, bytes)
                else expected_magic
            )
            if not any(data.startswith(signature) for signature in signatures):
                raise FetchRejected("response magic rejected")
        if expected_sha256 is not None:
            if len(expected_sha256) != 64 or any(
                character not in "0123456789abcdef" for character in expected_sha256
            ):
                raise FetchRejected("expected hash rejected")
            if hashlib.sha256(data).hexdigest() != expected_sha256:
                raise FetchRejected("response hash rejected")
        if image:
            validate_image_bytes(data)
        return data

    def probe(self, url: str) -> None:
        """建立一次受策略保护的连接，用于网络连通性探测。"""
        response = self.fetch(url)
        response.close()

    def download_to(
        self,
        url: str,
        destination: Path,
        *,
        headers: Mapping[str, str] | None = None,
        trusted_hosts: Collection[str] = (),
        expected_magic: bytes | tuple[bytes, ...] | None = None,
        progress: Callable[[int, int], None] | None = None,
        expected_sha256: str | None = None,
    ) -> Path:
        data = self.fetch_bytes(
            url,
            headers=headers,
            trusted_hosts=trusted_hosts,
            expected_magic=expected_magic,
            expected_sha256=expected_sha256,
        )
        temporary = destination.with_name(destination.name + ".fetching")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        if progress is not None:
            progress(len(data), len(data))
        return destination


from .fetch_policy_transport import (  # noqa: E402, F401
    _DefaultResolver,
    _DirectConnector,
    _PinnedHTTPConnection,
    _PinnedHTTPSConnection,
)
from .fetch_policy_validation import (  # noqa: E402
    _DefaultRedirect,
    _header,
    _parse_target,
    proxy_bypass,
    select_address,
    validate_image_bytes,
)
