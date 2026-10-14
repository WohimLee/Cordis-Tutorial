"""Plugin configuration validation contracts."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Protocol, cast


class ConfigValidator(Protocol):
    """Synchronous adapter used to normalize plugin configuration."""

    def validate(self, value: object) -> object:
        """Return normalized configuration or raise an exception."""


class ValidationError(TypeError):
    """Configuration validation failed before plugin activation."""

    def __init__(self, issues: Sequence[Mapping[str, object]]) -> None:
        self.issues = tuple(issues)
        lines: list[str] = []
        for issue in issues:
            line = f"  - {issue.get('message', '')}"
            path = issue.get("path")
            if isinstance(path, Sequence) and not isinstance(path, (str, bytes)):
                parts = cast(Sequence[object], path)
                line += " (at " + ".".join(str(part) for part in parts) + ")"
            lines.append(line)
        super().__init__("invalid config:\n" + "\n".join(lines))


def validate_config(validator: object | None, value: object, _plugin_name: str) -> object:
    """Run a validator object or callable and normalize its error type."""

    if validator is None:
        return value
    try:
        method = getattr(validator, "validate", None)
        if callable(method):
            callback = cast(Callable[[object], object], method)
            return callback(value)
        if callable(validator):
            callback = cast(Callable[[object], object], validator)
            return callback(value)
        raise TypeError("Config must be callable or provide validate()")
    except ValidationError:
        raise
    except BaseException as error:
        raise ValidationError(({"message": str(error)},)) from error
