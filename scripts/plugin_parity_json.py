# pyright: basic

import json
import re


def reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path):
    value = json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates
    )
    if not isinstance(value, dict):
        raise ValueError(f"root: expected object in {path}")
    return value


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    path.write_text(data + "\n", encoding="utf-8", newline="\n")


def append_error(errors, condition, message):
    if condition:
        errors.append(message)


def matches_type(value, expected):
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
    }.get(expected, True)


def validate_schema(value, schema, root, path):
    errors, prefix = [], path or "root"
    if "$ref" in schema:
        target = root
        for part in schema["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        return validate_schema(value, target, root, path)
    for child in schema.get("allOf", []):
        errors.extend(validate_schema(value, child, root, path))
    expected = schema.get("type")
    if expected and not matches_type(value, expected):
        return [f"{prefix}: expected {expected}"]
    if "const" in schema and (
        type(value) is not type(schema["const"]) or value != schema["const"]
    ):
        errors.append(f"{prefix}: expected {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{prefix}: value is not an allowed enum member")
    if isinstance(value, dict):
        for field in schema.get("required", []):
            if field not in value:
                errors.append(
                    f"{path + '.' if path else ''}{field}: missing required field"
                )
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for field in value:
                if field not in properties:
                    errors.append(
                        f"{path + '.' if path else ''}{field}: unexpected property"
                    )
        for field, child in properties.items():
            if field in value:
                errors.extend(
                    validate_schema(
                        value[field], child, root, f"{path}.{field}" if path else field
                    )
                )
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{prefix}: requires at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{prefix}: allows at most {schema['maxItems']} items")
        if "items" in schema:
            for index, item in enumerate(value):
                errors.extend(
                    validate_schema(item, schema["items"], root, f"{path}[{index}]")
                )
        if "contains" in schema and not any(
            not validate_schema(item, schema["contains"], root, f"{path}[]")
            for item in value
        ):
            errors.append(f"{prefix}: missing required contained value")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(
                f"{prefix}: requires at least {schema['minLength']} characters"
            )
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{prefix}: value does not match pattern")
    return errors
