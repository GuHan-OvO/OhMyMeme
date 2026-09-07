import pytest

from ohmymeme.core.domain import (
    CollectionId,
    DuplicateValueError,
    InCollection,
    InvalidDomainValue,
    MemeId,
    MemeQuery,
    Page,
    StableOrder,
    TagFilter,
    TagName,
)


def test_tag_filter_and_query_are_deterministic_and_immutable():
    # Given: mutable tags entered in a noncanonical order
    # When: a query parses the tag intersection and paging values
    # Then: later mutation cannot alter the query and tag order is deterministic
    supplied_tags = [TagName("zebra"), TagName("ant")]
    query = MemeQuery(
        keyword="  cats  ",
        tags=TagFilter.from_tags(supplied_tags),
        scope=InCollection(CollectionId(3)),
        page=Page(offset=0, limit=200),
    )
    supplied_tags.append(TagName("dog"))

    assert query.keyword == "cats"
    assert query.tags.values == (TagName("ant"), TagName("zebra"))
    assert query.scope.collection_id == CollectionId(3)
    assert query.page.offset == 0
    assert query.page.limit == 200


def test_tag_filter_rejects_case_insensitive_duplicates():
    # Given: a tag intersection with two spellings of one persisted tag
    # When: the filter is parsed
    # Then: it cannot encode an order-dependent database query
    with pytest.raises(DuplicateValueError) as captured:
        TagFilter.from_tags((TagName("cats"), TagName("CATS")))

    assert captured.value.field == "tags"


@pytest.mark.parametrize(("offset", "limit"), ((-1, 1), (0, 0)))
def test_page_rejects_invalid_bounds(offset, limit):
    # Given: pagination bounds outside the query contract
    # When: the page value is constructed
    # Then: invalid database limits cannot reach a caller
    with pytest.raises(InvalidDomainValue):
        Page(offset=offset, limit=limit)


def test_stable_order_freezes_input_and_rejects_duplicates():
    # Given: a mutable user-defined meme ordering
    # When: it is converted to a sorting value
    # Then: positions stay deterministic and duplicate IDs are rejected
    supplied_ids = [MemeId(7), MemeId(2)]
    ordering = StableOrder.from_ids(supplied_ids)
    supplied_ids.reverse()

    assert tuple((item.identifier, item.position) for item in ordering.positions) == (
        (MemeId(7), 0),
        (MemeId(2), 1),
    )
    with pytest.raises(DuplicateValueError):
        StableOrder.from_ids((MemeId(2), MemeId(2)))
