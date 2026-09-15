from ohmymeme_plugin_sync_s3 import S3Backend

from ohmymeme.core.plugins.network_config import SyncError


class R2Backend(S3Backend):
    provider_id = "sync.r2"

    def connect(self):
        # R2 owns its credentials, endpoint and SigV4/auto-region construction.
        import boto3
        from botocore.config import Config

        self.close()
        access = self.secrets.get("access_key_id")
        secret = self.secrets.get("secret_access_key")
        if not access or not secret:
            raise SyncError("R2 credentials not configured")
        endpoint = self.config.endpoint or (
            "https://%s.r2.cloudflarestorage.com" % self.config.account_id
        )
        try:
            self.client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                region_name=self.config.region,
                aws_access_key_id=access,
                aws_secret_access_key=secret,
                config=Config(
                    signature_version="s3v4",
                    s3={
                        "payload_signing_enabled": False,
                        "addressing_style": self.config.addressing_style,
                    },
                ),
            )
        except Exception as error:
            raise SyncError("R2 connect failed: %s" % error) from error


class Plugin:
    provider_id = "sync.r2"
    api_version = 1

    def start(self, context):
        # Activation never opens an ambient or shared S3 connection.
        pass

    def stop(self):
        # Host operation adapters own independent backend lifetimes.
        pass

    def create_backend(self, config, secrets):
        # Reusing S3 wire code never asks the registry for the S3 provider.
        return R2Backend(config, secrets)


def create_plugin():
    # Every invocation returns a distinct provider instance.
    return Plugin()
