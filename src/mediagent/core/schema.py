"""Small JSON-schema-compatible validator for tool inputs."""

from __future__ import annotations

from typing import Any


def validate_input(schema: dict[str, Any], data: Any) -> list[str]:
    return _validate(schema, data, "$")


def _validate(schema: dict[str, Any], data: Any, path: str) -> list[str]:
    errors: list[str] = []
    expected_type = schema.get("type")
    if expected_type and not _matches_type(expected_type, data):
        errors.append(f"{path}: expected {expected_type}")
        return errors

    enum = schema.get("enum")
    if enum is not None and data not in enum:
        errors.append(f"{path}: expected one of {enum!r}")

    if expected_type == "object":
        required = schema.get("required", [])
        if isinstance(data, dict):
            for key in required:
                if key not in data:
                    errors.append(f"{path}.{key}: required")
            for group in schema.get("required_any", []):
                if not any(key in data and data[key] not in (None, "") for key in group):
                    errors.append(f"{path}: one of {group!r} is required")
            for group in schema.get("required_all_or_none", []):
                present = [key for key in group if key in data and data[key] not in (None, "")]
                if present and len(present) != len(group):
                    errors.append(f"{path}: fields {group!r} must be provided together")
            for rule in schema.get("required_with", []):
                trigger = rule.get("field")
                required_with = rule.get("required", [])
                if trigger in data and data[trigger] not in (None, ""):
                    for key in required_with:
                        if key not in data or data[key] in (None, ""):
                            errors.append(f"{path}.{key}: required with {trigger}")
            properties = schema.get("properties", {})
            for key, value in data.items():
                child_schema = properties.get(key)
                if child_schema:
                    errors.extend(_validate(child_schema, value, f"{path}.{key}"))

    if expected_type == "array":
        item_schema = schema.get("items")
        if item_schema and isinstance(data, list):
            for index, item in enumerate(data):
                errors.extend(_validate(item_schema, item, f"{path}[{index}]"))

    return errors


def _matches_type(expected_type: str | list[str], data: Any) -> bool:
    if isinstance(expected_type, list):
        return any(_matches_type(item, data) for item in expected_type)
    if expected_type == "object":
        return isinstance(data, dict)
    if expected_type == "array":
        return isinstance(data, list)
    if expected_type == "string":
        return isinstance(data, str)
    if expected_type == "boolean":
        return isinstance(data, bool)
    if expected_type == "integer":
        return isinstance(data, int) and not isinstance(data, bool)
    if expected_type == "number":
        return (isinstance(data, int | float)) and not isinstance(data, bool)
    if expected_type == "null":
        return data is None
    return True
