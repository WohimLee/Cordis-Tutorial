"""Configuration and runtime models for Loader entries."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .errors import SourceLocation

if TYPE_CHECKING:
    from cordis.context import Context
    from cordis.fiber import Fiber
    from cordis.model import Plugin


@dataclass(frozen=True, slots=True)
class ParsedEntry:
    """An immutable, validated plugin declaration."""

    id: str
    module: str
    config: object
    location: SourceLocation
    disabled: bool = False
    inject: Mapping[str, object | None] = field(default_factory=lambda: dict[str, object | None]())
    children: tuple[ParsedEntry, ...] = ()


@dataclass(slots=True)
class Entry:
    """Mutable runtime state corresponding to one stable parsed entry id."""

    parsed: ParsedEntry
    plugin: Plugin | None = None
    fiber: Fiber | None = None
    version: int = 0
    error: BaseException | None = None
    parent: Entry | None = None
    context: Context | None = None
    children: list[Entry] = field(default_factory=lambda: list[Entry]())

    @property
    def id(self) -> str:
        return self.parsed.id
