from collections import namedtuple
from urllib.parse import urlparse


class SyncError(Exception):
    pass


FtpConfig = namedtuple(
    "FtpConfig",
    "host port user path tls tls_ca tls_protect_data",
    defaults=("", 21, "", "/", False, "", True),
)
S3Config = namedtuple(
    "S3Config",
    "endpoint region bucket path addressing_style signature_version "
    "multipart_threshold multipart_part_size",
    defaults=("", "", "", "", "virtual", "s3", 8388608, 5242880),
)
R2Config = namedtuple(
    "R2Config",
    "account_id bucket path endpoint region addressing_style "
    "multipart_threshold multipart_part_size",
    defaults=("", "", "", "", "auto", "virtual", 8388608, 5242880),
)
WebDAVConfig = namedtuple(
    "WebDAVConfig",
    "url user path timeout ca_file",
    defaults=("", "", "", 30, ""),
)
LanTransportConfig = namedtuple(
    "LanTransportConfig", "host port", defaults=("0.0.0.0", 17852)
)
SYNC_CONFIGS = {
    "sync.ftp": FtpConfig,
    "sync.s3": S3Config,
    "sync.r2": R2Config,
    "sync.webdav": WebDAVConfig,
}
SYNC_SECRETS = {
    "sync.ftp": ("password",),
    "sync.s3": ("access_key", "secret_key"),
    "sync.r2": ("access_key_id", "secret_access_key"),
    "sync.webdav": ("password",),
}


def validate_sync_config(provider_id, config):
    # Validate the same immutable record both before loading and inside factories.
    record = SYNC_CONFIGS[provider_id]
    kind = provider_id.split(".")[1]
    if type(config) is not record:
        raise SyncError(f"{provider_id}.config: expected {record.__name__}")
    for key, default in record._field_defaults.items():
        value = getattr(config, key)
        field = f"{kind}_{key}"
        if type(value) is not type(default):
            raise SyncError(f"{field}: expected {type(default).__name__}")
        if isinstance(value, str) and any(ord(c) < 32 for c in value):
            raise SyncError(f"{field}: invalid control character")
        if type(value) is int and (value <= 0 or (key == "port" and value > 65535)):
            raise SyncError(f"{field}: out of range")
        if key == "addressing_style" and value not in ("virtual", "path"):
            raise SyncError(f"{field}: expected virtual or path")
        if key == "signature_version" and value not in ("s3", "s3v4"):
            raise SyncError(f"{field}: expected s3 or s3v4")
        if key in ("url", "endpoint") and value:
            try:
                parsed = urlparse(value)
                valid = (
                    parsed.scheme in ("http", "https")
                    and parsed.hostname
                    and not parsed.username
                    and not parsed.password
                    and not parsed.query
                    and not parsed.fragment
                    and (parsed.port is None or 0 < parsed.port <= 65535)
                )
            except ValueError:
                valid = False
            if not valid:
                raise SyncError(f"{field}: invalid HTTP(S) endpoint")
    required = {
        "sync.ftp": ("host",),
        "sync.s3": ("endpoint", "bucket"),
        "sync.r2": ("account_id", "bucket"),
        "sync.webdav": ("url",),
    }
    for field in required[provider_id]:
        if not getattr(config, field).strip():
            raise SyncError(f"{kind}_{field}: not configured")
    if provider_id == "sync.r2" and not all(
        c.isascii() and (c.isalnum() or c in "_-") for c in config.account_id
    ):
        raise SyncError("r2_account_id: invalid account ID")
    return config


def validate_lan_config(config):
    # Transport receives no security, approval, command, or persistent state.
    if type(config) is not LanTransportConfig:
        raise ValueError("transport.lan.config: expected LanTransportConfig")
    if config.host not in ("0.0.0.0", "127.0.0.1"):
        raise ValueError("lan_host: expected IPv4 bind address")
    if type(config.port) is not int or not 0 <= config.port <= 65535:
        raise ValueError("lan_port: expected integer in 0..65535")
    return config
