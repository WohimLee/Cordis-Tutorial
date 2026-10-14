"""Include expansion, provenance and overlays for configuration (chapter 18).

The chapter builds one anchor, ``ConfigComposer``, and refuses to invent a
second one.  Composition is a pure function over *mappings*: a format adapter
(TOML or JSON from the standard library, YAML through a project-pinned parser)
reads a file into a mapping, and the composer never parses text itself.  That
boundary is what lets the same composer serve several formats.

Three contracts are established here:

* an ``include`` path is resolved relative to the *including* file and expanded
  recursively, with canonical paths used for cycle detection;
* every composed entry keeps the source path it was declared in, so an error
  can point at the file that owns the offending declaration;
* ``overlays`` patch entries by stable id, and composition is pure, so a failed
  reload never disturbs the runtime tree that is already mounted.

The module deliberately stops before HMR: there is no watcher, no module cache
and no timer.  ``cordis-py``'s ``ConfigComposer`` is a Python extension; the
upstream compatibility matrix excludes Include, so nothing here claims DSH
Include equivalence.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

_SOURCE = "_cordis_source"
_LINE = "_cordis_line"
_OVERLAY_FIELDS = frozenset({"module", "config", "disabled", "inject"})
_MERGED_FIELDS = frozenset({"config", "inject"})


class AdapterError(ValueError):
    """Raised by a format adapter when the file is not a valid document.

    It stays a ``ValueError`` so ``ConfigComposer._read`` wraps every adapter
    parse failure into one located ``ComposerError``.
    """


@dataclass(frozen=True, slots=True)
class SourceLocation:
    """Where a declaration came from; ``line`` is ``None`` when unknown."""

    path: Path
    line: int | None = None

    def __str__(self) -> str:
        return str(self.path) if self.line is None else f"{self.path}:{self.line}"


class ComposerError(Exception):
    """A composition failure with an optional source location and cause."""

    def __init__(
        self,
        message: str,
        location: SourceLocation | None = None,
        *,
        cause: BaseException | None = None,
    ) -> None:
        self.message = message
        self.location = location
        self.cause = cause
        detail = f"{location}: {message}" if location is not None else message
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class ComposedEntry:
    """One plugin declaration after includes and overlays are resolved."""

    id: str
    module: str
    config: object
    disabled: bool
    inject: Mapping[str, object]
    location: SourceLocation
    children: tuple[ComposedEntry, ...] = ()


# --------------------------------------------------------------------------- #
# Format adapters: text -> mapping
# --------------------------------------------------------------------------- #


class MappingAdapter(Protocol):
    """Read one configuration file into a mapping without interpreting it."""

    def read(self, source: Path) -> Mapping[str, object]: ...


class TomlMappingAdapter:
    """TOML adapter built on the standard library's non-executing parser."""

    def read(self, source: Path) -> Mapping[str, object]:
        return tomllib.loads(source.read_text())


class JsonMappingAdapter:
    """JSON adapter built on the standard library."""

    def read(self, source: Path) -> Mapping[str, object]:
        value = json.loads(source.read_text())
        if not isinstance(value, Mapping):
            raise AdapterError("top-level JSON document must be an object")
        return cast(Mapping[str, object], value)


# --------------------------------------------------------------------------- #
# Composer
# --------------------------------------------------------------------------- #


class ConfigComposer:
    """Expand includes, apply id-addressed overlays, keep provenance."""

    def __init__(self, adapter: MappingAdapter) -> None:
        self.adapter = adapter

    def load(self, source: Path) -> tuple[ComposedEntry, ...]:
        """Compose one configuration graph into immutable entries."""

        resolved = source.resolve()
        return self._build(self._compose(resolved, ()), resolved)

    # -- composition over annotated mappings -------------------------------- #

    def _compose(
        self,
        source: Path,
        stack: tuple[Path, ...],
    ) -> list[dict[str, object]]:
        if source in stack:
            chain = " -> ".join(str(path) for path in (*stack, source))
            raise ComposerError(
                f"include cycle detected: {chain}", SourceLocation(source)
            )
        document = self._read(source, stack)
        plugins: list[dict[str, object]] = []
        for included in self._include_paths(document, source):
            plugins.extend(self._compose(included, (*stack, source)))
        plugins.extend(self._annotate(document.get("plugins", []), source))
        self._apply_overlays(plugins, document.get("overlays"), source)
        self._index(plugins, source)
        return plugins

    def _read(
        self,
        source: Path,
        stack: tuple[Path, ...],
    ) -> Mapping[str, object]:
        try:
            document = self.adapter.read(source)
        except OSError as error:
            detail = f"cannot read config {source}: {error}"
            if stack:
                chain = " -> ".join(str(path) for path in (*stack, source))
                detail = f"{detail} (include chain: {chain})"
            raise ComposerError(detail, SourceLocation(source), cause=error) from error
        except ValueError as error:
            raise ComposerError(
                f"invalid configuration: {error}",
                SourceLocation(source),
                cause=error,
            ) from error
        if not isinstance(document, Mapping):
            raise ComposerError(
                "configuration document must be a mapping", SourceLocation(source)
            )
        return cast(Mapping[str, object], document)

    @staticmethod
    def _include_paths(
        document: Mapping[str, object],
        source: Path,
    ) -> tuple[Path, ...]:
        value = document.get("include")
        if value is None:
            return ()
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, list) and all(
            isinstance(item, str) for item in cast(list[object], value)
        ):
            values = cast(list[str], value)
        else:
            raise ComposerError(
                "'include' must be a path string or an array of path strings",
                SourceLocation(source),
            )
        return tuple((source.parent / item).resolve() for item in values)

    @classmethod
    def _annotate(
        cls,
        plugins: object,
        source: Path,
    ) -> list[dict[str, object]]:
        if plugins is None:
            return []
        if not isinstance(plugins, list):
            raise ComposerError(
                "top-level 'plugins' must be an array", SourceLocation(source)
            )
        result: list[dict[str, object]] = []
        for value in cast(list[object], plugins):
            if not isinstance(value, Mapping):
                raise ComposerError(
                    "plugin entry must be a mapping", SourceLocation(source)
                )
            entry = dict(cast(Mapping[str, object], value))
            entry.setdefault(_SOURCE, source)
            entry.setdefault(_LINE, None)
            children = entry.get("plugins", [])
            if isinstance(children, list):
                entry["plugins"] = cls._annotate(children, source)
            result.append(entry)
        return result

    @classmethod
    def _location(cls, entry: Mapping[str, object], fallback: Path) -> SourceLocation:
        path = entry.get(_SOURCE, fallback)
        line = entry.get(_LINE)
        return SourceLocation(
            path if isinstance(path, Path) else fallback,
            line if isinstance(line, int) else None,
        )

    @classmethod
    def _index(
        cls,
        plugins: list[dict[str, object]],
        source: Path,
    ) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for entry in plugins:
            entry_id = entry.get("id")
            if isinstance(entry_id, str):
                if entry_id in result:
                    raise ComposerError(
                        f"duplicate plugin entry id {entry_id!r}",
                        cls._location(entry, source),
                    )
                result[entry_id] = entry
            children = entry.get("plugins", [])
            if isinstance(children, list):
                child_index = cls._index(
                    cast(list[dict[str, object]], children), source
                )
                for child_id, child in child_index.items():
                    if child_id in result:
                        raise ComposerError(
                            f"duplicate plugin entry id {child_id!r}",
                            cls._location(child, source),
                        )
                    result[child_id] = child
        return result

    def _apply_overlays(
        self,
        plugins: list[dict[str, object]],
        overlays: object,
        source: Path,
    ) -> None:
        if overlays is None:
            return
        if not isinstance(overlays, Mapping):
            raise ComposerError("'overlays' must be a mapping", SourceLocation(source))
        index = self._index(plugins, source)
        for entry_id, patch in cast(Mapping[object, object], overlays).items():
            if not isinstance(entry_id, str) or not isinstance(patch, Mapping):
                raise ComposerError(
                    "overlay keys must be entry ids and values must be mappings",
                    SourceLocation(source),
                )
            target = index.get(entry_id)
            if target is None:
                raise ComposerError(
                    f"overlay target {entry_id!r} does not exist",
                    SourceLocation(source),
                )
            raw_patch = cast(Mapping[str, object], patch)
            unknown = [key for key in raw_patch if key not in _OVERLAY_FIELDS]
            if unknown:
                raise ComposerError(
                    f"overlay for {entry_id!r} contains unsupported field {unknown[0]!r}",
                    SourceLocation(source),
                )
            for key, item in raw_patch.items():
                if key == "module":
                    target[_SOURCE] = source
                    target[_LINE] = None
                if key in _MERGED_FIELDS and isinstance(item, Mapping):
                    previous = target.get(key)
                    if not isinstance(previous, Mapping):
                        previous = {}
                    target[key] = self._deep_merge(
                        cast(Mapping[str, object], previous),
                        cast(Mapping[str, object], item),
                    )
                else:
                    target[key] = deepcopy(item)

    @classmethod
    def _deep_merge(
        cls,
        base: Mapping[str, object],
        overlay: Mapping[str, object],
    ) -> dict[str, object]:
        result: dict[str, object] = deepcopy(dict(base))
        for key, value in overlay.items():
            previous = result.get(key)
            if isinstance(previous, Mapping) and isinstance(value, Mapping):
                result[key] = cls._deep_merge(
                    cast(Mapping[str, object], previous),
                    cast(Mapping[str, object], value),
                )
            else:
                result[key] = deepcopy(value)
        return result

    # -- validation and typed conversion ------------------------------------ #

    def _build(
        self,
        plugins: list[dict[str, object]],
        source: Path,
    ) -> tuple[ComposedEntry, ...]:
        entries: list[ComposedEntry] = []
        for value in plugins:
            location = self._location(value, source)
            entry_id = value.get("id")
            if not isinstance(entry_id, str) or not entry_id:
                raise ComposerError(
                    "plugin entry requires a non-empty string 'id'", location
                )
            module = value.get("module")
            if not isinstance(module, str) or not module:
                raise ComposerError(
                    "plugin entry requires a non-empty string 'module'", location
                )
            disabled = value.get("disabled", False)
            if not isinstance(disabled, bool):
                raise ComposerError("plugin 'disabled' must be boolean", location)
            inject = value.get("inject", {})
            if not isinstance(inject, Mapping) or not all(
                isinstance(key, str) for key in cast(Mapping[object, object], inject)
            ):
                raise ComposerError(
                    "plugin 'inject' must be a string-keyed mapping", location
                )
            children = value.get("plugins", [])
            if not isinstance(children, list):
                raise ComposerError("nested 'plugins' must be an array", location)
            entries.append(
                ComposedEntry(
                    id=entry_id,
                    module=module,
                    config=value.get("config"),
                    disabled=disabled,
                    inject=dict(cast(Mapping[str, object], inject)),
                    location=location,
                    children=self._build(
                        cast(list[dict[str, object]], children), source
                    ),
                )
            )
        return tuple(entries)


# --------------------------------------------------------------------------- #
# Runtime side: atomic install and host-driven reload
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class MountedNode:
    """A composed entry as mounted by the teaching runtime."""

    entry: ComposedEntry
    children: list[MountedNode] = field(default_factory=list)


class CompositeRuntime:
    """Dictionary-backed tree that swaps in a composed forest atomically."""

    def __init__(self) -> None:
        self.roots: list[MountedNode] = []
        self.activations: list[str] = []

    def install(self, entries: tuple[ComposedEntry, ...]) -> None:
        """Build the new tree first, then swap it in; never partially mounted."""

        nodes = self._build(entries)
        self.roots = nodes

    def _build(self, entries: tuple[ComposedEntry, ...]) -> list[MountedNode]:
        nodes: list[MountedNode] = []
        for entry in entries:
            self.activations.append(f"mount:{entry.id}")
            nodes.append(MountedNode(entry=entry, children=self._build(entry.children)))
        return nodes

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(entry.id for entry in self._walk(tuple(self.roots)))

    def _walk(self, nodes: tuple[MountedNode, ...]) -> list[ComposedEntry]:
        entries: list[ComposedEntry] = []
        for node in nodes:
            entries.append(node.entry)
            entries.extend(self._walk(tuple(node.children)))
        return entries


class ConfigSession:
    """Host-driven reload: compose first, install second, snapshot last."""

    def __init__(
        self,
        composer: ConfigComposer,
        runtime: CompositeRuntime,
        source: Path,
    ) -> None:
        self.composer = composer
        self.runtime = runtime
        self.source = source.resolve()
        self.snapshot: tuple[ComposedEntry, ...] | None = None

    def load(self) -> tuple[ComposedEntry, ...]:
        """Compose and install the initial configuration."""

        entries = self.composer.load(self.source)
        self.runtime.install(entries)
        self.snapshot = entries
        return entries

    def reload(self) -> bool:
        """Apply a changed configuration; a compose failure changes nothing."""

        if self.snapshot is None:
            raise RuntimeError("config session has not been loaded")
        candidate = self.composer.load(self.source)
        if candidate == self.snapshot:
            return False
        self.runtime.install(candidate)
        self.snapshot = candidate
        return True


__all__ = [
    "AdapterError",
    "ComposedEntry",
    "ComposerError",
    "CompositeRuntime",
    "ConfigComposer",
    "ConfigSession",
    "JsonMappingAdapter",
    "MappingAdapter",
    "MountedNode",
    "SourceLocation",
    "TomlMappingAdapter",
]
