from ohmymeme.app.library import LibraryService
from ohmymeme.core.adapters.sqlite.library import MemeDbLibraryPort
from ohmymeme.core.config import Config
from ohmymeme.core.database import MemeDB


def _service(tmp_path):
    config = Config(tmp_path / "config.json", tmp_path / "data")
    database = MemeDB(config.db_path)
    return LibraryService(config, MemeDbLibraryPort(database), lambda: None), database


def _meme(database, name, tags=()):
    return database.add_meme(
        f"{name}.png",
        file_hash=name * 8,
        original_name=name,
        tags=list(tags),
    )


def test_library_when_filters_combine_then_page_and_total_match_legacy(tmp_path):
    # Given: tagged, favorited and collection-member memes in a real temporary database
    service, database = _service(tmp_path)
    first = _meme(database, "cat-one", ("cat", "fun"))
    second = _meme(database, "cat-two", ("cat", "fun"))
    _meme(database, "dog", ("fun",))
    database.toggle_favorite(first)
    collection = database.create_collection("pets")
    database.add_to_collection(first, collection)
    database.add_to_collection(second, collection)

    # When: the bridge-compatible query combines text, all tags, collection and pages
    total = service.count_memes("cat", ["cat", "fun"], collection)
    page = service.search_memes("cat", ["cat", "fun"], collection, 1, 1)

    # Then: matching, count and offset preserve the historical API contract
    assert total == 2
    assert [meme["id"] for meme in page] == [second]
    assert service.search_memes("cat", ["cat", "missing"], collection, 0, 10) == []


def test_library_when_virtual_collections_and_tree_are_requested_then_unions_and_order_match(tmp_path):
    # Given: one nested collection, overlapping memberships, a favorite and recent use
    service, database = _service(tmp_path)
    parent_member = _meme(database, "parent")
    child_member = _meme(database, "child")
    uncategorized = _meme(database, "loose")
    parent = database.create_collection("parent")
    child = database.create_collection("child", parent)
    database.add_to_collection(parent_member, parent)
    database.add_to_collection(child_member, child)
    database.add_to_collection(child_member, parent)
    database.toggle_favorite(parent_member)
    database.record_use(child_member)

    # When: querying virtual groups and the nested collection tree
    collections = service.get_collections()

    # Then: virtual entries and parent counts retain their legacy union semantics
    assert collections[:3] == [
        {"id": -2, "name": "收藏夹", "count": 1},
        {"id": -3, "name": "最近使用", "count": 1},
        {"id": -4, "name": "未分类", "count": 1},
    ]
    assert collections[3] == {
        "id": parent,
        "name": "parent",
        "count": 2,
        "children": [{"id": child, "name": "child", "count": 1}],
    }
    assert service.search_memes("", [], -4, 0, 10)[0]["id"] == uncategorized
    assert service.search_memes("", [], -3, 0, 10)[0]["id"] == child_member
    assert service.search_memes("missing", ["missing"], -3, 0, 10)[0]["id"] == child_member


def test_library_when_duplicate_depth_mutation_and_manifest_faults_then_legacy_results_remain_visible(tmp_path):
    # Given: a top-level group, its child, and a manifest writer which fails
    config = Config(tmp_path / "config.json", tmp_path / "data")
    database = MemeDB(config.db_path)
    manifest_calls = []

    def failing_manifest():
        manifest_calls.append("called")
        raise OSError("manifest write failed")

    service = LibraryService(config, MemeDbLibraryPort(database), failing_manifest)
    meme = _meme(database, "one")
    parent = database.create_collection("same")
    child = database.create_collection("child", parent)

    # When: duplicate creation, too-deep creation, membership and a persisted reorder occur
    duplicate = service.create_collection_members("same", [meme])
    depth_error = service.create_subcollection("grandchild", child)
    reordered = service.reorder_memes([meme])

    # Then: duplicates/depth preserve bridge values and a manifest failure never claims success
    assert duplicate == {"ok": False, "error": "同名分组已存在，请从下拉框选择已有分组"}
    assert depth_error == {"ok": False, "error": "最大支持1层小分组"}
    assert reordered is False
    assert manifest_calls == ["called"]


def test_library_when_persistence_fails_then_bridge_failure_sentinels_are_preserved(
    tmp_path, monkeypatch
):
    # Given: a service whose typed persistence port rejects tag and membership writes
    service, database = _service(tmp_path)
    meme = _meme(database, "one")
    collection = database.create_collection("target")

    def fail_tags(*_args):
        raise OSError("database unavailable")

    def fail_members(*_args):
        raise OSError("database unavailable")

    monkeypatch.setattr(service._persistence, "set_meme_tags", fail_tags)
    monkeypatch.setattr(service._persistence, "set_collection_members", fail_members)

    # When: bridge-compatible mutation routes encounter persistence faults
    tags_result = service.set_meme_tags(meme, ["tag"])
    members_result = service.set_collection_members(collection, [meme])

    # Then: neither call claims success or exposes a partial bridge result
    assert tags_result is False
    assert members_result is False


def test_library_when_collection_members_reorder_then_membership_union_and_order_persist(tmp_path):
    # Given: two collections with one shared meme and a manifest recorder
    config = Config(tmp_path / "config.json", tmp_path / "data")
    database = MemeDB(config.db_path)
    manifest_calls = []
    service = LibraryService(config, MemeDbLibraryPort(database), lambda: manifest_calls.append("built"))
    first = _meme(database, "first")
    second = _meme(database, "second")
    target = database.create_collection("target")
    other = database.create_collection("other")
    database.set_collection_members(target, [first, second])
    database.add_to_collection(second, other)

    # When: the target membership order changes through the application service
    result = service.reorder_collection_members(target, [second, first])

    # Then: target order changes without removing the shared membership elsewhere
    assert result is True
    assert [row["id"] for row in database.search(collection_id=target)] == [second, first]
    assert [row["id"] for row in database.search(collection_id=other)] == [second]
    assert manifest_calls == ["built"]
