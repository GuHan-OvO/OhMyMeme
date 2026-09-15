import hashlib
import io
import ipaddress
import json
import socket
import subprocess
import sys
import tempfile
import traceback
from argparse import ArgumentParser
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from ohmymeme.core.plugins.manifest import canonical_descriptor
from ohmymeme.core.plugins.network_config import SYNC_CONFIGS, SYNC_SECRETS, SyncError
from ohmymeme.core.plugins.registry import PluginRegistry
from ohmymeme.services.sync.backends import get_backend

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PROFILES = [
    "ftp",
    "ftps-control",
    "webdav",
    "webdav-https",
    "s3-sigv2-virtual",
    "s3-sigv2-path",
    "s3-sigv4-virtual",
    "s3-sigv4-path",
    "r2-region-auto",
]
FAILURES = ["missing", "disabled", "incompatible", "stale-entry-point"]


@contextmanager
def offline_network(loopback):
    # Explicit fixtures alone may connect; unexpected access is recorded even if caught.
    violations = []
    original_dns = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_sendto = socket.socket.sendto

    def check(host):
        try:
            allowed = loopback and ipaddress.ip_address(host).is_loopback
        except ValueError:
            allowed = loopback and host == "localhost"
        if not allowed:
            violations.append(f"undeclared network: {host}")
            raise OSError(violations[-1])

    def resolve(host, *args, **kwargs):
        check(host)
        return original_dns(host, *args, **kwargs)

    def connect(sock, address):
        check(address[0])
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check(address[0])
        return original_connect_ex(sock, address)

    def sendto(sock, data, *args):
        check(args[-1][0])
        return original_sendto(sock, data, *args)

    def denied(*args, **kwargs):
        violations.append("undeclared external operation")
        raise OSError(violations[-1])

    with ExitStack() as stack:
        for target, name, replacement in (
            (socket, "getaddrinfo", resolve),
            (socket, "gethostbyname", denied),
            (socket, "gethostbyname_ex", denied),
            (socket.socket, "connect", connect),
            (socket.socket, "connect_ex", connect_ex),
            (socket.socket, "sendto", sendto),
            (subprocess, "Popen", denied),
        ):
            stack.enter_context(patch.object(target, name, replacement))
        yield violations


def seed(provider):
    # These addresses are never resolved: invalid/absence probes stop before connect.
    return {
        "sync_type": provider.split(".")[1],
        "ftp_host": "fixture.invalid",
        "s3_endpoint": "https://fixture.invalid",
        "s3_bucket": "bucket",
        "r2_account_id": "account",
        "r2_bucket": "bucket",
        "r2_access_key_id": "access",
        "r2_secret_access_key": "secret",
        "webdav_url": "https://fixture.invalid",
    }


def expected_matrix():
    # Matrix fields are compared against the real runtime record definitions.
    return {
        "schema_version": 1,
        "port": "RemoteBackendPort",
        "host_owns": [
            "configuration",
            "secrets",
            "durable_paths",
            "leases",
            "manifest",
            "order",
            "pull_commit",
            "hash_validation",
        ],
        "providers": {
            provider: {
                "record": record.__name__,
                "fields": list(record._fields),
                "secrets": list(SYNC_SECRETS[provider]),
            }
            for provider, record in SYNC_CONFIGS.items()
        },
    }


def provider_rejections():
    # Exercise the same require path used by Container, including a stale triplet.
    from importlib.metadata import EntryPoint

    observations = []
    for provider in SYNC_CONFIGS:
        descriptor = canonical_descriptor(provider)
        for reason in FAILURES:
            active = (
                descriptor._replace(api_version=99)
                if reason == "incompatible"
                else descriptor
            )
            entries = ()
            if reason == "stale-entry-point":
                entries = (
                    EntryPoint(
                        name=provider,
                        value="wrong:create_plugin",
                        group=descriptor.group,
                    ),
                )
            registry = PluginRegistry((active,), {}, entries)
            enabled = () if reason == "disabled" else None
            try:
                get_backend(seed(provider), registry=registry, enabled=enabled)
            except SyncError as error:
                observations.append(
                    {"provider": provider, "case": reason, "error": str(error)}
                )
            else:
                raise AssertionError(f"{provider}: {reason} accepted")
    return observations


def runtime_roundtrip(root):
    # Real Container -> sync service/leases -> registry -> factory -> WebDAV wire.
    from PIL import Image

    from ohmymeme.app.container import Container
    from ohmymeme.core.imports import ImportBytes
    from scripts.local_remote_servers import LocalWebDavServer

    server = LocalWebDavServer(root / "wire")
    first, second = Container(root / "push"), Container(root / "pull")
    server.start()
    stream = io.BytesIO()
    Image.new("RGBA", (3, 2), (80, 120, 20, 255)).save(stream, "PNG")
    payload = stream.getvalue()
    try:
        for container in (first, second):
            container.config.set("sync_type", "webdav")
            container.config.set("webdav_url", f"http://127.0.0.1:{server.port}")
            container.config.set("webdav_path", "fixture")
        imported = first.create_import_service().import_bytes(
            ImportBytes(payload, "one.png")
        )
        assert len(imported.imported_ids) == 1
        pushed = first.sync.push()
        probe = first.sync.sync_test()
        assert probe == "ok"
        pulled = second.sync.pull()
        assert first.db.count() == second.db.count() == 1
        row = second.db.search()[0]
        actual = (second.config.cache_dir / row["filename"]).read_bytes()
        assert actual == payload
        commits = [
            event["entrypoint"]
            for event in second.remote_mutations.get_transcript()
            if event["event"] == "commit"
        ]
        assert "sync.pull" in commits
        return {
            "surface": "Container.sync",
            "sync_test": probe,
            "push": pushed,
            "pull": pulled,
            "rows": second.db.count(),
            "commits": commits,
            "sha256": hashlib.sha256(actual).hexdigest(),
            "wire_methods": [entry["method"] for entry in server.state.request_log],
        }
    finally:
        first.close()
        second.close()
        first.db.close()
        second.db.close()
        server.close()
        assert not server.thread.is_alive()


def happy(root):
    # Existing loopback contracts now execute installed implementation packages.
    from scripts import local_remote_contracts as wire

    observations, factories, servers, backends = {}, [], [], []

    def construct(cfg):
        backend = get_backend(cfg)
        backends.append(backend)
        raw = backend._backend
        factories.append(
            {
                "provider": backend.provider_id,
                "implementation": type(raw).__module__,
                "record": type(raw.config).__name__,
            }
        )
        assert type(raw).__module__.startswith("ohmymeme_plugin_sync_")
        return backend

    with ExitStack() as stack:
        stack.enter_context(patch.object(wire, "get_backend", construct))
        for name in ("LocalFtpServer", "LocalWebDavServer", "LocalS3Server"):
            original = getattr(wire, name)

            def fixture_server(*args, _factory=original, **kwargs):
                instance = _factory(*args, **kwargs)
                servers.append(instance)
                return instance

            stack.enter_context(patch.object(wire, name, fixture_server))
        for profile in PROFILES:
            work = root / profile
            work.mkdir()
            if profile in ("ftp", "ftps-control"):
                result = wire._ftp_profile(work, profile == "ftps-control")
            elif profile.startswith("webdav"):
                result = wire._webdav_profile(work, profile.endswith("https"))
            elif profile.startswith("s3"):
                _, signature, addressing = profile.split("-")
                result = wire._s3_profile(
                    work, "s3" if signature == "sigv2" else "s3v4", addressing
                )
            else:
                result = wire._r2_profile(work)
            assert result["status"] == "pass"
            observations[profile] = result
    assert all(not server.thread.is_alive() for server in servers)
    assert all(server.server.socket.fileno() == -1 for server in servers)
    assert all(
        backend._closed and not backend._secret_port._secrets for backend in backends
    )
    assert all(backend._temporary is None for backend in backends)
    return {
        "profiles": observations,
        "factories": factories,
        "provider_rejections": provider_rejections(),
        "runtime": runtime_roundtrip(root / "runtime"),
        "cleanup": {
            "fixture_servers_closed": len(servers),
            "backend_scopes_closed": len(backends),
            "live_fixture_threads": sum(server.thread.is_alive() for server in servers),
        },
    }


def invalid(fixture):
    # Capture actual loader/socket call counts, never echo expected fixture results.
    observations = []
    for case in fixture["invalid"]:
        provider = case["provider"]
        assert provider in SYNC_CONFIGS
        registry = PluginRegistry((canonical_descriptor(provider),))
        with patch.object(registry, "get", wraps=registry.get) as load:
            with patch(
                "socket.socket", side_effect=AssertionError("network before validation")
            ) as network:
                try:
                    get_backend(seed(provider) | case["config"], registry=registry)
                except SyncError as error:
                    message = str(error)
                    assert case["field"] in message
                else:
                    raise AssertionError(f"{case['field']}: invalid input accepted")
        assert load.call_count == network.call_count == 0
        observations.append(
            {
                "provider": provider,
                "error": message,
                "provider_calls": load.call_count,
                "network_calls": network.call_count,
            }
        )
    return observations


def main():
    # Always replace the requested report with this invocation's observed results.
    parser = ArgumentParser()
    parser.add_argument("--check", action="store_true", required=True)
    for name in ("matrix", "fixture", "report"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    report = {"status": "failed", "observations": {}, "errors": []}
    exit_code = 1
    try:
        matrix = json.loads(args.matrix.read_text(encoding="utf-8"))
        if json.dumps(matrix, sort_keys=True) != json.dumps(
            expected_matrix(), sort_keys=True
        ):
            raise ValueError("matrix: config/host ownership mapping mismatch")
        fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
        if (
            type(fixture.get("schema_version")) is not int
            or fixture["schema_version"] != 1
        ):
            raise ValueError("schema_version: expected integer 1")
        failing = "invalid" in fixture
        if failing:
            if fixture.get("network") != "deny-all" or not fixture["invalid"]:
                raise ValueError("invalid: expected nonempty deny-all cases")
        elif fixture != {
            "schema_version": 1,
            "network": "declared-loopback-only",
            "profiles": PROFILES,
            "runtime": "container-push-pull",
            "provider_failures": FAILURES,
        }:
            raise ValueError("fixture: undeclared profiles or runtime")
        with offline_network(not failing) as violations:
            with tempfile.TemporaryDirectory(
                prefix="ohmm-plugin-sync-qa-"
            ) as temporary:
                report["observations"] = (
                    invalid(fixture) if failing else happy(Path(temporary))
                )
        report["external_violations"] = violations
        assert not violations, violations
        report["status"] = "rejected" if failing else "passed"
        exit_code = 1 if failing else 0
    except Exception as error:
        report["errors"].append(f"{type(error).__name__}: {error}")
        report["traceback"] = traceback.format_exc()
    report["exit_code"] = exit_code
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
