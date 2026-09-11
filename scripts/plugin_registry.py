# pyright: basic

import importlib
import sys
import threading
from pathlib import Path

from plugin_parity_json import load_json, validate_schema, write_json

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

REGISTRY_SCHEMA = ROOT / "schemas" / "plugin" / "registry-fixture.schema.json"
MANIFEST_PATH = ROOT / "config" / "plugin-manifest.json"
manifest_module = importlib.import_module("ohmymeme.core.plugins.manifest")
registry_module = importlib.import_module("ohmymeme.core.plugins.registry")
operations_module = importlib.import_module("ohmymeme.app.operation_coordinator")
domain_module = importlib.import_module("ohmymeme.core.domain")


class _FixturePlugin:
    def __init__(self, start_error=None):
        self.started = 0
        self.stopped = 0
        self._start_error = start_error

    def start(self, _context):
        self.started += 1
        if self._start_error:
            raise RuntimeError(self._start_error)

    def stop(self):
        self.stopped += 1


class _FixtureEntryPoint:
    def __init__(self, value):
        self.group = value["group"]
        self.name = value["name"]
        self.value = value["value"]
        self._load_error = value.get("load_error")
        self._start_error = value.get("start_error")
        self.load_calls = 0

    def load(self):
        self.load_calls += 1
        if self._load_error:
            raise RuntimeError(self._load_error)
        return lambda: _FixturePlugin(self._start_error)


class _Child:
    def __init__(self):
        self.terminated = 0
        self.killed = 0

    def poll(self):
        return 0 if self.killed else None

    def terminate(self):
        self.terminated += 1

    def kill(self):
        self.killed += 1

    def wait(self, _timeout):
        self.killed += 1
        return 0


class _Socket:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


def _parse_arguments(argv):
    paths = {}
    index = 0
    while index < len(argv):
        option = argv[index]
        if option not in ("--fixture", "--operation-fixture", "--report"):
            raise ValueError(f"unknown option: {option}")
        if index + 1 >= len(argv):
            raise ValueError(f"{option}: missing value")
        paths[option] = Path(argv[index + 1])
        index += 2
    for option in ("--fixture", "--operation-fixture", "--report"):
        if option not in paths:
            raise ValueError(f"{option}: missing required option")
    return paths["--fixture"], paths["--operation-fixture"], paths["--report"]


def _validate_fixture(value):
    schema = load_json(REGISTRY_SCHEMA)
    errors = validate_schema(value, schema, schema, "")
    if errors:
        raise ValueError("; ".join(errors))


def _factory():
    return _FixturePlugin()


def _registry_result(fixture):
    _validate_fixture(fixture)
    manifest = load_json(MANIFEST_PATH)
    descriptors = manifest_module.validate_manifest(
        manifest, manifest_module.ENTRY_POINT_GROUP
    )
    entries = tuple(_FixtureEntryPoint(value) for value in fixture["entry_points"])
    builtins = {provider_id: _factory for provider_id in fixture["builtins"]}
    registry = registry_module.PluginRegistry(descriptors, builtins, entries)
    before_load = {entry.name: entry.load_calls for entry in entries}
    for provider_id in fixture["load"]:
        registry.get(provider_id)
    for provider_id in fixture["start"]:
        if registry.start(provider_id, None):
            raise ValueError("registry: expected provider start failure")
    if registry.start("source.qqnt", None) is not True:
        raise ValueError("registry: expected initial provider start")
    if registry.start("source.qqnt", None) is not False:
        raise ValueError("registry: expected duplicate provider start rejection")
    if (
        registry.stop("source.qqnt") is not True
        or registry.stop("source.qqnt") is not False
    ):
        raise ValueError("registry: expected idempotent provider stop")
    status = {item["id"]: item for item in registry.status()}
    sources = {provider_id: item["source"] for provider_id, item in status.items()}
    quarantined = sorted(
        provider_id
        for provider_id, item in status.items()
        if item["quarantine"] is not None
    )
    if sources != fixture["expected"]["sources"]:
        raise ValueError("registry: unexpected provider source order")
    if quarantined != fixture["expected"]["quarantined"]:
        raise ValueError("registry: unexpected quarantine set")
    if before_load.get("source.qqnt") != 0:
        raise ValueError("registry: eager shadowed entry point load")
    isolated = registry_module.PluginRegistry(descriptors, builtins, entries)
    isolated_status = {item["id"]: item for item in isolated.status()}
    for provider_id in fixture["start"]:
        if isolated_status[provider_id]["quarantine"] is not None:
            raise ValueError("registry: provider state leaked across instances")
    return {
        "plugin_duplicate_start": "rejected",
        "plugin_ids": [item["id"] for item in registry.status()],
        "quarantined": quarantined,
        "sources": sources,
    }


def _operation_result(fixture):
    legacy = {
        kind.value for kind in domain_module.TaskKind if kind.value != "lan.service"
    }
    if set(fixture["legacy_task_kinds"]) != legacy:
        raise ValueError("operations: legacy task kinds changed")
    if fixture["lan_task_kind"] != domain_module.TaskKind.LAN_SERVICE.value:
        raise ValueError("operations: missing LAN_SERVICE")
    if operations_module._MUTEX_GROUPS[domain_module.TaskKind.LAN_SERVICE] != "lan":
        raise ValueError("operations: LAN_SERVICE mutex must be lan")
    coordinator = operations_module.OperationCoordinator()
    cleanups = []
    children = []

    def start_worker(task_kind):
        ready = threading.Event()

        def worker(context):
            for socket_name in ("udp", "tcp", "session"):
                socket = _Socket()
                context.register_socket(socket)
                cleanups.append((socket_name, socket))
            context.register_temp(lambda: cleanups.append(("confirmation", None)))
            child = _Child()
            children.append(child)
            context.register_process(child)
            ready.set()
            _ = context.wait_cancelled()

        snapshot = coordinator.start(task_kind, worker)
        if not ready.wait(timeout=1):
            raise ValueError("operations: worker did not register resources")
        return snapshot

    started = start_worker(domain_module.TaskKind.LAN_SERVICE)
    duplicate = coordinator.start(domain_module.TaskKind.LAN_SERVICE, lambda _: None)
    if duplicate.task_id != started.task_id:
        raise ValueError("operations: duplicate task start changed legacy semantics")
    wechat = start_worker(domain_module.TaskKind.IMPORT_WECHAT)
    if wechat.task_id == started.task_id:
        raise ValueError("operations: task IDs are not unique")
    if coordinator.cancel(domain_module.TaskKind.IMPORT_WECHAT) != coordinator.cancel(
        domain_module.TaskKind.IMPORT_WECHAT
    ):
        raise ValueError("operations: cancel is not idempotent")
    coordinator.wait(domain_module.TaskKind.IMPORT_WECHAT, timeout=1)
    telegram = start_worker(domain_module.TaskKind.IMPORT_TELEGRAM)
    coordinator.cancel(domain_module.TaskKind.IMPORT_TELEGRAM)
    coordinator.wait(domain_module.TaskKind.IMPORT_TELEGRAM, timeout=1)
    if telegram.task_id == wechat.task_id:
        raise ValueError("operations: task IDs are not unique")
    first_cancel = coordinator.cancel(domain_module.TaskKind.LAN_SERVICE)
    second_cancel = coordinator.cancel(domain_module.TaskKind.LAN_SERVICE)
    if first_cancel != second_cancel:
        raise ValueError("operations: cancel is not idempotent")
    report = coordinator.shutdown(timeout=0.1, child_grace=0.01)
    if (
        report.timed_out
        or report.inventory.processes
        or report.inventory.sockets
        or report.inventory.temporary
        or report.inventory.threads
    ):
        raise ValueError("operations: shutdown was not bounded")
    if any(socket.closed != 1 for _, socket in cleanups if socket is not None):
        raise ValueError("operations: LAN sockets were not drained")
    if any(child.terminated != 1 for child in children):
        raise ValueError("operations: child process was not drained")
    if ("confirmation", None) not in cleanups:
        raise ValueError("operations: confirmation wait was not cleaned")
    return {
        "cancel": fixture["expected"]["cancel"],
        "coordinator_duplicate_start": fixture["expected"][
            "coordinator_duplicate_start"
        ],
        "lan_mutex": fixture["expected"]["lan_mutex"],
        "shutdown": fixture["expected"]["shutdown"],
        "task_id": str(started.task_id),
    }


def main(argv=None):
    report = None
    try:
        registry_path, operation_path, report = _parse_arguments(
            sys.argv[1:] if argv is None else argv
        )
        result = {
            "operations": _operation_result(load_json(operation_path)),
            "registry": _registry_result(load_json(registry_path)),
            "status": "PASS",
        }
        write_json(report, result)
        print("PASS: registry discovery and lifecycle isolation verified")
    except (OSError, ValueError) as error:
        if report is not None:
            write_json(report, {"errors": [str(error)], "status": "REJECTED"})
        print(f"REJECTED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
