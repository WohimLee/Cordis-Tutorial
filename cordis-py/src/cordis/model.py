"""Shared immutable runtime records."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeAlias, TypeVar, cast

Plugin: TypeAlias = object
InjectSpec: TypeAlias = list[str] | tuple[str, ...] | Mapping[str, object | None]
METHOD_INJECT = "__cordis_method_inject__"
Decorated = TypeVar("Decorated")


@dataclass(frozen=True, slots=True)
class PluginSpec:
    """Normalized plugin entrypoint and metadata."""

    callback: Plugin
    name: str
    inject: Mapping[str, object | None]
    validator: object | None = None


def normalize_inject(value: object) -> dict[str, object | None]:
    """Normalize array or mapping dependency metadata."""

    if value is None:
        return {}
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        if not all(isinstance(name, str) for name in mapping):
            raise TypeError("plugin inject mapping keys must be strings")
        return {cast(str, name): config for name, config in mapping.items()}
    if isinstance(value, (list, tuple)):
        sequence = cast(Sequence[object], value)
        if all(isinstance(name, str) for name in sequence):
            return {cast(str, name): None for name in sequence}
    raise TypeError("plugin inject must be a string sequence or mapping")


class Inject:
    """Cordis-compatible dependency decorator for classes and methods."""

    def __init__(self, name: str, config: object = None) -> None:
        self.name = name
        self.config = config

    def __call__(self, value: Decorated) -> Decorated:
        target = cast(Any, value)
        attribute = "inject" if isinstance(value, type) else METHOD_INJECT
        inherited = normalize_inject(getattr(target, attribute, None))
        setattr(target, attribute, inherited | {self.name: self.config})
        return value

    @staticmethod
    def resolve(
        value: object,
        result: dict[str, object | None] | None = None,
    ) -> dict[str, object | None]:
        """Normalize dependency metadata into a mutable mapping."""

        resolved = {} if result is None else result
        resolved.update(normalize_inject(value))
        return resolved
