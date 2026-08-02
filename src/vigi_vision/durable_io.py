"""Strict JSON and trusted local-path boundaries for durable artifacts."""

from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Final, NoReturn, TypeAlias, cast

from pydantic import BeforeValidator, JsonValue, TypeAdapter, ValidationError

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_UTC_TIMESTAMP_PATTERN: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_WINDOWS_REPARSE_POINT: Final = 0x400


class DurableJsonError(ValueError):
    """Raised when a durable JSON document cannot be parsed unambiguously."""


def load_durable_json_object(raw: str) -> dict[str, JsonValue]:
    """Parse one strict JSON object, rejecting duplicate and non-finite values."""
    try:
        parsed = cast(
            "object",
            json.loads(
                raw,
                object_pairs_hook=_reject_duplicate_keys,
                parse_float=_parse_finite_float,
                parse_constant=_reject_non_json_number,
            ),
        )
        return _JSON_OBJECT.validate_python(parsed)
    except (DurableJsonError, TypeError, ValueError, ValidationError):
        raise DurableJsonError from None


def parse_canonical_utc(value: datetime | str) -> datetime:
    """Parse a whole-second UTC value from a typed value or canonical JSON text."""
    if isinstance(value, str):
        if _UTC_TIMESTAMP_PATTERN.fullmatch(value) is None:
            raise ValueError
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    else:
        return value
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError
    return parsed.astimezone(timezone.utc)


def is_safe_contained_path(root: Path, target: Path, *, require_target: bool = False) -> bool:
    """Return whether an existing path tree stays inside a trusted root."""
    root_absolute = _absolute(root)
    target_absolute = _absolute(target)
    try:
        _ = target_absolute.relative_to(root_absolute)
    except ValueError:
        return False
    if not _components_are_safe(target_absolute):
        return False
    try:
        if require_target and not target_absolute.exists() and not target_absolute.is_symlink():
            return False
        if not root_absolute.exists() or root_absolute.is_symlink():
            return False
        resolved_root = root_absolute.resolve(strict=True)
        resolved_target = target_absolute.resolve(strict=require_target)
        _ = resolved_target.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def is_safe_path(path: Path, *, require_target: bool = False) -> bool:
    """Return whether every existing component from the filesystem anchor is safe."""
    absolute = _absolute(path)
    try:
        if require_target and not absolute.exists() and not absolute.is_symlink():
            return False
    except OSError:
        return False
    return _components_are_safe(absolute)


def _reject_duplicate_keys(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    values: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in values:
            raise DurableJsonError
        values[key] = value
    return values


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise DurableJsonError
    return parsed


def _reject_non_json_number(value: str) -> NoReturn:
    del value
    raise DurableJsonError


def _absolute(path: Path) -> Path:
    return path.absolute()


def _components_are_safe(target: Path) -> bool:
    components: list[Path] = []
    current = target
    anchor = Path(target.anchor)
    while True:
        if current == anchor:
            break
        components.append(current)
        parent = current.parent
        if parent == current:
            return False
        current = parent
    return all(not _is_path_indirection(component) for component in reversed(components))


def _is_path_indirection(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        if not path.exists():
            return False
        mount_point = False
        if path != Path(path.anchor):
            try:
                mount_point = path.is_mount()
            except NotImplementedError:
                mount_point = False
        if os.name != "nt":
            return mount_point
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attributes & _WINDOWS_REPARSE_POINT) or mount_point
    except (AttributeError, OSError, RuntimeError):
        return True


CanonicalUtc: TypeAlias = Annotated[datetime, BeforeValidator(parse_canonical_utc)]
