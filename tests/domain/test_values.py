import pytest

from ohmymeme.core.domain import (
    CollectionId,
    CollectionName,
    InvalidDomainValue,
    MemeFilename,
    MemeId,
    Sha256,
    TagId,
    TagName,
    TaskId,
    TaskState,
    UnsupportedTaskStateError,
)


@pytest.mark.parametrize(
    ("factory", "raw"),
    [
        (MemeId, 0),
        (CollectionId, 0),
        (TagId, 0),
        (TaskId, ""),
    ],
)
def test_typed_identifiers_reject_malformed_values(factory, raw):
    # Given: an identifier outside its domain
    # When: the value object is constructed
    # Then: construction fails with a stable domain error
    with pytest.raises(InvalidDomainValue) as captured:
        factory(raw)

    assert captured.value.code == "invalid_domain_value"


@pytest.mark.parametrize(
    ("factory", "raw", "field"),
    (
        (MemeId, True, "meme_id"),
        (MemeId, "1", "meme_id"),
        (CollectionId, False, "collection_id"),
        (CollectionId, 1.5, "collection_id"),
        (TagId, True, "tag_id"),
        (TagId, "1", "tag_id"),
    ),
)
def test_identifier_values_reject_bool_and_non_integer_runtime_inputs(
    factory, raw, field
):
    # Given: a runtime primitive that violates an integer identifier contract
    # When: the identifier is constructed
    # Then: it raises the documented domain error rather than a low-level error
    with pytest.raises(InvalidDomainValue) as captured:
        factory(raw)

    assert captured.value.field == field
    assert captured.value.reason == "must be an integer"
    assert str(captured.value) == f"invalid_domain_value:{field}:must be an integer"


@pytest.mark.parametrize(
    ("factory", "field"),
    (
        (TaskId, "task_id"),
        (MemeFilename, "filename"),
        (TagName, "tag"),
        (CollectionName, "collection"),
        (Sha256, "sha256"),
    ),
)
def test_text_values_reject_non_string_runtime_inputs(factory, field):
    # Given: a runtime primitive that cannot provide text value semantics
    # When: the value object parses it
    # Then: callers receive the stable domain error shape
    with pytest.raises(InvalidDomainValue) as captured:
        factory(1)

    assert captured.value.field == field
    assert captured.value.reason == "must be a string"
    assert str(captured.value) == f"invalid_domain_value:{field}:must be a string"


@pytest.mark.parametrize(
    "raw",
    ("", ".hidden.png", "../escape.png", "folder/meme.png", "C:\\meme.png"),
)
def test_meme_filename_rejects_unsafe_values(raw):
    # Given: a filename that cannot name a cached meme
    # When: it crosses the domain boundary
    # Then: no unsafe filename value exists
    with pytest.raises(InvalidDomainValue) as captured:
        MemeFilename(raw)

    assert captured.value.field == "filename"


@pytest.mark.parametrize("raw", ("", "a" * 63, "g" * 64, "A" * 64))
def test_sha256_rejects_noncanonical_values(raw):
    # Given: an empty, short, non-hex, or noncanonical hash
    # When: the hash value is constructed
    # Then: its failure shape remains deterministic
    with pytest.raises(InvalidDomainValue) as captured:
        Sha256(raw)

    assert captured.value.field == "sha256"
    assert captured.value.reason == "must be 64 lowercase hexadecimal characters"
    assert str(captured.value) == (
        "invalid_domain_value:sha256:must be 64 lowercase hexadecimal characters"
    )


def test_typed_values_preserve_valid_observable_semantics():
    # Given: values accepted by the current cache and tag behavior
    # When: they are parsed into domain values
    # Then: semantic identifier types stay distinct and tags are trimmed
    assert MemeFilename("表情 a.webp").value == "表情 a.webp"
    assert Sha256("a" * 64).value == "a" * 64
    assert TagName("  cats  ").value == "cats"
    assert MemeId(4) != CollectionId(4)


def test_manual_pure_domain_interaction():
    # Given: a caller constructing one cache-facing domain value
    # When: valid and invalid inputs cross the pure-domain boundary
    # Then: the interaction prints deterministic values and typed rejections
    filename = MemeFilename("cat.webp")
    with pytest.raises(InvalidDomainValue) as invalid_hash:
        Sha256("")
    with pytest.raises(UnsupportedTaskStateError) as unknown_state:
        TaskState.parse("paused")

    assert filename.value == "cat.webp"
    assert str(invalid_hash.value) == (
        "invalid_domain_value:sha256:must be 64 lowercase hexadecimal characters"
    )
    assert str(unknown_state.value) == "unsupported_task_state:paused"
    print(f"domain valid filename={filename.value}")
    print(
        "domain rejected "
        f"hash={invalid_hash.value} state={unknown_state.value}"
    )
