"""Pydantic models for the stable desktop bridge ABI."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, RootModel, TypeAdapter

type JsonValue = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)
JsonObject: TypeAlias = dict[str, JsonValue]


class BridgeModel(BaseModel):
    """Base model for immutable, closed bridge payloads."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


type BridgeInput = JsonValue | BridgeModel


class BridgeError(BridgeModel):
    """Structured internal error information."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    details: JsonObject = Field(default_factory=dict)


class BridgeInvocation(BridgeModel):
    """A JSON-safe method invocation used by the runtime validator."""

    method: str = Field(min_length=1)
    args: tuple[JsonValue, ...] = ()


class BridgeEnvelope(BridgeModel):
    """Internal result envelope before legacy shape projection."""

    ok: bool
    value: JsonValue | None = None
    error: BridgeError | None = None


class JsonValueModel(RootModel[JsonValue]):
    """Schema root for values returned by compatibility methods."""

    model_config = ConfigDict(frozen=True, strict=True)


class JsonObjectModel(RootModel[JsonObject]):
    """Schema root for open, JSON-compatible bridge dictionaries."""

    model_config = ConfigDict(frozen=True, strict=True)


class JsonListModel(RootModel[list[JsonValue]]):
    """Schema root for JSON-compatible bridge lists."""

    model_config = ConfigDict(frozen=True, strict=True)


class JsonBoolModel(RootModel[bool]):
    """Schema root for boolean compatibility results."""

    model_config = ConfigDict(frozen=True, strict=True)


class JsonStringModel(RootModel[str]):
    """Schema root for string compatibility results."""

    model_config = ConfigDict(frozen=True, strict=True)


class Meme(BridgeModel):
    """Main-window meme card DTO."""

    id: int
    filename: str
    name: str
    file_hash: str
    from_stego: int | None = None
    width: int
    height: int
    mime_type: str
    is_gif: bool | None = None
    is_animated: bool | None = None
    favorited: bool | None = None
    auto_play_gif: bool | None = None
    hover_to_play: bool | None = None


class Collection(BridgeModel):
    """Main-window collection tree DTO."""

    id: int
    name: str
    count: int
    parent_id: int | None = None
    children: list["Collection"] = Field(default_factory=list)


class Tag(BridgeModel):
    """Main-window tag DTO."""

    name: str
    count: int


class Page(BridgeModel):
    """Paged query DTO shared by bridge consumers."""

    items: list[JsonValue]
    total: int
    limit: int
    offset: int


class CopyResult(BridgeModel):
    """Historical copy result envelope."""

    ok: bool
    status: Literal["copied", "copy_failed"]


class ImportResult(BridgeModel):
    """Historical import result envelope."""

    ok: bool
    imported: int | None = None
    rejected: int | None = None
    cancelled: bool | None = None
    error: str | None = None


class SettingsData(BridgeModel):
    """Complete settings DTO returned to the settings window."""

    version: str
    hotkey: str
    hotkey_show_at_mouse: bool
    auto_start: bool
    silent_start: bool
    language: str
    cache_max_size_mb: int
    thumbnail_size: int
    cache_dir: str
    sync_auto_fetch_index: bool
    sync_auto_sync: bool
    sync_type: str
    sync_interval_minutes: int
    sync_delete_remote: bool
    sync_remove_local: bool
    sync_hide_upload_warning: bool
    sync_threads: int
    show_upload_progress: bool
    show_upload_done: bool
    show_download_progress: bool
    show_download_done: bool
    ftp_host: str
    ftp_port: int
    ftp_user: str
    ftp_password: str
    ftp_path: str
    s3_endpoint: str
    s3_region: str
    s3_bucket: str
    s3_access_key: str
    s3_secret_key: str
    s3_path: str
    s3_addressing_style: str
    s3_signature_version: str
    r2_account_id: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_bucket: str
    r2_path: str
    webdav_url: str
    webdav_user: str
    webdav_password: str
    webdav_path: str
    webdav_timeout: int
    copy_resize_mode: int
    copy_resize_max: int
    lan_port: int
    lan_secret: str
    tg_tdata_path: str
    theme: str
    window_x: int
    window_y: int
    auto_play_gif: bool
    hover_to_play: bool
    try_original_image: bool
    show_uncategorized: bool
    record_recent_use: bool
    show_startup_animation: bool
    disabled_plugins: list[str]


class SettingsPatch(BridgeModel):
    """Known partial settings input accepted by save_settings."""

    version: str | None = None
    hotkey: str | None = None
    hotkey_show_at_mouse: bool | None = None
    auto_start: bool | None = None
    silent_start: bool | None = None
    language: str | None = None
    cache_max_size_mb: int | None = None
    thumbnail_size: int | None = None
    cache_dir: str | None = None
    sync_auto_fetch_index: bool | None = None
    sync_auto_sync: bool | None = None
    sync_type: str | None = None
    sync_interval_minutes: int | None = None
    sync_delete_remote: bool | None = None
    sync_remove_local: bool | None = None
    sync_hide_upload_warning: bool | None = None
    sync_threads: int | None = None
    show_upload_progress: bool | None = None
    show_upload_done: bool | None = None
    show_download_progress: bool | None = None
    show_download_done: bool | None = None
    ftp_host: str | None = None
    ftp_port: int | None = None
    ftp_user: str | None = None
    ftp_password: str | None = None
    ftp_path: str | None = None
    s3_endpoint: str | None = None
    s3_region: str | None = None
    s3_bucket: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_path: str | None = None
    s3_addressing_style: str | None = None
    s3_signature_version: str | None = None
    r2_account_id: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None
    r2_bucket: str | None = None
    r2_path: str | None = None
    webdav_url: str | None = None
    webdav_user: str | None = None
    webdav_password: str | None = None
    webdav_path: str | None = None
    webdav_timeout: int | None = None
    copy_resize_mode: int | None = None
    copy_resize_max: int | None = None
    lan_port: int | None = None
    lan_secret: str | None = None
    tg_tdata_path: str | None = None
    theme: str | None = None
    window_x: int | None = None
    window_y: int | None = None
    auto_play_gif: bool | None = None
    hover_to_play: bool | None = None
    try_original_image: bool | None = None
    show_uncategorized: bool | None = None
    record_recent_use: bool | None = None
    show_startup_animation: bool | None = None
    disabled_plugins: list[str] | None = None


class ChildCollection(BridgeModel):
    """Minimal child collection DTO returned by the legacy bridge."""

    id: int
    name: str


class SearchCollection(BridgeModel):
    """Collection search DTO returned by the add-to-group dialog."""

    id: int
    name: str
    depth: int


class BridgeInitData(BridgeModel):
    """Initial main-window payload."""

    memes: list[Meme]
    tags: list[str]
    collections: list[Collection]
    show_startup_animation: bool
    startup_bg_color: str


class MemeList(RootModel[list[Meme]]):
    """Validated list of meme DTOs."""


class StringList(RootModel[list[str]]):
    """Validated list of tag strings."""


class CollectionList(RootModel[list[Collection]]):
    """Validated collection tree list."""


class ChildCollectionList(RootModel[list[ChildCollection]]):
    """Validated child collection list."""


class SearchCollectionList(RootModel[list[SearchCollection]]):
    """Validated collection search list."""


class MemePaths(RootModel[dict[str, str]]):
    """Validated filename-to-path map."""


class CollectionMember(BridgeModel):
    """Minimal member DTO compatible with legacy collection responses."""

    id: int
    filename: str | None = None
    name: str | None = None
    file_hash: str | None = None
    width: int | None = None
    height: int | None = None
    mime_type: str | None = None
    from_stego: int | None = None
    is_gif: bool | None = None
    is_animated: bool | None = None
    favorited: bool | None = None
    auto_play_gif: bool | None = None
    hover_to_play: bool | None = None


class CollectionMemberList(RootModel[list[CollectionMember]]):
    """Validated collection member list."""


@dataclass(frozen=True, slots=True)
class MethodSpec:
    """Runtime validation adapters for one public bridge method."""

    result: TypeAdapter[JsonValue]
    legacy_failure: JsonValue | None = None
    arguments: tuple[TypeAdapter[JsonValue], ...] = ()


class BridgeContractError(ValueError):
    """Raised when a bridge payload violates its generated contract."""

    def __init__(self, method: str, phase: Literal["input", "output"], detail: str):
        self.method = method
        self.phase = phase
        self.detail = detail
        super().__init__(f"bridge {phase} rejected for {method}: {detail}")


_JSON_VALUE = TypeAdapter(JsonValue)
_JSON_LIST = TypeAdapter(list[JsonValue])
_JSON_OBJECT = TypeAdapter(JsonObject)
_BOOL = TypeAdapter(bool)
_INT = TypeAdapter(int)
_STRING = TypeAdapter(str)
_NONE = TypeAdapter(None)


class BridgeContract:
    """Validate boundary inputs/outputs while preserving legacy sentinels."""

    def __init__(self, specs: Mapping[str, MethodSpec]) -> None:
        self._specs = dict(specs)

    def validate_input(
        self, method: str, args: Sequence[JsonValue]
    ) -> tuple[BridgeInput, ...]:
        spec = self._specs.get(method)
        if spec is None:
            raise BridgeContractError(method, "input", "unknown method")
        try:
            invocation = BridgeInvocation(method=method, args=tuple(args))
            if len(invocation.args) != len(spec.arguments):
                raise ValueError("argument count mismatch")
            checked = tuple(
                adapter.validate_python(value, strict=True)
                for adapter, value in zip(spec.arguments, invocation.args, strict=True)
            )
        except ValueError as error:
            raise BridgeContractError(method, "input", str(error)) from error
        return checked

    def validate_output(self, method: str, value: JsonValue) -> JsonValue:
        spec = self._specs.get(method)
        if spec is None:
            raise BridgeContractError(method, "output", "unknown method")
        try:
            validated = spec.result.validate_python(value, strict=True)
        except ValueError as error:
            raise BridgeContractError(method, "output", str(error)) from error
        if isinstance(validated, BaseModel):
            return validated.model_dump(mode="json", exclude_none=True)
        return validated

    @property
    def methods(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))


def _spec(
    result: TypeAdapter[JsonValue],
    failure: JsonValue | None = None,
    *arguments: TypeAdapter[JsonValue],
) -> MethodSpec:
    return MethodSpec(result, failure, tuple(arguments))


def _build_specs(settings_surface: bool = False) -> dict[str, MethodSpec]:
    """Build the stable primitive/result validation table."""
    main_lists = {
        "search_memes",
        "get_tags",
        "get_meme_tags",
        "get_collections",
        "get_child_collections",
        "search_collections",
        "get_collection_members",
    }
    main_bools = {
        "toggle_favorite",
        "is_favorite",
        "rename_meme",
        "set_meme_tags",
        "delete_meme",
        "add_to_collection",
        "add_to_existing_collection",
        "set_collection_members",
        "reorder_memes",
        "reorder_collections",
        "reorder_collection_members",
        "delete_collection",
        "rename_collection",
        "record_meme_use",
        "remove_from_recent",
        "clear_recent",
        "remove_from_collection",
        "rescan_cache",
        "start_download",
        "run_downloaded_installer",
        "import_memes",
        "start_window_drag",
        "start_native_drag",
        "open_adb_folder",
        "open_adb_help",
    }
    main_strings = {"sync_test"}
    settings_strings = {"lan_get_ip", "get_current_version", "sync_test"}
    specs: dict[str, MethodSpec] = {}
    integer = TypeAdapter(int)
    string = TypeAdapter(str)
    boolean = TypeAdapter(bool)
    integer_list = TypeAdapter(list[int])
    string_list_or_none = TypeAdapter(list[str] | None)
    integer_or_none = TypeAdapter(int | None)
    string_or_none = TypeAdapter(str | None)
    boolean_or_none = TypeAdapter(bool | None)
    settings_patch = TypeAdapter(SettingsPatch)
    empty: tuple[TypeAdapter[JsonValue], ...] = ()
    argument_map: dict[str, tuple[TypeAdapter[JsonValue], ...]] = {
        "search_memes": (
            string,
            string_list_or_none,
            integer_or_none,
            integer,
            integer,
        ),
        "count_memes": (string, string_list_or_none, integer_or_none),
        "get_meme_tags": (integer,),
        "set_meme_tags": (integer, string_list_or_none),
        "get_meme_path": (integer,),
        "get_meme_paths": (integer_list,),
        "start_native_drag": (integer,),
        "copy_meme": (integer,),
        "toggle_favorite": (integer,),
        "is_favorite": (integer,),
        "rename_meme": (
            integer,
            string,
        ),
        "delete_meme": (integer,),
        "delete_memes": (integer_list,),
        "get_child_collections": (integer,),
        "search_collections": (string,),
        "get_collection_members": (integer,),
        "add_to_collection": (
            integer,
            string,
        ),
        "add_to_existing_collection": (
            integer,
            integer,
        ),
        "set_collection_members": (
            integer,
            integer_list,
        ),
        "set_collection_members_new": (
            string,
            integer_list,
        ),
        "reorder_memes": (integer_list,),
        "reorder_collections": (integer_list,),
        "reorder_collection_members": (
            integer,
            integer_list,
        ),
        "delete_collection": (integer,),
        "rename_collection": (
            integer,
            string,
        ),
        "create_subcollection": (
            string,
            integer,
        ),
        "record_meme_use": (integer,),
        "remove_from_recent": (integer,),
        "remove_from_collection": (
            integer,
            integer,
        ),
        "log": (
            string,
            string,
        ),
        "check_update": (
            boolean,
            boolean,
        ),
        "start_download": (string,),
        "download_update": (string,),
        "download_original_image": (string,),
        "import_folder": (boolean,),
        "lan_confirm_device": (
            boolean,
            string,
        ),
        "save_settings": (settings_patch,),
        "move_window": (
            integer,
            integer,
        ),
        "start_window_drag": (
            integer,
            integer,
            integer,
        ),
        "lan_start": (integer_or_none, string_or_none),
        "lan_set_allow_secret_config": (boolean,),
        "start_tg_import": (string_or_none, string, boolean),
        "start_douyin_import": (string,),
        "pick_wechat_root": (),
        "inspect_wechat_environment": (string_or_none,),
        "list_wechat_stickers": (string, string_or_none),
        "start_wechat_import": (string_or_none, boolean, string_or_none),
        "apply_storage_dir": (
            string,
            boolean,
        ),
        "qqnt_default_dir": (
            string,
            string,
        ),
        "qqnt_start": (string, string, boolean, boolean),
        "qqnt_open_dir": (string,),
        "sync_push": (boolean_or_none,) if settings_surface else (),
        "sync_pull": (boolean_or_none,) if settings_surface else (),
        "get_remote_orphans": (boolean,),
    }
    for name in main_lists:
        specs[name] = _spec(_JSON_LIST, [], *argument_map.get(name, empty))
    for name in main_bools:
        specs[name] = _spec(_BOOL, False, *argument_map.get(name, empty))
    for name in main_strings | settings_strings:
        specs[name] = _spec(_STRING, "", *argument_map.get(name, empty))
    for name in {
        "count_memes",
        "get_meme_path",
        "get_meme_paths",
        "get_init_data",
        "copy_meme",
        "delete_memes",
        "set_collection_members_new",
        "create_subcollection",
        "check_update",
        "get_download_progress",
        "download_update",
        "check_connectivity",
        "download_original_image",
        "get_sync_progress",
        "sync_push",
        "sync_pull",
        "run_auto_sync",
        "import_folder",
        "import_from_clipboard",
        "open_settings",
        "lan_confirm_device",
        "get_settings",
        "reset_settings",
        "hide_window",
    }:
        specs[name] = _spec(_JSON_VALUE, None, *argument_map.get(name, empty))
    for name in {
        "check_connectivity",
        "lan_start",
        "lan_stop",
        "lan_get_status",
        "lan_set_allow_secret_config",
        "get_settings",
        "refresh_memes",
        "refresh_tags",
        "refresh_collections",
        "reset_settings",
        "start_qq_import",
        "get_qq_import_progress",
        "save_qq_zip",
        "export_logs",
        "pick_tg_tdata",
        "start_tg_import",
        "get_tg_import_progress",
        "start_douyin_import",
        "get_douyin_import_progress",
        "pick_wechat_root",
        "inspect_wechat_environment",
        "list_wechat_stickers",
        "start_wechat_import",
        "get_wechat_import_progress",
        "qqnt_check_env",
        "qqnt_pick_ini",
        "qqnt_pick_userdata",
        "qqnt_pick_base",
        "get_storage_info",
        "pick_storage_dir",
        "apply_storage_dir",
        "qqnt_default_dir",
        "qqnt_start",
        "qqnt_get_progress",
        "qqnt_open_dir",
        "import_memes",
        "check_update",
        "get_download_progress",
        "download_update",
        "get_sync_progress",
        "sync_push",
        "sync_pull",
        "delete_all_local",
        "delete_all_cloud",
        "get_remote_orphans",
        "check_sync_status",
    }:
        specs[name] = _spec(_JSON_OBJECT, {"ok": False}, *argument_map.get(name, empty))
    for name in {
        "save_settings",
        "cancel_qq_import",
        "cancel_tg_import",
        "cancel_douyin_import",
        "cancel_wechat_import",
        "qqnt_cancel",
        "close_settings",
        "move_window",
    }:
        specs[name] = _spec(_NONE, None, *argument_map.get(name, empty))
    specs["get_tags"] = _spec(_JSON_LIST, [], *argument_map.get("get_tags", empty))
    specs["get_collections"] = _spec(
        _JSON_LIST, [], *argument_map.get("get_collections", empty)
    )
    specs["clear_recent"] = _spec(
        _BOOL, False, *argument_map.get("clear_recent", empty)
    )
    specs["rescan_cache"] = _spec(
        _BOOL, False, *argument_map.get("rescan_cache", empty)
    )
    specs["log"] = _spec(_NONE, None, *argument_map["log"])
    specs["search_memes"] = _spec(
        TypeAdapter(MemeList), None, *argument_map["search_memes"]
    )
    specs["get_tags"] = _spec(
        TypeAdapter(StringList), None, *argument_map.get("get_tags", empty)
    )
    specs["get_meme_tags"] = _spec(
        TypeAdapter(StringList), None, *argument_map["get_meme_tags"]
    )
    specs["get_collections"] = _spec(
        TypeAdapter(CollectionList), None, *argument_map.get("get_collections", empty)
    )
    specs["get_child_collections"] = _spec(
        TypeAdapter(ChildCollectionList), None, *argument_map["get_child_collections"]
    )
    specs["search_collections"] = _spec(
        TypeAdapter(SearchCollectionList), None, *argument_map["search_collections"]
    )
    specs["get_collection_members"] = _spec(
        TypeAdapter(CollectionMemberList),
        None,
        *argument_map["get_collection_members"],
    )
    specs["get_init_data"] = _spec(
        TypeAdapter(BridgeInitData), None, *argument_map.get("get_init_data", empty)
    )
    specs["get_meme_paths"] = _spec(
        TypeAdapter(MemePaths), None, *argument_map["get_meme_paths"]
    )
    specs["copy_meme"] = _spec(
        TypeAdapter(CopyResult), None, *argument_map["copy_meme"]
    )
    specs["count_memes"] = _spec(_INT, 0, *argument_map.get("count_memes", empty))
    specs["get_meme_path"] = _spec(
        _STRING, "", *argument_map.get("get_meme_path", empty)
    )
    specs["open_settings"] = _spec(
        _BOOL, False, *argument_map.get("open_settings", empty)
    )
    for name in {
        "open_adb_folder",
        "open_adb_help",
        "qqnt_open_dir",
        "start_download",
        "run_downloaded_installer",
    }:
        specs[name] = _spec(_BOOL, False, *argument_map.get(name, empty))
    for name in {"refresh_memes", "refresh_tags", "refresh_collections"}:
        specs[name] = _spec(_NONE, None, *argument_map.get(name, empty))
    return specs


MAIN_BRIDGE_CONTRACT = BridgeContract(_build_specs())
SETTINGS_BRIDGE_CONTRACT = BridgeContract(_build_specs(True))
BRIDGE_CONTRACT = MAIN_BRIDGE_CONTRACT
BRIDGE_SCHEMA_MODELS = (
    BridgeError,
    BridgeInvocation,
    BridgeEnvelope,
    JsonValueModel,
    JsonObjectModel,
    JsonListModel,
    JsonBoolModel,
    JsonStringModel,
    Meme,
    Collection,
    Tag,
    Page,
    CopyResult,
    ImportResult,
    SettingsData,
    SettingsPatch,
    ChildCollection,
    SearchCollection,
    BridgeInitData,
    MemeList,
    StringList,
    CollectionList,
    ChildCollectionList,
    SearchCollectionList,
    MemePaths,
    CollectionMember,
    CollectionMemberList,
)
