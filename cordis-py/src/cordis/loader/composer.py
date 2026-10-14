"""Include expansion and stable-id overlays for Loader configuration."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import cast

from .errors import LoaderError, SourceLocation
from .model import ParsedEntry
from .parser import TomlConfigParser, load_yaml_mapping

_OVERLAY_FIELDS = frozenset({"module", "config", "disabled", "inject"})
_MERGED_FIELDS = frozenset({"config", "inject"})


class ConfigComposer:
    """Load a TOML configuration graph and apply deterministic overlays."""

    def __init__(self, parser: TomlConfigParser | None = None) -> None:
        self.parser = parser or TomlConfigParser()

    def load(
        self,
        source: Path,
        *,
        env: Mapping[str, str] | None = None,
    ) -> tuple[ParsedEntry, ...]:
        """Expand includes and overlays, then parse the combined document."""

        document = self._compose(source.resolve(), ())
        return self.parser.parse(document, source.resolve(), env=env)

    def _compose(
        self,
        source: Path,
        stack: tuple[Path, ...],
    ) -> dict[str, object]:
        if source in stack:
            cycle = " -> ".join(str(path) for path in (*stack, source))
            raise LoaderError(
                f"include cycle detected: {cycle}",
                SourceLocation(source),
            )
        try:
            text = source.read_text()
        except OSError as error:
            raise LoaderError(
                f"cannot read included config: {error}",
                SourceLocation(source),
                cause=error,
            ) from error
        if source.suffix.lower() in {".yaml", ".yml"}:
            raw_document = load_yaml_mapping(text, source)
        else:
            try:
                raw_document = tomllib.loads(text)
            except tomllib.TOMLDecodeError as error:
                raise LoaderError(
                    f"invalid TOML: {error}",
                    SourceLocation(source),
                    cause=error,
                ) from error
        document = cast(dict[str, object], raw_document)
        includes = self._include_paths(document.pop("include", []), source)
        plugins: list[object] = []
        for included in includes:
            child = self._compose(included, (*stack, source))
            plugins.extend(self._plugins(child, included, None))
        plugins.extend(self._plugins(document, source, text))

        result: dict[str, object] = {"plugins": plugins}
        overlays = document.get("overlays", {})
        self._apply_overlays(plugins, overlays, source)
        self._validate_unique_ids(plugins, source)
        return result

    @staticmethod
    def _include_paths(value: object, source: Path) -> tuple[Path, ...]:
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, list):
            raw_values = cast(list[object], value)
            if not all(isinstance(item, str) for item in raw_values):
                raise LoaderError(
                    "'include' must be a path string or an array of path strings",
                    SourceLocation(source),
                )
            values = cast(list[str], raw_values)
        else:
            raise LoaderError(
                "'include' must be a path string or an array of path strings",
                SourceLocation(source),
            )
        return tuple((source.parent / value).resolve() for value in values)

    @classmethod
    def _plugins(
        cls,
        document: Mapping[str, object],
        source: Path,
        source_text: str | None,
    ) -> list[object]:
        plugins = document.get("plugins", [])
        if not isinstance(plugins, list):
            raise LoaderError("top-level 'plugins' must be an array", SourceLocation(source))
        result = deepcopy(cast(list[object], plugins))
        cls._annotate_sources(result, source, source_text)
        return result

    @classmethod
    def _annotate_sources(
        cls,
        plugins: list[object],
        source: Path,
        source_text: str | None,
    ) -> None:
        for index, value in enumerate(plugins):
            if not isinstance(value, dict):
                continue
            entry = cast(dict[str, object], value)
            entry.setdefault("_cordis_source", source)
            entry.setdefault(
                "_cordis_line",
                cls._module_line(entry.get("module"), source_text, index),
            )
            children = entry.get("plugins", [])
            if isinstance(children, list):
                cls._annotate_sources(cast(list[object], children), source, source_text)

    @staticmethod
    def _module_line(module: object, source_text: str | None, fallback: int) -> int:
        if not isinstance(module, str) or source_text is None:
            return fallback + 1
        for number, line in enumerate(source_text.splitlines(), 1):
            if module in line:
                return number
        return fallback + 1

    def _apply_overlays(self, plugins: list[object], value: object, source: Path) -> None:
        if not isinstance(value, Mapping):
            raise LoaderError("'overlays' must be a table/object", SourceLocation(source))
        overlays = cast(Mapping[object, object], value)
        index = self._index(plugins, source)
        for entry_id, patch in overlays.items():
            if not isinstance(entry_id, str) or not isinstance(patch, Mapping):
                raise LoaderError(
                    "overlay keys must be entry ids and values must be objects",
                    SourceLocation(source),
                )
            target = index.get(entry_id)
            if target is None:
                raise LoaderError(
                    f"overlay target {entry_id!r} does not exist",
                    SourceLocation(source),
                )
            raw_patch = cast(Mapping[object, object], patch)
            unknown = [key for key in raw_patch if key not in _OVERLAY_FIELDS]
            if unknown:
                raise LoaderError(
                    f"overlay for {entry_id!r} contains unsupported field {unknown[0]!r}",
                    SourceLocation(source),
                )
            for key, item in raw_patch.items():
                field = cast(str, key)
                if field == "module":
                    target["_cordis_source"] = source
                    target["_cordis_line"] = 1
                if field in _MERGED_FIELDS and isinstance(item, Mapping):
                    previous = target.get(field, {})
                    if not isinstance(previous, Mapping):
                        previous = {}
                    target[field] = self._deep_merge(
                        cast(Mapping[object, object], previous),
                        cast(Mapping[object, object], item),
                    )
                else:
                    target[field] = deepcopy(item)

    def _index(
        self,
        plugins: list[object],
        source: Path,
    ) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for value in plugins:
            if not isinstance(value, dict):
                continue
            entry = cast(dict[str, object], value)
            entry_id = entry.get("id")
            if isinstance(entry_id, str):
                if entry_id in result:
                    raise LoaderError(
                        f"duplicate plugin entry id {entry_id!r}",
                        SourceLocation(source),
                    )
                result[entry_id] = entry
            children = entry.get("plugins", [])
            if isinstance(children, list):
                child_index = self._index(cast(list[object], children), source)
                for child_id, child in child_index.items():
                    if child_id in result:
                        raise LoaderError(
                            f"duplicate plugin entry id {child_id!r}",
                            SourceLocation(source),
                        )
                    result[child_id] = child
        return result

    def _validate_unique_ids(self, plugins: list[object], source: Path) -> None:
        self._index(plugins, source)

    @classmethod
    def _deep_merge(
        cls,
        base: Mapping[object, object],
        overlay: Mapping[object, object],
    ) -> dict[object, object]:
        result = deepcopy(dict(base))
        for key, value in overlay.items():
            previous = result.get(key)
            if isinstance(previous, Mapping) and isinstance(value, Mapping):
                result[key] = cls._deep_merge(
                    cast(Mapping[object, object], previous),
                    cast(Mapping[object, object], value),
                )
            else:
                result[key] = deepcopy(value)
        return result
