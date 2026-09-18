# pyright: basic

import asyncio
import builtins
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

DENIED_EXTERNAL = (
    "dns",
    "socket",
    "urllib",
    "curl",
    "subprocess",
    "device",
    "helper",
    "ffmpeg",
    "adb",
)
EXPECTED_DENIALS = DENIED_EXTERNAL + ("path.write",)
PATH_SCOPES = (
    "repo-read",
    "fixture-read",
    "runtime-read",
    "output-write",
    "workspace-write",
)


class FixturePolicyError(ValueError):
    pass


class ExternalAccessDenied(RuntimeError):
    def __init__(self, kind, target):
        self.kind = kind
        self.target = str(target)
        super().__init__(f"{kind}: denied by offline fixture policy")


# Decode JSON without allowing ambiguous duplicate keys in fixtures or reports.
def load_json(path):
    def unique_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise FixturePolicyError(f"fixture JSON duplicate key: {key}")
            value[key] = item
        return value

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=unique_pairs
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FixturePolicyError(
            f"fixture policy: cannot read {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise FixturePolicyError("fixture policy: expected object")
    return value


# Require all switches so a caller cannot accidentally run an online capture.
def require_offline_flags(offline, no_network, intercept_external):
    if not offline:
        raise FixturePolicyError("--offline: required")
    if not no_network:
        raise FixturePolicyError("--no-network: required")
    if not intercept_external:
        raise FixturePolicyError("--intercept-external: required")


class FixtureRunner:
    def __init__(
        self,
        repo_root,
        fixture_root,
        scope,
        output_paths=(),
        write_roots=(),
        workspace_parent=None,
        policy_path=None,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.fixture_root = Path(fixture_root).resolve()
        self.scope = scope
        self.policy_path = Path(
            policy_path or self.fixture_root / "offline-policy.json"
        ).resolve()
        self.policy = self._load_policy()
        self.errors = []
        self.trace = []
        self.denials = []
        self._patches = []
        self._workspace = None
        self._workspace_parent = Path(
            workspace_parent or self.repo_root / ".omo" / "evidence"
        ).resolve()
        self._write_targets = {Path(path).resolve() for path in output_paths}
        self._write_roots = {Path(path).resolve() for path in write_roots}
        self._read_roots = (
            self.repo_root,
            self.fixture_root,
            self.policy_path.parent,
            Path(sys.prefix).resolve(),
            Path(sys.base_prefix).resolve(),
        )

    # Validate the one closed policy format instead of supporting a policy DSL.
    def _load_policy(self):
        value = load_json(self.policy_path)
        allowed = {
            "schema_version",
            "kind",
            "allowed_path_scopes",
            "allow_subprocess",
            "denied_external",
            "expected_denials",
            "external_probe",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise FixturePolicyError(f"fixture policy: forbidden field {unknown[0]}")
        required = allowed - {"external_probe"}
        missing = sorted(required - set(value))
        if missing:
            raise FixturePolicyError(f"fixture policy: missing field {missing[0]}")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise FixturePolicyError("fixture policy.schema_version: expected 1")
        if value["kind"] not in ("shared", self.scope):
            raise FixturePolicyError(f"fixture policy.kind: expected {self.scope}")
        for field, expected in (
            ("allowed_path_scopes", PATH_SCOPES),
            ("denied_external", DENIED_EXTERNAL),
            ("expected_denials", EXPECTED_DENIALS),
        ):
            actual = value[field]
            if not isinstance(actual, list) or any(
                not isinstance(item, str) for item in actual
            ):
                raise FixturePolicyError(
                    f"fixture policy.{field}: expected string array"
                )
            if tuple(actual) != expected:
                raise FixturePolicyError(f"fixture policy.{field}: unexpected")
        if not isinstance(value["allow_subprocess"], list):
            raise FixturePolicyError("fixture policy.allow_subprocess: expected array")
        for item in value["allow_subprocess"]:
            if not isinstance(item, dict) or set(item) != {
                "command",
                "scope",
                "purpose",
            }:
                raise FixturePolicyError(
                    "fixture policy.allow_subprocess: expected fixed object"
                )
            if item != {
                "command": "node",
                "scope": "ui",
                "purpose": "local-chromium-capture",
            }:
                raise FixturePolicyError(
                    "fixture policy.allow_subprocess: unsupported interaction"
                )
        probe = value.get("external_probe")
        if probe is not None:
            if not isinstance(probe, dict) or set(probe) != {
                "provider",
                "kind",
                "target",
                "must_be_blocked",
            }:
                raise FixturePolicyError(
                    "fixture policy.external_probe: expected fixed object"
                )
            if (
                not isinstance(probe["provider"], str)
                or probe["kind"] not in ("dns", "browser-request")
                or not isinstance(probe["target"], str)
                or not probe["target"]
                or type(probe["must_be_blocked"]) is not bool
            ):
                raise FixturePolicyError(
                    "fixture policy.external_probe: invalid fields"
                )
        return value

    # Allocate a runner-owned operation root outside fixtures and tracked source files.
    def workspace(self):
        if self._workspace is None:
            self._workspace_parent.mkdir(parents=True, exist_ok=True)
            self._workspace = Path(
                tempfile.mkdtemp(prefix="todo17-fixture-", dir=self._workspace_parent)
            ).resolve()
            self.trace.append(
                {
                    "kind": "path.workspace",
                    "target": "<workspace>",
                    "verdict": "allowed",
                }
            )
        return self._workspace

    # Keep evidence portable by replacing machine-local roots in intercepted paths.
    def _display_path(self, path):
        path = Path(path).resolve()
        for root, label in (
            (self._workspace, "<workspace>"),
            (self.fixture_root, "<fixture-root>"),
            (self.repo_root, "<repo>"),
        ):
            if root is None:
                continue
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            return label if relative == Path(".") else f"{label}/{relative.as_posix()}"
        return path.as_posix()

    # Determine whether a resolved target is contained by one fixed root.
    def _inside(self, path, root):
        try:
            Path(path).resolve().relative_to(Path(root).resolve())
        except (OSError, ValueError):
            return False
        return True

    # Allow reads only from source, fixture, and selected runtime roots.
    def _allow_read(self, path):
        return any(self._inside(path, root) for root in self._read_roots)

    # Allow writes only to caller-selected reports and runner-owned workspaces.
    def _allow_write(self, path):
        candidate = Path(path).resolve()
        if candidate in self._write_targets:
            return True
        if self._workspace is not None and self._inside(candidate, self._workspace):
            return True
        return any(self._inside(candidate, root) for root in self._write_roots)

    # Record a blocked operation before it reaches any operating-system backend.
    def _deny(self, kind, target):
        self.trace.append({"kind": kind, "target": str(target), "verdict": "blocked"})
        self.denials.append(kind)
        raise ExternalAccessDenied(kind, target)

    # Check file operation targets before allowing their original implementation.
    def _guard_path(self, path, write):
        allowed = self._allow_write(path) if write else self._allow_read(path)
        if not allowed:
            self._deny("path.write" if write else "path.read", self._display_path(path))

    # Track one global monkey patch so restoration is always symmetric.
    def _patch(self, target, name, replacement):
        self._patches.append((target, name, getattr(target, name)))
        setattr(target, name, replacement)

    # Classify a blocked executable without executing or resolving it.
    def _command_kind(self, command):
        if isinstance(command, (list, tuple)) and command:
            name = Path(str(command[0])).name.casefold()
        else:
            name = str(command).split(maxsplit=1)[0].rsplit("/", 1)[-1].casefold()
        if "ffmpeg" in name:
            return "ffmpeg", name
        if name in ("adb", "adb.exe"):
            return "adb", name
        if "wechat" in name or "keyfinder" in name or "helper" in name:
            return "helper", name
        if "device" in name:
            return "device", name
        return "subprocess", name or "unknown"

    # Permit only the fixed local Node launcher for the Chromium fixture capture.
    def _subprocess_allowed(self, command):
        if not isinstance(command, (list, tuple)) or not command:
            return False
        executable = Path(str(command[0])).stem.casefold()
        expected_script = (
            self.repo_root / "scripts" / "plugin_ui_capture.mjs"
        ).resolve()
        includes_script = any(
            isinstance(value, (str, os.PathLike))
            and Path(value).resolve() == expected_script
            for value in command[1:]
        )
        return (
            self.scope == "ui"
            and includes_script
            and any(
                item["command"] == executable and item["scope"] == "ui"
                for item in self.policy["allow_subprocess"]
            )
        )

    # Install no-network, no-process, and constrained-path guards for this scope.
    def _install_guards(self):
        original_open = builtins.open
        original_path_open = Path.open
        original_path_write_text = Path.write_text
        original_path_write_bytes = Path.write_bytes
        original_path_unlink = Path.unlink
        original_path_rename = Path.rename
        original_path_replace = Path.replace
        original_path_mkdir = Path.mkdir
        original_os_open = os.open
        original_os_remove = os.remove
        original_os_unlink = os.unlink
        original_os_rename = os.rename
        original_os_replace = os.replace
        original_os_mkdir = os.mkdir
        original_os_rmdir = os.rmdir
        original_popen = subprocess.Popen
        original_which = shutil.which

        def guarded_open(file, mode="r", *args, **kwargs):
            if isinstance(file, (str, bytes, os.PathLike)):
                self._guard_path(file, any(flag in mode for flag in "wax+"))
            return original_open(file, mode, *args, **kwargs)

        def guarded_path_open(path, mode="r", *args, **kwargs):
            self._guard_path(path, any(flag in mode for flag in "wax+"))
            return original_path_open(path, mode, *args, **kwargs)

        def guarded_write_text(path, *args, **kwargs):
            self._guard_path(path, True)
            return original_path_write_text(path, *args, **kwargs)

        def guarded_write_bytes(path, *args, **kwargs):
            self._guard_path(path, True)
            return original_path_write_bytes(path, *args, **kwargs)

        def guarded_unlink(path, *args, **kwargs):
            self._guard_path(path, True)
            return original_path_unlink(path, *args, **kwargs)

        def guarded_rename(path, target, *args, **kwargs):
            self._guard_path(path, True)
            self._guard_path(target, True)
            return original_path_rename(path, target, *args, **kwargs)

        def guarded_replace(path, target, *args, **kwargs):
            self._guard_path(path, True)
            self._guard_path(target, True)
            return original_path_replace(path, target, *args, **kwargs)

        def guarded_path_mkdir(path, *args, **kwargs):
            if not path.exists():
                self._guard_path(path, True)
            return original_path_mkdir(path, *args, **kwargs)

        def guarded_os_open(path, flags, *args, **kwargs):
            write = bool(
                flags
                & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            )
            self._guard_path(path, write)
            return original_os_open(path, flags, *args, **kwargs)

        def guarded_os_remove(path, *args, **kwargs):
            self._guard_path(path, True)
            return original_os_remove(path, *args, **kwargs)

        def guarded_os_unlink(path, *args, **kwargs):
            self._guard_path(path, True)
            return original_os_unlink(path, *args, **kwargs)

        def guarded_os_rename(source, destination, *args, **kwargs):
            self._guard_path(source, True)
            self._guard_path(destination, True)
            return original_os_rename(source, destination, *args, **kwargs)

        def guarded_os_replace(source, destination, *args, **kwargs):
            self._guard_path(source, True)
            self._guard_path(destination, True)
            return original_os_replace(source, destination, *args, **kwargs)

        def guarded_os_mkdir(path, *args, **kwargs):
            self._guard_path(path, True)
            return original_os_mkdir(path, *args, **kwargs)

        def guarded_os_rmdir(path, *args, **kwargs):
            self._guard_path(path, True)
            return original_os_rmdir(path, *args, **kwargs)

        def guarded_popen(command, *args, **kwargs):
            if self._subprocess_allowed(command):
                self.trace.append(
                    {
                        "kind": "subprocess",
                        "target": "node:local-chromium-capture",
                        "verdict": "allowed",
                    }
                )
                return original_popen(command, *args, **kwargs)
            kind, name = self._command_kind(command)
            return self._deny(kind, name)

        def guarded_system(command):
            kind, name = self._command_kind(command)
            return self._deny(kind, name)

        def guarded_os_popen(command, *args, **kwargs):
            kind, name = self._command_kind(command)
            return self._deny(kind, name)

        def guarded_which(command, *args, **kwargs):
            kind, name = self._command_kind(command)
            if kind in ("helper", "ffmpeg", "adb", "device"):
                return self._deny(kind, name)
            return original_which(command, *args, **kwargs)

        def deny_dns(*args, **kwargs):
            return self._deny("dns", args[0] if args else "unknown")

        def deny_socket(*args, **kwargs):
            return self._deny("socket", "socket")

        def deny_urllib(*args, **kwargs):
            return self._deny("urllib", args[0] if args else "unknown")

        def deny_curl(*args, **kwargs):
            return self._deny("curl", args[1] if len(args) > 1 else "unknown")

        async def deny_async_process(*args, **kwargs):
            kind, name = self._command_kind(args)
            return self._deny(kind, name)

        self._patch(builtins, "open", guarded_open)
        self._patch(Path, "open", guarded_path_open)
        self._patch(Path, "write_text", guarded_write_text)
        self._patch(Path, "write_bytes", guarded_write_bytes)
        self._patch(Path, "unlink", guarded_unlink)
        self._patch(Path, "rename", guarded_rename)
        self._patch(Path, "replace", guarded_replace)
        self._patch(Path, "mkdir", guarded_path_mkdir)
        self._patch(os, "open", guarded_os_open)
        self._patch(os, "remove", guarded_os_remove)
        self._patch(os, "unlink", guarded_os_unlink)
        self._patch(os, "rename", guarded_os_rename)
        self._patch(os, "replace", guarded_os_replace)
        self._patch(os, "mkdir", guarded_os_mkdir)
        self._patch(os, "rmdir", guarded_os_rmdir)
        self._patch(subprocess, "Popen", guarded_popen)
        self._patch(os, "system", guarded_system)
        self._patch(os, "popen", guarded_os_popen)
        self._patch(shutil, "which", guarded_which)
        self._patch(asyncio, "create_subprocess_exec", deny_async_process)
        self._patch(asyncio, "create_subprocess_shell", deny_async_process)
        self._patch(socket, "socket", deny_socket)
        self._patch(socket, "create_connection", deny_socket)
        self._patch(socket, "getaddrinfo", deny_dns)
        self._patch(socket, "gethostbyname", deny_dns)
        self._patch(socket, "gethostbyname_ex", deny_dns)
        self._patch(socket, "gethostbyaddr", deny_dns)
        self._patch(urllib.request, "urlopen", deny_urllib)
        self._patch(urllib.request.OpenerDirector, "open", deny_urllib)
        self._patch(http.client.HTTPConnection, "connect", deny_urllib)
        self._patch(http.client.HTTPSConnection, "connect", deny_urllib)
        try:
            from curl_cffi import requests as curl_requests
        except ImportError:
            curl_requests = None
        if curl_requests is not None:
            self._patch(curl_requests.Session, "request", deny_curl)

    # Enter guards without creating source-tree bytecode.
    def __enter__(self):
        self._previous_dont_write_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        self._install_guards()
        self.trace.append(
            {
                "kind": "interception",
                "target": "dns/socket/http/curl/subprocess/helper/ffmpeg/adb/path",
                "verdict": "active",
            }
        )
        return self

    # Restore every process-global hook even when a fixture or assertion fails.
    def __exit__(self, _exc_type, _exc_value, _traceback):
        try:
            for target, name, original in reversed(self._patches):
                setattr(target, name, original)
        finally:
            self._patches.clear()
            sys.dont_write_bytecode = self._previous_dont_write_bytecode

    # Exercise every denied class without issuing a real network request or process.
    def probe_denials(self):
        def curl_probe():
            from curl_cffi import requests as curl_requests

            return curl_requests.Session().get("https://fixture.invalid/")

        probes = (
            ("dns", lambda: socket.getaddrinfo("fixture.invalid", 443)),
            ("socket", lambda: socket.socket()),
            ("urllib", lambda: urllib.request.urlopen("https://fixture.invalid/")),
            ("curl", curl_probe),
            ("subprocess", lambda: subprocess.run(["fixture-subprocess"])),
            ("device", lambda: subprocess.run(["fixture-device"])),
            ("helper", lambda: subprocess.run(["wechat_keyfinder.exe"])),
            ("ffmpeg", lambda: subprocess.run(["ffmpeg", "-version"])),
            ("adb", lambda: subprocess.run(["adb", "devices"])),
            (
                "path.write",
                lambda: (self.repo_root / ".qa-todo17-denied-write").write_text(
                    "blocked", encoding="utf-8"
                ),
            ),
        )
        for expected, probe in probes:
            try:
                probe()
            except ExternalAccessDenied as error:
                if error.kind != expected:
                    self.errors.append(
                        f"policy.{expected}: wrong denied operation {error.kind}"
                    )
            except Exception as error:
                self.errors.append(
                    f"policy.{expected}: guard raised {type(error).__name__}"
                )
            else:
                self.errors.append(f"policy.{expected}: external access was allowed")

    # Exercise the optional adversarial DNS request and retain its blocked trace.
    def probe_fixture_external_request(self):
        probe = self.policy.get("external_probe")
        if not probe or probe["kind"] == "browser-request":
            return
        try:
            socket.getaddrinfo(probe["target"], 443)
        except ExternalAccessDenied:
            blocked = True
        else:
            blocked = False
        if not blocked:
            self.errors.append(
                f"provider {probe['provider']} policy.{probe['kind']}: "
                "interception missing"
            )
        if not probe["must_be_blocked"]:
            self.errors.append(
                f"provider {probe['provider']} policy.{probe['kind']}: "
                "fixture requested prohibited external access"
            )

    # Return deterministic interception facts after every requested probe has run.
    def summary(self):
        missing = sorted(set(self.policy["expected_denials"]) - set(self.denials))
        for item in missing:
            message = f"policy.{item}: denial probe was not observed"
            if message not in self.errors:
                self.errors.append(message)
        return {
            "policy": self._display_path(self.policy_path),
            "scope": self.scope,
            "trace": list(self.trace),
            "denials": sorted(set(self.denials)),
            "policy_errors": sorted(set(self.errors)),
        }

    # Remove only the workspace created by this runner and leave a cleanup receipt.
    def cleanup(self):
        removed = False
        if self._workspace is not None:
            shutil.rmtree(self._workspace, ignore_errors=True)
            removed = not self._workspace.exists()
        self.trace.append(
            {
                "kind": "cleanup.workspace",
                "target": "<workspace>",
                "verdict": "removed" if removed else "not-created",
            }
        )
        return removed
