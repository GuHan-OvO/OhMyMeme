import pytest

from ohmymeme.app.manifest_service import ManifestService, ManifestValidationError
from ohmymeme.services.sync.service import _publish_heartbeat


def _manifest() -> bytes:
    return b'''{
      "version": 3,
      "memes": [
        {"filename": "first.png", "name": "first", "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "file_size": 1, "mtime": "1", "sort_order": 0},
        {"filename": "second.png", "name": "second", "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "file_size": 1, "mtime": "2", "sort_order": 1}
      ],
      "collections": [
        {"name": "Root", "filenames": ["first.png"], "children": [
          {"name": "Middle", "filenames": [], "children": [
            {"name": "Leaf", "filenames": ["second.png"]}
          ]}
        ]}
      ]
    }'''


def test_parse_normalizes_v2_and_preserves_three_level_collection_order():
    service = ManifestService()

    projection = service.parse_json(_manifest())
    legacy = service.parse_json(
        b'{"version":2,"memes":[],"collections":[{"name":"Legacy","filenames":[]}]}'
    )

    assert projection.to_data()["collections"][0]["children"][0]["children"][0][
        "name"
    ] == "Leaf"
    assert [entry["filename"] for entry in projection.to_data()["memes"]] == [
        "first.png",
        "second.png",
    ]
    assert legacy.to_data() == {
        "version": 3,
        "memes": [],
        "collections": [{"name": "Legacy", "filenames": []}],
    }


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b'{"version":3,"version":3,"memes":[],"collections":[]}', "duplicate_key"),
        (b'{"version":3,"memes":[null],"collections":[]}', "entry_not_object"),
        (b'{"version":3,"memes":[{"filename":"../bad.png","sort_order":0}],"collections":[]}', "unsafe_filename"),
        (b'{"version":3,"memes":[{"filename":"a.png","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","sort_order":0},{"filename":"a.png","sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","sort_order":1}],"collections":[]}', "duplicate_filename"),
        (b'{"version":3,"memes":[],"collections":[{"name":"Foo","filenames":[]},{"name":"foo","filenames":[]}]}', "collection_case_conflict"),
        (b'{"version":4,"memes":[],"collections":[]}', "unsupported_version"),
        (b'{"version":3,"memes":[],"collections":[{"name":"bad\\u0000","filenames":[]}]}', "control_character"),
        (b'{"version":3,"memes":[],"collections":[{"name":"\\ud800","filenames":[]}]}', "unpaired_surrogate"),
    ],
)
def test_parse_rejects_unsafe_or_ambiguous_input_without_projection(raw, code):
    service = ManifestService()

    with pytest.raises(ManifestValidationError) as raised:
        service.parse_json(raw)

    assert raised.value.code == code


def test_canonical_bytes_match_jcs_object_sorting_and_number_semantics():
    service = ManifestService()

    canonical = service.canonical_bytes(
        {"\U0001f600": -0.0, "a": [1.0, 1e-6, 1e-7], "\u20ac": "\u000f"}
    )

    assert canonical == b'{"a":[1,0.000001,1e-7],"\xe2\x82\xac":"\\u000f","\xf0\x9f\x98\x80":0}'


def test_parse_ignores_unknown_fields_but_keeps_whitelisted_public_shape():
    service = ManifestService()

    projection = service.parse_json(
        b'{"version":3,"ignored":true,"memes":[{"filename":"a.png","name":"a","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","file_size":0,"mtime":"","sort_order":0,"extra":1}],"collections":[{"name":"A","filenames":["a.png"],"extra":1}]}'
    )

    assert projection.to_data() == {
        "version": 3,
        "memes": [
            {
                "filename": "a.png",
                "name": "a",
                "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "file_size": 0,
                "mtime": "", "sort_order": 0,
            }
        ],
        "collections": [{"name": "A", "filenames": ["a.png"]}],
    }


@pytest.mark.parametrize("sha256", ("a" * 63, "A" * 64, "g" * 64))
def test_parse_rejects_noncanonical_remote_sha256(sha256):
    # Given: a remote v3 entry with a non-canonical digest
    manifest = {
        "version": 3,
        "memes": [
            {
                "filename": "asset.png",
                "name": "asset",
                "sha256": sha256,
                "file_size": 1,
                "mtime": "",
                "sort_order": 0,
            }
        ],
        "collections": [],
    }

    # When/Then: parsing rejects it before any pull can stage bytes
    with pytest.raises(ManifestValidationError) as raised:
        ManifestService().parse_data(manifest, strict_hash=True)

    assert raised.value.code == "invalid_sha256"


@pytest.mark.parametrize(
    ("entries", "code"),
    [
        ([{"filename": "a.png"}], "missing_sort_order"),
        ([{"filename": "a.png", "sha256": "a" * 64, "sort_order": 0}, {"filename": "b.png", "sha256": "b" * 64, "sort_order": 0}], "duplicate_sort_order"),
        ([{"filename": "A.png", "sha256": "a" * 64, "sort_order": 0}, {"filename": "a.png", "sha256": "b" * 64, "sort_order": 1}], "filename_case_conflict"),
    ],
)
def test_parse_requires_unique_sibling_sort_and_filename_identity(entries, code):
    service = ManifestService()

    with pytest.raises(ManifestValidationError) as raised:
        service.parse_data({"version": 3, "memes": entries, "collections": []})

    assert raised.value.code == code


def test_failed_heartbeat_contains_only_confirmed_uploaded_entries(tmp_path):
    local = {
        "version": 3,
        "memes": [
                {"filename": "confirmed.png", "name": "confirmed", "sha256": "a" * 64, "file_size": 0, "mtime": "", "sort_order": 0},
                {"filename": "pending.png", "name": "pending", "sha256": "b" * 64, "file_size": 0, "mtime": "", "sort_order": 1},
        ],
        "collections": [{"name": "Root", "filenames": ["confirmed.png", "pending.png"]}],
    }

    class Backend:
        def __init__(self):
            self.payload = None

        def upload_file(self, path, _remote_path):
            self.payload = ManifestService().parse_json(path.read_bytes()).to_data()
            return False

    backend = Backend()
    _publish_heartbeat(backend, "meme-index.json", local, {}, ["confirmed.png"], tmp_path)

    assert [entry["filename"] for entry in backend.payload["memes"]] == ["confirmed.png"]
    assert backend.payload["collections"] == [{"name": "Root", "filenames": ["confirmed.png"]}]
