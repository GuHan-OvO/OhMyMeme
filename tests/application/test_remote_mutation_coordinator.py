import io
import threading

import pytest
from PIL import Image

from ohmymeme.app.library import LibraryService
from ohmymeme.app.remote_mutation_coordinator import (
    RemoteMutationBusyError,
    RemoteMutationCoordinator,
    RemoteMutationReentrantError,
)
from ohmymeme.app.remote_mutation_errors import RemoteMutationWorkerError
from ohmymeme.core.imports import ImportBytes
from ohmymeme.services.sync import service as sync_service


def test_remote_mutation_when_committed_then_generation_and_barrier_are_recorded(
    tmp_path,
):
    # Given: one coordinator owning one data root
    coordinator = RemoteMutationCoordinator(tmp_path / "data")
    try:
        # When: a mutation reaches its worker barrier and commits
        with coordinator.mutation("library.reorder") as lease:
            assert lease.generation == 0
            lease.start_workers(2)
            lease.worker_done()
            lease.worker_done()
            lease.wait_for_workers(timeout=1)
            lease.commit()

        # Then: one generation advances and the complete lease transcript is ordered
        assert coordinator.generation == 1
        events = coordinator.get_transcript()
        assert [event["event"] for event in events] == [
            "acquire",
            "barrier_start",
            "barrier_worker_done",
            "barrier_worker_done",
            "barrier_complete",
            "commit",
            "release",
        ]
        assert events[-2]["generation"] == 1
        assert events[-1]["committed"] is True
    finally:
        coordinator.close()


def test_remote_mutation_when_same_thread_reenters_then_fails_closed(tmp_path):
    # Given: an active non-reentrant library-state lease
    coordinator = RemoteMutationCoordinator(tmp_path / "data")
    try:
        with coordinator.mutation("library.import"):
            # When/Then: the owner cannot acquire a nested lease
            with pytest.raises(RemoteMutationReentrantError):
                coordinator.acquire("library.rename")
    finally:
        coordinator.close()


def test_remote_mutation_when_another_thread_acquires_then_busy_is_observable(tmp_path):
    # Given: one thread holds the only library-state lease
    coordinator = RemoteMutationCoordinator(tmp_path / "data")
    entered = threading.Event()
    release = threading.Event()
    observed = []

    def owner():
        with coordinator.mutation("sync.push"):
            entered.set()
            release.wait(timeout=1)

    worker = threading.Thread(target=owner)
    worker.start()
    assert entered.wait(timeout=1)

    # When: another thread requests a conflicting mutation
    try:
        with pytest.raises(RemoteMutationBusyError):
            coordinator.acquire("lan.push_file")
        observed.append(True)
    finally:
        release.set()
        worker.join(timeout=1)
        coordinator.close()

    # Then: the conflict was returned, not silently accepted
    assert observed == [True]


def test_remote_mutation_when_data_root_is_claimed_then_second_owner_is_rejected(
    tmp_path,
):
    # Given: one process-local owner for a data root
    first = RemoteMutationCoordinator(tmp_path / "data")
    try:
        # When/Then: a second coordinator cannot share the root
        with pytest.raises(RemoteMutationBusyError):
            RemoteMutationCoordinator(tmp_path / "data")
    finally:
        first.close()


def test_library_rejects_foreign_lease_before_delete_all(tmp_path):
    # Given: a LibraryService bound to coordinator A and a lease from coordinator B
    first = RemoteMutationCoordinator(tmp_path / "first")
    second = RemoteMutationCoordinator(tmp_path / "second")
    deleted = []

    class Persistence:
        def delete_all(self):
            deleted.append(True)

    try:
        service = LibraryService(
            None, Persistence(), lambda: None, mutation_coordinator=first
        )
        with second.mutation("foreign") as lease:
            with pytest.raises(Exception):
                service.delete_all_metadata(lease)
        assert deleted == []
    finally:
        first.close()
        second.close()


def test_direct_sync_workers_require_coordinator_lease(tmp_path):
    # Given: the public legacy worker exports without a bound runtime
    with pytest.raises(RemoteMutationWorkerError):
        sync_service._push_worker([], "", tmp_path, {})
    with pytest.raises(RemoteMutationWorkerError):
        sync_service._pull_worker([], "", tmp_path, None)


def test_worker_wrapper_rejects_lease_from_foreign_runtime_coordinator(tmp_path):
    # Given: runtime coordinator A and a worker wrapper handed coordinator B's lease
    first = RemoteMutationCoordinator(tmp_path / "first")
    second = RemoteMutationCoordinator(tmp_path / "second")
    try:
        with first.mutation("runtime") as first_lease:
            with sync_service._bind_legacy_runtime(first, first_lease):
                with second.mutation("foreign") as second_lease:
                    with pytest.raises(RemoteMutationWorkerError):
                        sync_service._push_worker_with_barrier(
                            second_lease, [], "/", tmp_path, {}
                        )
        assert not any(
            event["entrypoint"] == "foreign" and event["event"] == "commit"
            for event in second.get_transcript()
        )
    finally:
        first.close()
        second.close()

    # Then: release permits a later owner
    second = RemoteMutationCoordinator(tmp_path / "data")
    second.close()


def test_container_when_library_imports_then_all_state_services_share_one_coordinator(
    tmp_path,
):
    # Given: one fully composed Container and a valid image payload
    from ohmymeme.app.container import Container

    container = Container(tmp_path / "app")
    payload = io.BytesIO()
    Image.new("RGBA", (1, 1), (255, 0, 0, 255)).save(payload, "PNG")
    try:
        # When: the Container-owned import service commits library state
        service = container.create_import_service()
        result = service.import_bytes(ImportBytes(payload.getvalue(), "one.png"))

        # Then: composition uses one coordinator and records the import commit
        assert result.imported_ids
        assert container.catalog._mutation_coordinator is container.remote_mutations
        assert service._mutation_coordinator is container.remote_mutations
        assert container.sync._coordinator is container.remote_mutations
        assert container.lan._mutation_coordinator is container.remote_mutations
        assert container.lan._coordinator is container.operations
        assert any(
            event["event"] == "commit" and event["entrypoint"] == "library.import"
            for event in container.remote_mutations.get_transcript()
        )
    finally:
        container.close()


def test_container_when_same_data_root_is_open_then_second_container_fails_closed(
    tmp_path,
):
    # Given: a live Container that owns its process-local data root
    from ohmymeme.app.container import Container

    root = tmp_path / "app"
    first = Container(root)
    try:
        # When/Then: a second object graph cannot share library state
        with pytest.raises(RemoteMutationBusyError):
            Container(root)
    finally:
        first.close()


def test_sync_pushes_to_loopback_webdav_with_worker_barrier(
    tmp_path,
):
    # Given: a Container-bound library and an actual loopback WebDAV listener
    from ohmymeme.app.container import Container
    from .local_remote_servers import LocalWebDavServer

    server = LocalWebDavServer(tmp_path / "fixture")
    server.start()
    container = Container(tmp_path / "app")
    try:
        container.config.set("sync_type", "webdav")
        container.config.set("webdav_url", f"http://127.0.0.1:{server.port}")
        container.config.save()
        payload = io.BytesIO()
        Image.new("RGBA", (1, 1), (255, 0, 0, 255)).save(payload, "PNG")
        service = container.create_import_service()
        imported = service.import_bytes(ImportBytes(payload.getvalue(), "one.png"))
        assert imported.imported_ids
        result = container.sync.push()
        assert result["uploaded"] == 1
        events = container.remote_mutations.get_transcript()
        assert any(event["event"] == "barrier_start" for event in events)
        assert any(
            event["event"] == "commit" and event["entrypoint"] == "sync.push"
            for event in events
        )
    finally:
        if not container._closed:
            container.close()
        server.close()


def test_lan_file_during_sync_lease_returns_busy(
    tmp_path,
):
    # Given: a Container whose LAN command handler shares the library-state lease
    from ohmymeme.app.container import Container

    container = Container(tmp_path / "app")
    entered = threading.Event()
    release = threading.Event()
    try:

        def hold_lease():
            with container.remote_mutations.mutation("sync.push"):
                entered.set()
                release.wait(timeout=1)

        worker = threading.Thread(target=hold_lease)
        worker.start()
        assert entered.wait(timeout=1)
        result = container.lan._cmd_push_file(
            {"filename": "one.png", "data": "bm90LWltYWdl"}
        )
        assert result["ok"] is False
        assert "remote_mutation_busy" in result["error"]
        release.set()
        worker.join(timeout=1)
    finally:
        release.set()
        container.close()


def test_container_sync_apply_remote_metadata_mutations_use_the_shared_lease(tmp_path):
    # Given: a Container with two memes that can receive a remote order/collection
    from ohmymeme.app.container import Container

    container = Container(tmp_path / "app")
    first = container.db.add_meme("one.png", file_hash="1" * 64)
    second = container.db.add_meme("two.png", file_hash="2" * 64)
    try:
        # When: the public Container sync facade applies both remote projections
        container.sync.apply_remote_order(
            {"memes": [{"filename": "two.png"}, {"filename": "one.png"}]}
        )
        container.sync.apply_remote_collections(
            {"collections": [{"name": "remote", "filenames": ["one.png"]}]}
        )

        # Then: each public mutation has its own ordered coordinator transcript
        events = container.remote_mutations.get_transcript()
        commits = [
            event["entrypoint"] for event in events if event["event"] == "commit"
        ]
        assert commits == ["sync.apply_remote_order", "sync.apply_remote_collections"]
        assert first != second
        assert [row["filename"] for row in container.db.search()] == [
            "two.png",
            "one.png",
        ]
    finally:
        container.close()


def test_public_legacy_sync_push_records_a_temporary_lease_without_changing_abi(
    tmp_path, monkeypatch
):
    # Given: a legacy module call and a spy that retains its temporary coordinator
    import ohmymeme.services.sync.service as sync

    class Config:
        data_dir = tmp_path

    class Database:
        pass

    coordinators = []

    class SpyCoordinator(RemoteMutationCoordinator):
        def __init__(self, data_root):
            super().__init__(data_root)
            coordinators.append(self)

    monkeypatch.setattr(sync, "RemoteMutationCoordinator", SpyCoordinator)
    monkeypatch.setattr(sync, "get_config", lambda: Config())
    monkeypatch.setattr(sync, "get_db", lambda: Database())
    monkeypatch.setattr(sync, "_push_impl", lambda _delete_remote=None: {"ok": True})
    # When: the public legacy push adapter is called with its frozen signature
    result = sync.push()

    # Then: the adapter acquires and commits a library-state lease
    assert result == {"ok": True}
    assert [
        event["entrypoint"]
        for event in coordinators[0].get_transcript()
        if event["event"] == "commit"
    ] == ["sync.push"]
    coordinators[0].close()


def test_public_legacy_sync_mutation_exports_all_acquire_leases(tmp_path, monkeypatch):
    # Given: legacy mutation exports whose implementation work is isolated
    import ohmymeme.services.sync.service as sync

    class Config:
        data_dir = tmp_path

    class Database:
        pass

    implementations = {
        "_pull_impl": lambda *_args: {"ok": True},
        "_upload_index_impl": lambda *_args: True,
        "_delete_all_remote_impl": lambda *_args: {"ok": True},
        "_cleanup_remote_orphans_impl": lambda *_args: {"ok": True},
    }
    for name, implementation in implementations.items():
        monkeypatch.setattr(sync, name, implementation)
    monkeypatch.setattr(sync, "get_config", lambda: Config())
    monkeypatch.setattr(sync, "get_db", lambda: Database())
    coordinators = []

    class SpyCoordinator(RemoteMutationCoordinator):
        def __init__(self, data_root):
            super().__init__(data_root)
            coordinators.append(self)

    monkeypatch.setattr(sync, "RemoteMutationCoordinator", SpyCoordinator)
    monkeypatch.setattr(sync, "get_config", lambda: Config())
    monkeypatch.setattr(sync, "get_db", lambda: Database())
    # When: each public legacy mutation export is called with its frozen ABI
    sync.pull()
    sync.upload_index()
    sync.cleanup_remote_orphans(delete=True)
    sync.delete_all_remote()

    # Then: no export bypasses acquire/commit/release on its mutation path
    commits = [
        event["entrypoint"]
        for coordinator in coordinators
        for event in coordinator.get_transcript()
        if event["event"] == "commit"
    ]
    assert commits == [
        "sync.pull",
        "sync.upload_index",
        "sync.cleanup",
        "sync.delete_all",
    ]
    for coordinator in coordinators:
        coordinator.close()


def test_container_lan_push_manifest_reuses_one_outer_lease(tmp_path):
    # Given: a Container-owned LAN handler and a valid empty v3 manifest
    from ohmymeme.app.container import Container

    container = Container(tmp_path / "app")
    try:
        # When: LAN applies the manifest through its command path
        result = container.lan._cmd_push_manifest(
            {"version": 3, "memes": [], "collections": []}
        )

        # Then: the outer LAN operation commits once without reentrant failure
        assert result["ok"] is True
        commits = [
            event["entrypoint"]
            for event in container.remote_mutations.get_transcript()
            if event["event"] == "commit"
        ]
        assert commits == ["lan.push_manifest"]
    finally:
        container.close()


def test_container_lan_send_config_when_busy_does_not_mutate_or_commit(tmp_path):
    # Given: a Container whose library-state lease is held by another worker
    from ohmymeme.app.container import Container

    container = Container(tmp_path / "app")
    container.config.set("hotkey", "before")
    container.config.save()
    entered = threading.Event()
    release = threading.Event()
    try:

        def hold_lease():
            with container.remote_mutations.mutation("sync.push"):
                entered.set()
                release.wait(timeout=1)

        worker = threading.Thread(target=hold_lease)
        worker.start()
        assert entered.wait(timeout=1)

        # When: an authorized LAN client sends a configuration mutation while busy
        result = container.lan._cmd_send_config({"hotkey": "after"})

        # Then: the compatible busy envelope is returned and no config commit occurs
        assert result["ok"] is False
        assert "remote_mutation_busy" in result["error"]
        assert container.config.get("hotkey") == "before"
        assert not any(
            event["event"] == "commit" and event["entrypoint"] == "lan.send_config"
            for event in container.remote_mutations.get_transcript()
        )
        release.set()
        worker.join(timeout=1)
    finally:
        release.set()
        container.close()


def test_container_lan_send_config_success_records_shared_lease(tmp_path):
    # Given: a Container LAN handler with its own config and coordinator
    from ohmymeme.app.container import Container

    container = Container(tmp_path / "app")
    try:
        # When: an authorized LAN configuration update commits
        result = container.lan._cmd_send_config({"hotkey": "Ctrl+Shift+L"})

        # Then: config changes once and the coordinator transcript is complete
        assert result == {"ok": True}
        assert container.config.get("hotkey") == "Ctrl+Shift+L"
        events = container.remote_mutations.get_transcript()
        assert [event["event"] for event in events] == [
            "acquire",
            "commit",
            "release",
        ]
        assert events[1]["entrypoint"] == "lan.send_config"
    finally:
        container.close()
