import logging

from ohmymeme.core.plugins.network_config import SyncError, validate_sync_config

logger = logging.getLogger(__name__)


class S3Backend:
    provider_id = "sync.s3"

    def __init__(self, config, secrets):
        # R2 shares only this pure network implementation, not registry selection.
        self.config = validate_sync_config(self.provider_id, config)
        self.secrets = secrets
        self.client = None
        self.bucket = config.bucket
        prefix = config.path.strip("/")
        self.prefix = prefix + "/" if prefix else ""
        self.multipart_threshold = max(5242880, config.multipart_threshold)
        self.multipart_part_size = max(5242880, config.multipart_part_size)

    def connect(self):
        # Keep boto3 lazy and OSS SigV2/addressing behavior unchanged.
        import boto3
        from botocore.config import Config

        self.close()
        access = self.secrets.get("access_key") or ""
        secret = self.secrets.get("secret_key") or ""
        kwargs = {
            "endpoint_url": self.config.endpoint,
            "aws_access_key_id": access,
            "aws_secret_access_key": secret,
        }
        if self.config.region:
            kwargs["region_name"] = self.config.region
        try:
            self.client = boto3.client(
                "s3",
                config=Config(
                    signature_version=self.config.signature_version,
                    s3={
                        "payload_signing_enabled": False,
                        "addressing_style": self.config.addressing_style,
                    },
                ),
                **kwargs,
            )
        except Exception as error:
            raise SyncError("S3 connect failed: %s" % error) from error

    def _key(self, remote_path):
        # Object keys keep the configured prefix exactly once.
        return self.prefix + remote_path.lstrip("/")

    def ensure_remote_dir(self, path):
        # Object stores have no physical directory creation.
        pass

    def upload_file(self, local_path, remote_path):
        # SigV2 put_object avoids OSS chunked encoding incompatibility.
        try:
            key = self._key(remote_path)
            if local_path.stat().st_size < self.multipart_threshold:
                with open(local_path, "rb") as stream:
                    self.client.put_object(
                        Bucket=self.bucket,
                        Key=key,
                        Body=stream.read(),
                        ContentType="application/octet-stream",
                    )
                return True
            upload = self.client.create_multipart_upload(
                Bucket=self.bucket, Key=key, ContentType="application/octet-stream"
            )
            upload_id = upload["UploadId"]
            parts = []
            try:
                with open(local_path, "rb") as stream:
                    while data := stream.read(self.multipart_part_size):
                        number = len(parts) + 1
                        response = self.client.upload_part(
                            Bucket=self.bucket,
                            Key=key,
                            PartNumber=number,
                            UploadId=upload_id,
                            Body=data,
                        )
                        parts.append({"ETag": response["ETag"], "PartNumber": number})
                self.client.complete_multipart_upload(
                    Bucket=self.bucket,
                    Key=key,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                )
            except Exception:
                self.client.abort_multipart_upload(
                    Bucket=self.bucket,
                    Key=key,
                    UploadId=upload_id,
                )
                raise
            return True
        except Exception:
            logger.warning("upload failed %s", remote_path)
            return False

    def download_file(self, remote_path, local_path):
        # The streaming body is closed even when read fails.
        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            body = self.client.get_object(
                Bucket=self.bucket, Key=self._key(remote_path)
            )["Body"]
            try:
                raw = body.read()
            finally:
                body.close()
            with open(local_path, "wb") as stream:
                stream.write(raw)
            return True
        except Exception:
            logger.warning("download failed %s", remote_path)
            return False

    def file_exists(self, path):
        # GET fallback retains legacy HEAD-limited backend support.
        key = self._key(path)
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            try:
                self.client.get_object(Bucket=self.bucket, Key=key)["Body"].close()
                return True
            except Exception:
                return False

    def delete_file(self, path):
        # SDK delete remains idempotent with a boolean failure result.
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self._key(path))
            return True
        except Exception:
            logger.warning("delete failed %s", path)
            return False

    def list_files(self, path):
        # Preserve pagination and let malformed/failed remote listings fail closed.
        prefix = self._key(path)
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        keys = []
        kwargs = {"Bucket": self.bucket, "Prefix": prefix}
        tokens = set()
        while True:
            response = self.client.list_objects_v2(**kwargs)
            for obj in response.get("Contents", []):
                key = obj["Key"]
                if not key.endswith("/"):
                    keys.append(key[len(prefix) :])
            if not response.get("IsTruncated"):
                return keys
            token = response.get("NextContinuationToken")
            if not isinstance(token, str) or not token or token in tokens:
                raise SyncError("S3 listing: invalid continuation token")
            tokens.add(token)
            kwargs["ContinuationToken"] = token

    def test_connection(self):
        # Preserve the existing client-construction probe.
        pass

    def close(self):
        # Close the SDK HTTP pool, not only the Python reference.
        client, self.client = self.client, None
        if client is not None:
            client.close()


class Plugin:
    provider_id = "sync.s3"
    api_version = 1

    def start(self, context):
        # Activation is independent of operation-scoped HTTP clients.
        pass

    def stop(self):
        # Host operations drain and close their own network clients.
        pass

    def create_backend(self, config, secrets):
        # Backends are independent even if the registry caches this factory object.
        return S3Backend(config, secrets)


def create_plugin():
    # Resolve only network implementation owned by this distribution.
    return Plugin()
