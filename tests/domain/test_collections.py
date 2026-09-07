import pytest

from ohmymeme.core.domain import (
    Collection,
    CollectionCycleError,
    CollectionGraph,
    CollectionId,
    CollectionName,
    DuplicateCollectionError,
    UnknownCollectionParentError,
)


def _collection(identifier, name, parent_id=None):
    return Collection(CollectionId(identifier), CollectionName(name), parent_id)


def test_collection_graph_rejects_duplicate_identifiers():
    # Given: two collection records with the same persistent identity
    # When: a collection graph is constructed
    # Then: duplicate parentage cannot be represented
    with pytest.raises(DuplicateCollectionError) as captured:
        CollectionGraph((_collection(1, "cats"), _collection(1, "dogs")))

    assert captured.value.collection_id == CollectionId(1)
    assert str(captured.value) == "duplicate_collection:1"


def test_collection_graph_rejects_missing_parent():
    # Given: a collection whose parent is absent
    # When: the graph is constructed
    # Then: it fails before callers can traverse incomplete hierarchy
    with pytest.raises(UnknownCollectionParentError) as captured:
        CollectionGraph((_collection(2, "cats", CollectionId(1)),))

    assert captured.value.collection_id == CollectionId(2)
    assert captured.value.parent_id == CollectionId(1)


def test_collection_graph_rejects_cycles_deterministically():
    # Given: two collections that point to one another
    # When: the graph validates parentage
    # Then: the exact cycle is stable regardless of caller mutation
    collections = [
        _collection(2, "child", CollectionId(1)),
        _collection(1, "parent", CollectionId(2)),
    ]
    with pytest.raises(CollectionCycleError) as captured:
        CollectionGraph(collections)

    collections.clear()
    assert captured.value.path == (CollectionId(1), CollectionId(2), CollectionId(1))
    assert str(captured.value) == "collection_cycle:1>2>1"


def test_collection_graph_keeps_children_in_stable_sort_order():
    # Given: an unordered collection input sequence
    # When: the graph is built
    # Then: root and child traversal is ordered by the explicit collection order
    graph = CollectionGraph(
        (
            _collection(3, "third"),
            _collection(1, "first"),
            _collection(2, "second", CollectionId(1)),
        )
    )

    assert graph.roots == (CollectionId(1), CollectionId(3))
    assert graph.children_of(CollectionId(1)) == (CollectionId(2),)
