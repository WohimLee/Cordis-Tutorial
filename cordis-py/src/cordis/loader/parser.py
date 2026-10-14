"""Safe, source-aware parsing for declarative Loader configuration."""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import cast

import yaml

from .errors import LoaderError, SourceLocation
from .model import ParsedEntry

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def interpolate_env(value: object, env: Mapping[str, str], location: SourceLocation) -> object:
    """Recursively replace `${NAME}` placeholders without evaluating expressions."""

    if isinstance(value, str):
        match = _ENV_PATTERN.fullmatch(value)
        if match is not None:
            name = match.group(1)
            if name not in env:
                raise LoaderError(f"environment variable {name!r} is not set", location)
            return env[name]

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in env:
                raise LoaderError(f"environment variable {name!r} is not set", location)
            return env[name]

        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return MappingProxyType(
            {key: interpolate_env(item, env, location) for key, item in mapping.items()}
        )
    if isinstance(value, list | tuple):
        sequence = cast(list[object] | tuple[object, ...], value)
        return tuple(interpolate_env(item, env, location) for item in sequence)
    return value


class ConfigParser:
    """Validate format-neutral mappings into immutable parsed entries."""

    def parse(
        self,
        document: Mapping[str, object],
        source: Path,
        *,
        env: Mapping[str, str] | None = None,
        source_text: str | None = None,
    ) -> tuple[ParsedEntry, ...]:
        raw_plugins = document.get("plugins")
        if not isinstance(raw_plugins, list):
            raise LoaderError("top-level 'plugins' must be an array", SourceLocation(source))
        environment = os.environ if env is None else env
        return self._parse_entries(
            cast(list[object], raw_plugins), source, environment, source_text
        )

    def _parse_entries(
        self,
        values: list[object],
        source: Path,
        env: Mapping[str, str],
        source_text: str | None,
    ) -> tuple[ParsedEntry, ...]:
        entries: list[ParsedEntry] = []
        ids: set[str] = set()
        for index, value in enumerate(values):
            fallback = SourceLocation(source, index + 1, 1)
            if not isinstance(value, Mapping):
                raise LoaderError("plugin entry must be a table/object", fallback)
            raw = cast(Mapping[object, object], value)
            entry_id = raw.get("id")
            module = raw.get("module")
            entry_source = raw.get("_cordis_source", source)
            entry_line = raw.get("_cordis_line", fallback.line)
            if isinstance(entry_source, Path) and isinstance(entry_line, int):
                fallback = SourceLocation(entry_source, entry_line, 1)
            location = self._locate(fallback.path, source_text, module, fallback)
            if not isinstance(entry_id, str) or not entry_id:
                raise LoaderError("plugin entry requires a non-empty string 'id'", location)
            if entry_id in ids:
                raise LoaderError(f"duplicate plugin entry id {entry_id!r}", location)
            ids.add(entry_id)
            if not isinstance(module, str) or not module:
                raise LoaderError("plugin entry requires a non-empty string 'module'", location)
            disabled = raw.get("disabled", False)
            if not isinstance(disabled, bool):
                raise LoaderError("plugin 'disabled' must be boolean", location)
            inject = raw.get("inject", {})
            if not isinstance(inject, Mapping) or not all(
                isinstance(key, str) for key in cast(Mapping[object, object], inject)
            ):
                raise LoaderError("plugin 'inject' must be a string-keyed object", location)
            raw_inject = cast(Mapping[object, object], inject)
            children = raw.get("plugins", [])
            if not isinstance(children, list):
                raise LoaderError("nested 'plugins' must be an array", location)
            frozen_inject = cast(
                Mapping[str, object | None],
                interpolate_env(raw_inject, env, location),
            )
            entries.append(
                ParsedEntry(
                    id=entry_id,
                    module=module,
                    config=interpolate_env(raw.get("config"), env, location),
                    location=location,
                    disabled=disabled,
                    inject=frozen_inject,
                    children=self._parse_entries(
                        cast(list[object], children), source, env, source_text
                    ),
                )
            )
        return tuple(entries)

    @staticmethod
    def _locate(
        source: Path,
        source_text: str | None,
        module: object,
        fallback: SourceLocation,
    ) -> SourceLocation:
        if source_text is None or not isinstance(module, str):
            return fallback
        for line_number, line in enumerate(source_text.splitlines(), 1):
            column = line.find(module)
            if column >= 0:
                return SourceLocation(source, line_number, column + 1)
        return fallback


class TomlConfigParser(ConfigParser):
    """Parse TOML using the Python standard library's non-executing parser."""

    def loads(
        self,
        text: str,
        source: Path,
        *,
        env: Mapping[str, str] | None = None,
    ) -> tuple[ParsedEntry, ...]:
        try:
            document = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            match = re.search(r"line (\d+), column (\d+)", str(error))
            location = (
                SourceLocation(source, int(match.group(1)), int(match.group(2)))
                if match is not None
                else SourceLocation(source)
            )
            raise LoaderError(f"invalid TOML: {error}", location, cause=error) from error
        return self.parse(document, source, env=env, source_text=text)

    def load(
        self,
        source: Path,
        *,
        env: Mapping[str, str] | None = None,
    ) -> tuple[ParsedEntry, ...]:
        return self.loads(source.read_text(), source, env=env)


def load_yaml_mapping(text: str, source: Path) -> Mapping[str, object]:
    """Load one YAML document without constructing Python-specific objects."""

    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        location = (
            SourceLocation(source, mark.line + 1, mark.column + 1)
            if mark is not None
            else SourceLocation(source)
        )
        raise LoaderError(f"invalid YAML: {error}", location, cause=error) from error
    if not isinstance(document, Mapping):
        raise LoaderError("top-level YAML document must be an object", SourceLocation(source))
    mapping = cast(Mapping[object, object], document)
    if not all(isinstance(key, str) for key in mapping):
        raise LoaderError("top-level YAML keys must be strings", SourceLocation(source))
    return cast(Mapping[str, object], mapping)


class YamlConfigParser(ConfigParser):
    """Thin safe-YAML frontend over the format-neutral parser."""

    def loads(
        self,
        text: str,
        source: Path,
        *,
        env: Mapping[str, str] | None = None,
    ) -> tuple[ParsedEntry, ...]:
        document = load_yaml_mapping(text, source)
        return self.parse(document, source, env=env, source_text=text)

    def load(
        self,
        source: Path,
        *,
        env: Mapping[str, str] | None = None,
    ) -> tuple[ParsedEntry, ...]:
        return self.loads(source.read_text(), source, env=env)
