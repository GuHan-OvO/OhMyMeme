import logging
import ssl
from ftplib import FTP, FTP_TLS, error_perm

from ohmymeme.core.plugins.network_config import SyncError, validate_sync_config

logger = logging.getLogger(__name__)


class FtpBackend:
    def __init__(self, config, secrets):
        # Each connection owns its immutable config and operation secret accessor.
        self.config = validate_sync_config("sync.ftp", config)
        self.secrets = secrets
        self.ftp = None

    def connect(self):
        # FTPS retains certificate verification and protected-data defaults.
        self.close()
        password = self.secrets.get("password") or ""
        ftp = None
        try:
            if self.config.tls:
                context = ssl.create_default_context(cafile=self.config.tls_ca or None)
                ftp = FTP_TLS(context=context)
            else:
                ftp = FTP()
            ftp.connect(self.config.host, self.config.port, timeout=15)
            if self.config.user:
                ftp.login(self.config.user, password)
            else:
                ftp.login()
            ftp.encoding = "utf-8"
            if self.config.tls and self.config.tls_protect_data:
                ftp.prot_p()
            self.ftp = ftp
        except Exception as error:
            if ftp is not None:
                ftp.close()
            raise SyncError("FTP connect failed: %s" % error) from error

    def ensure_remote_dir(self, path):
        # Create remote parents using the existing absolute FTP path convention.
        sofar = ""
        for part in path.strip("/").split("/"):
            if not part:
                continue
            sofar += "/" + part
            try:
                self.ftp.cwd(sofar)
            except error_perm:
                self.ftp.mkd(sofar)
                self.ftp.cwd(sofar)

    def upload_file(self, local_path, remote_path):
        # The host supplies only an operation staging path.
        try:
            with open(local_path, "rb") as stream:
                self.ftp.storbinary("STOR %s" % remote_path, stream)
            return True
        except Exception:
            logger.warning("upload failed %s", remote_path)
            return False

    def download_file(self, remote_path, local_path):
        # Download bytes without interpreting host files or manifests.
        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            with open(local_path, "wb") as stream:
                self.ftp.retrbinary("RETR %s" % remote_path, stream.write)
            return True
        except Exception:
            logger.warning("download failed %s", remote_path)
            return False

    def file_exists(self, path):
        # Preserve the legacy unavailable/missing false result.
        try:
            self.ftp.size(path)
            return True
        except Exception:
            return False

    def delete_file(self, path):
        # FTP 550 remains idempotent success, unlike WebDAV 404.
        try:
            self.ftp.delete(path)
            return True
        except error_perm as error:
            return "550" in str(error)
        except Exception:
            return False

    def list_files(self, path):
        # Listing errors must propagate, not resemble an empty remote.
        names = []
        self.ftp.retrlines("NLST %s" % path, names.append)
        return [n.split("/")[-1] for n in names if n and not n.endswith("/")]

    def test_connection(self):
        # FTP login already performs the legacy connection probe.
        pass

    def close(self):
        # Failed QUIT still closes the control socket.
        ftp, self.ftp = self.ftp, None
        if ftp is not None:
            try:
                ftp.quit()
            except Exception:
                ftp.close()


class Plugin:
    provider_id = "sync.ftp"
    api_version = 1

    def start(self, context):
        # Connections are created per host operation, not at provider activation.
        pass

    def stop(self):
        # The host adapter owns backend close and secret revocation.
        pass

    def create_backend(self, config, secrets):
        # Never share a connection between worker threads.
        return FtpBackend(config, secrets)


def create_plugin():
    # Entry-point factory has no host implementation dependency.
    return Plugin()
