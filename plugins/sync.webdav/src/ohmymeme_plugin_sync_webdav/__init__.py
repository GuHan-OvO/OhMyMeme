import base64
import logging
import os
import shutil
import ssl
import threading
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import quote, unquote, urlparse

from ohmymeme.core.plugins.network_config import SyncError, validate_sync_config

logger = logging.getLogger(__name__)
_dav_dirs = set()
_dav_dirs_lock = threading.Lock()


def clear_directory_cache():
    with _dav_dirs_lock:
        _dav_dirs.clear()


def quote_path(path):
    # Encode path segments without encoding their separators.
    return "/".join(quote(part, safe="") for part in path.split("/"))


class WebDAVBackend:
    def __init__(self, config, secrets):
        # Configuration is a narrow immutable record, never the host Config.
        self.config = validate_sync_config("sync.webdav", config)
        self.secrets = secrets
        self.base_url = ""
        self.auth_header = ""
        self.timeout = config.timeout
        self.ssl_context = None

    def connect(self):
        # Preserve encoded URL segments and per-connection CA verification.
        parsed = urlparse(self.config.url)
        self.base_url = "%s://%s%s" % (
            parsed.scheme,
            parsed.netloc,
            quote(parsed.path, safe="/%").rstrip("/"),
        )
        self.ssl_context = None
        if parsed.scheme == "https" and self.config.ca_file:
            self.ssl_context = ssl.create_default_context(cafile=self.config.ca_file)
        self.auth_header = ""
        if self.config.user:
            password = self.secrets.get("password") or ""
            token = base64.b64encode(
                ("%s:%s" % (self.config.user, password)).encode("utf-8")
            ).decode("ascii")
            self.auth_header = "Basic %s" % token

    def _url(self, remote_path):
        # Remote paths retain the old per-segment escaping rules.
        encoded = quote_path(remote_path.lstrip("/"))
        return self.base_url.rstrip("/") + ("/" + encoded if encoded else "")

    def _request(self, method, url, data=None, headers=None):
        # Requests have only the provider's scoped authentication.
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("User-Agent", "OhMyMeme")
        if self.auth_header:
            request.add_header("Authorization", self.auth_header)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        if self.ssl_context is None:
            return urllib.request.urlopen(request, timeout=self.timeout)
        return urllib.request.urlopen(
            request, timeout=self.timeout, context=self.ssl_context
        )

    def clear_directory_cache(self):
        clear_directory_cache()

    def ensure_remote_dir(self, path):
        # Cache checks and MKCOL share one lock so concurrent workers issue it once.
        relative = ""
        for part in filter(None, path.strip("/").split("/")):
            relative += "/" + part
            url = self._url(relative)
            with _dav_dirs_lock:
                if url in _dav_dirs:
                    continue
                try:
                    with self._request("MKCOL", url):
                        pass
                    _dav_dirs.add(url)
                    continue
                except urllib.error.HTTPError as error:
                    if error.code == 405:
                        _dav_dirs.add(url)
                        continue
                    if 300 <= error.code < 400 and self.file_exists(relative):
                        _dav_dirs.add(url)
                        continue
                    raise SyncError(
                        "MKCOL %s 失败: HTTP %d" % (url, error.code)
                    ) from error
                except Exception as error:
                    raise SyncError("MKCOL %s 失败: %s" % (url, error)) from error
        return True

    def upload_file(self, local_path, remote_path):
        # Stream a host-owned staging file; do not interpret media or metadata.
        try:
            size = local_path.stat().st_size
            with open(local_path, "rb") as stream:
                with self._request(
                    "PUT",
                    self._url(remote_path),
                    data=stream,
                    headers={
                        "Content-Length": str(size),
                        "Content-Type": "application/octet-stream",
                    },
                ) as response:
                    return 200 <= response.status < 300
        except Exception:
            logger.warning("upload failed %s", remote_path)
            return False

    def download_file(self, remote_path, local_path):
        # Failed downloads never replace the destination staging file.
        temporary = local_path.with_suffix(local_path.suffix + ".tmp")
        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            with self._request("GET", self._url(remote_path)) as response:
                with temporary.open("wb") as stream:
                    shutil.copyfileobj(response, stream, length=1024 * 1024)
            os.replace(temporary, local_path)
            return True
        except Exception:
            temporary.unlink(missing_ok=True)
            logger.warning("download failed %s", remote_path)
            return False

    def file_exists(self, path):
        # Only unsupported PROPFIND can fall back to HEAD on the same provider.
        try:
            with self._request(
                "PROPFIND", self._url(path), headers={"Depth": "0"}
            ) as r:
                return r.status in (200, 207)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return False
            if error.code in (405, 501):
                try:
                    with self._request("HEAD", self._url(path)) as response:
                        return response.status in (200, 204)
                except urllib.error.HTTPError as fallback:
                    if fallback.code == 404:
                        return False
                    raise SyncError(
                        "WebDAV HEAD fallback failed: HTTP %d" % fallback.code
                    ) from fallback
                except Exception as fallback:
                    raise SyncError(
                        "WebDAV HEAD fallback failed: %s" % fallback
                    ) from fallback
            raise SyncError("WebDAV PROPFIND failed: HTTP %d" % error.code) from error
        except Exception as error:
            raise SyncError("WebDAV file_exists failed: %s" % error) from error

    def test_connection(self):
        # Keep the existing permission/network probe and its public diagnostics.
        url = self._url(self.config.path)
        try:
            with self._request("PROPFIND", url, headers={"Depth": "0"}) as response:
                if response.status not in (200, 207):
                    raise SyncError(
                        "WebDAV PROPFIND returned HTTP %d" % response.status
                    )
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise SyncError(
                    "WebDAV 目录不存在（HTTP 404），首次上传将自动创建"
                ) from error
            if error.code in (401, 403):
                raise SyncError("WebDAV 鉴权失败（HTTP %d）" % error.code) from error
            if error.code in (405, 501):
                raise SyncError(
                    "WebDAV 服务器不支持 PROPFIND（HTTP %d）" % error.code
                ) from error
            raise SyncError("WebDAV 连接测试失败: HTTP %d" % error.code) from error
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            raise SyncError("WebDAV 网络不可达: %s" % error) from error

    def delete_file(self, path):
        # WebDAV 404 is not a confirmed removal and remains false.
        try:
            with self._request("DELETE", self._url(path)) as response:
                return response.status in (200, 204)
        except Exception:
            return False

    def list_files(self, path):
        # Parse the original Depth-1 DAV representation, preserving error results.
        url = self._url(path)
        try:
            with self._request("PROPFIND", url, headers={"Depth": "1"}) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            if error.code in (405, 501):
                raise SyncError(
                    "WebDAV 服务器不支持 PROPFIND（HTTP %d）" % error.code
                ) from error
            raise SyncError("WebDAV list_files failed: HTTP %d" % error.code) from error
        except Exception as error:
            raise SyncError("WebDAV list_files failed: %s" % error) from error
        requested = urlparse(url).path.rstrip("/") or "/"
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as error:
            raise SyncError("WebDAV PROPFIND 响应不是合法 XML: %s" % error) from error
        files = []
        for response in root.findall("{DAV:}response"):
            href_element = response.find("{DAV:}href")
            if href_element is None or href_element.text is None:
                continue
            href = href_element.text.strip()
            if href.endswith("/"):
                continue
            href_path = urlparse(href).path if "://" in href else href
            href_path = href_path.rstrip("/") or "/"
            if href_path == requested:
                continue
            prefix = requested.rstrip("/") + "/"
            name = (
                href_path[len(prefix) :]
                if href_path.startswith(prefix)
                else href_path.split("/")[-1]
            )
            name = unquote(name)
            if name:
                files.append(name)
        return files

    def close(self):
        # Drop derived authentication as soon as the operation closes.
        self.auth_header = ""


class Plugin:
    provider_id = "sync.webdav"
    api_version = 1

    def start(self, context):
        # The factory has no network resources until a host operation requests them.
        pass

    def stop(self):
        # Per-operation adapters revoke their own authentication.
        pass

    def create_backend(self, config, secrets):
        # Return an independent connection/authentication state.
        return WebDAVBackend(config, secrets)


def create_plugin():
    # No host network implementation is imported by the factory.
    return Plugin()
