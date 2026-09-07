from dataclasses import dataclass

from .errors import InvalidDomainValue


def _require_positive_integer(value: int, field: str) -> None:
    if type(value) is not int:
        raise InvalidDomainValue(field, "must be an integer")
    if value < 1:
        raise InvalidDomainValue(field, "must be positive")


def _require_text(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise InvalidDomainValue(field, "must be a string")
    return value


@dataclass(frozen=True, slots=True)
class MemeId:
    value: int

    def __post_init__(self) -> None:
        _require_positive_integer(self.value, "meme_id")


@dataclass(frozen=True, slots=True)
class CollectionId:
    value: int

    def __post_init__(self) -> None:
        _require_positive_integer(self.value, "collection_id")


@dataclass(frozen=True, slots=True)
class TagId:
    value: int

    def __post_init__(self) -> None:
        _require_positive_integer(self.value, "tag_id")


@dataclass(frozen=True, slots=True)
class TaskId:
    value: str

    def __post_init__(self) -> None:
        normalized = _require_text(self.value, "task_id").strip()
        if not normalized:
            raise InvalidDomainValue("task_id", "must not be empty")
        object.__setattr__(self, "value", normalized)


@dataclass(frozen=True, slots=True)
class MemeFilename:
    value: str

    def __post_init__(self) -> None:
        value = _require_text(self.value, "filename")
        if (
            not value
            or value in (".", "..")
            or value.startswith((".", "/", "\\", "~"))
            or "/" in value
            or "\\" in value
        ):
            raise InvalidDomainValue("filename", "must be a safe basename")


@dataclass(frozen=True, slots=True)
class Sha256:
    value: str

    def __post_init__(self) -> None:
        value = _require_text(self.value, "sha256")
        is_lower_hex = all(character in "0123456789abcdef" for character in value)
        if len(value) != 64 or not is_lower_hex:
            raise InvalidDomainValue(
                "sha256", "must be 64 lowercase hexadecimal characters"
            )


@dataclass(frozen=True, slots=True)
class TagName:
    value: str

    def __post_init__(self) -> None:
        normalized = _require_text(self.value, "tag").strip()
        if not normalized:
            raise InvalidDomainValue("tag", "must not be empty")
        object.__setattr__(self, "value", normalized)


@dataclass(frozen=True, slots=True)
class CollectionName:
    value: str

    def __post_init__(self) -> None:
        normalized = _require_text(self.value, "collection").strip()
        if not normalized:
            raise InvalidDomainValue("collection", "must not be empty")
        object.__setattr__(self, "value", normalized)
