"""Declarative plugin trees for the tutorial Cordis runtime (chapter 17).

The chapter separates four responsibilities that are easy to blur together:

* a *parser* turns a format-neutral mapping into immutable ``ParsedEntry``
  declarations (``TomlConfigParser`` is a thin standard-library frontend);
* a *resolver* turns a ``module:attribute`` specifier into a plugin object
  (the teaching version uses an explicit table instead of ``importlib``);
* the *runtime* (``Loader``) mounts, updates and replaces the entry tree and
  owns every transaction boundary;
* a *reloader* is host-driven: it re-reads the source and delegates to
  ``Loader.update()``.

Everything is mounted through the teaching ``Context.plugin()``; the Loader
never sorts plugins by their dependencies.  A consumer declared before its
provider stays ``PENDING`` until the provider appears, then activates through
the normal Fiber path.  Include, file watching and module-cache HMR are out of
scope here (chapter 18 and later).
"""

from __future__ import annotations

import asyncio
import inspect
import tomllib
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import cast

Cleanup = Callable[[], object]
Plugin = object


# --------------------------------------------------------------------------- #
# Source-aware errors
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SourceLocation:
    """A location in a configuration source."""

    path: Path
    line: int = 1
    column: int = 1

    def __str__(self) -> str:
        return f"{self.path}:{self.line}:{self.column}"


class LoaderError(Exception):
    """A Loader error with an optional source location and cause."""

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


class LoaderSecurityError(LoaderError):
    """A module specifier rejected by the resolver policy."""


class InactiveEffectError(RuntimeError):
    """Raised when a disposed owner tries to create a new long-lived resource."""


# --------------------------------------------------------------------------- #
# Declarative model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ParsedEntry:
    """An immutable, validated plugin declaration with a stable id."""

    id: str
    module: str
    config: object
    location: SourceLocation
    disabled: bool = False
    inject: Mapping[str, object | None] = field(
        default_factory=lambda: MappingProxyType({})
    )
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
    children: list[Entry] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.parsed.id


def normalize_inject(value: object) -> dict[str, object]:
    """Normalize a plugin's ``inject`` declaration into a name-keyed mapping."""

    if value is None:
        return {}
    if isinstance(value, str):
        return {value: None}
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {str(key): item for key, item in mapping.items()}
    if isinstance(value, Iterable):
        return {str(item): None for item in cast(Iterable[object], value)}
    raise TypeError("inject must be a service name, a mapping or an iterable")


# --------------------------------------------------------------------------- #
# Parser: format-neutral mapping -> immutable entries
# --------------------------------------------------------------------------- #

_ENV_PREFIX = "${"


def interpolate_env(
    value: object,
    env: Mapping[str, str],
    location: SourceLocation,
) -> object:
    """Recursively replace ``${NAME}`` placeholders without evaluating code."""

    if isinstance(value, str):
        if value.startswith(_ENV_PREFIX) and value.endswith("}"):
            name = value[2:-1]
            if name not in env:
                raise LoaderError(f"environment variable {name!r} is not set", location)
            return env[name]
        result = value
        for name, replacement in env.items():
            result = result.replace(f"{_ENV_PREFIX}{name}}}", replacement)
        return result
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return MappingProxyType(
            {key: interpolate_env(item, env, location) for key, item in mapping.items()}
        )
    if isinstance(value, list | tuple):
        sequence = cast(list[object] | tuple[object, ...], value)
        return tuple(interpolate_env(item, env, location) for item in sequence)
    return value


class MappingConfigParser:
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
            raise LoaderError(
                "top-level 'plugins' must be an array", SourceLocation(source)
            )
        environment: Mapping[str, str] = {} if env is None else env
        return self._parse_entries(
            cast(list[object], raw_plugins),
            source,
            environment,
            source_text,
            set(),
        )

    def _parse_entries(
        self,
        values: list[object],
        source: Path,
        env: Mapping[str, str],
        source_text: str | None,
        ids: set[str],
    ) -> tuple[ParsedEntry, ...]:
        entries: list[ParsedEntry] = []
        for index, value in enumerate(values):
            fallback = SourceLocation(source, index + 1, 1)
            if not isinstance(value, Mapping):
                raise LoaderError("plugin entry must be a table/object", fallback)
            raw = cast(Mapping[object, object], value)
            entry_id = raw.get("id")
            module = raw.get("module")
            location = self._locate(source, source_text, module, fallback)
            if not isinstance(entry_id, str) or not entry_id:
                raise LoaderError(
                    "plugin entry requires a non-empty string 'id'", location
                )
            if entry_id in ids:
                raise LoaderError(f"duplicate plugin entry id {entry_id!r}", location)
            ids.add(entry_id)
            if not isinstance(module, str) or not module:
                raise LoaderError(
                    "plugin entry requires a non-empty string 'module'", location
                )
            disabled = raw.get("disabled", False)
            if not isinstance(disabled, bool):
                raise LoaderError("plugin 'disabled' must be boolean", location)
            inject = raw.get("inject", {})
            if not isinstance(inject, Mapping) or not all(
                isinstance(key, str) for key in cast(Mapping[object, object], inject)
            ):
                raise LoaderError(
                    "plugin 'inject' must be a string-keyed object", location
                )
            children = raw.get("plugins", [])
            if not isinstance(children, list):
                raise LoaderError("nested 'plugins' must be an array", location)
            frozen_inject = cast(
                Mapping[str, object | None], interpolate_env(inject, env, location)
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
                        cast(list[object], children), source, env, source_text, ids
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


class TomlConfigParser(MappingConfigParser):
    """Parse TOML with the standard library's non-executing parser."""

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
            raise LoaderError(
                f"invalid TOML: {error}", SourceLocation(source), cause=error
            ) from error
        return self.parse(document, source, env=env, source_text=text)

    def load(
        self,
        source: Path,
        *,
        env: Mapping[str, str] | None = None,
    ) -> tuple[ParsedEntry, ...]:
        try:
            text = source.read_text()
        except OSError as error:
            raise LoaderError(
                f"cannot read config: {error}", SourceLocation(source), cause=error
            ) from error
        return self.loads(text, source, env=env)


# --------------------------------------------------------------------------- #
# Resolver: specifier -> plugin object
# --------------------------------------------------------------------------- #


class ModuleResolver:
    """Resolve ``module:attribute`` specifiers from an explicit plugin table."""

    def __init__(
        self,
        table: Mapping[str, Plugin] | None = None,
        *,
        allowed: Iterable[str] = (),
    ) -> None:
        self.table: dict[str, Plugin] = dict(table or {})
        self.allowed: frozenset[str] | None = frozenset(allowed) if allowed else None

    def resolve(self, specifier: str, location: SourceLocation) -> Plugin:
        """Return the plugin for one specifier or raise a located error."""

        module, separator, attribute = specifier.partition(":")
        if not separator or not module or not attribute:
            raise LoaderError(
                "plugin reference must use 'module:attribute' syntax", location
            )
        if self.allowed is not None and module not in self.allowed:
            raise LoaderSecurityError(
                f"plugin module {module!r} is not allowed", location
            )
        plugin = self.table.get(specifier)
        if plugin is None:
            raise LoaderError(f"cannot resolve plugin {specifier!r}", location)
        return plugin


# --------------------------------------------------------------------------- #
# Teaching Core: effects, fibers, context
# --------------------------------------------------------------------------- #


class FiberState(StrEnum):
    """Portable Fiber states used by the tutorial runtime."""

    PENDING = "PENDING"
    LOADING = "LOADING"
    ACTIVE = "ACTIVE"
    UNLOADING = "UNLOADING"
    FAILED = "FAILED"
    DISPOSED = "DISPOSED"


class Disposer:
    """Idempotent, awaitable cleanup handle owned by exactly one scope."""

    def __init__(self, execute: Callable[[], Awaitable[None]], label: str) -> None:
        self.label = label
        self._execute = execute
        self._task: asyncio.Task[None] | None = None

    def __call__(self) -> Awaitable[None]:
        return self.dispose()

    async def dispose(self) -> None:
        task = self._task
        if task is None:
            task = asyncio.ensure_future(self._execute())
            self._task = task
        await asyncio.shield(task)


class EffectScope:
    """Ordered cleanup stack: reverse order, idempotent, fully awaited."""

    def __init__(self) -> None:
        self._items: list[Disposer] = []

    def install(self, execute: Callable[[], Awaitable[None]], label: str) -> Disposer:
        disposer = Disposer(execute, label)
        self._items.append(disposer)
        return disposer

    def __len__(self) -> int:
        return len(self._items)

    async def close(self) -> None:
        errors: list[BaseException] = []
        for disposer in reversed(self._items):
            try:
                await disposer.dispose()
            except BaseException as error:  # noqa: BLE001 - cleanup continues after the first failure
                errors.append(error)
        self._items.clear()
        if errors:
            raise BaseExceptionGroup("effect cleanup failed", errors)


class RootFiber:
    """The root owner: it has effects but no plugin activation."""

    uid = 0
    name = "root"
    state = FiberState.ACTIVE
    error: BaseException | None = None

    def __init__(self, context: Context) -> None:
        self.ctx = self.context = context
        self.effects = EffectScope()
        self.children: list[Fiber] = []

    async def dispose(self) -> None:
        if self.state is FiberState.DISPOSED:
            return
        self.state = FiberState.DISPOSED
        for child in reversed(self.children):
            await child.dispose()
        self.children.clear()
        await self.effects.close()


class Fiber:
    """One plugin activation owned by one Context."""

    _counter = 0

    def __init__(
        self,
        parent: Context,
        plugin: Plugin,
        config: object,
        *,
        name: str | None = None,
        inject: Mapping[str, object | None] | None = None,
    ) -> None:
        Fiber._counter += 1
        self.uid = Fiber._counter
        self.parent = parent
        self.ctx = self.context = Context.derive(parent, self)
        self.plugin = plugin
        self.name = name or getattr(plugin, "name", None) or type(plugin).__name__
        self.inject = normalize_inject(getattr(plugin, "inject", None)) | dict(
            inject or {}
        )
        self.config: object = config
        self.state = FiberState.PENDING
        self.version = 0
        self.error: BaseException | None = None
        self.effects = EffectScope()
        self.children: list[Fiber] = []
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._requested = False
        parent_fiber = parent.fiber
        if isinstance(parent_fiber, Fiber):
            parent_fiber.children.append(self)

    def __await__(self) -> object:
        return self.wait().__await__()

    def request(self) -> None:
        """Schedule one refresh pass unless a pass is already pending."""

        if self.state is FiberState.DISPOSED:
            return
        self._requested = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def wait(self) -> Fiber:
        """Wait until this fiber is stable, then surface an activation error."""

        while True:
            task = self._task
            if task is None or task.done():
                break
            await asyncio.shield(task)
        if self.error is not None:
            raise self.error
        return self

    async def refresh(self) -> None:
        """Re-evaluate dependencies and load or unload accordingly."""

        async with self._lock:
            if self.state is FiberState.DISPOSED:
                return
            ready = self._dependencies_ready()
            if ready and self.state is FiberState.PENDING:
                await self._load(self.config)
            elif not ready and self.state is FiberState.ACTIVE:
                await self._release()
                self.state = FiberState.PENDING
                self.version += 1

    async def update(self, config: object) -> None:
        """Validate and apply new configuration through one epoch change."""

        async with self._lock:
            if self.state is FiberState.DISPOSED:
                raise InactiveEffectError("cannot update a disposed fiber")
            validated = self._validated(config)
            if not self._dependencies_ready():
                self.config = validated
                return
            if self.state is not FiberState.PENDING:
                await self._release()
            self.config = validated
            await self._load(validated)
            if self.state is FiberState.FAILED:
                assert self.error is not None
                raise self.error
            self.version += 1

    async def dispose(self) -> None:
        """Dispose children first, then own effects; never reactivate."""

        task = self._task
        if task is not None and not task.done():
            await asyncio.shield(task)
        async with self._lock:
            if self.state is FiberState.DISPOSED:
                return
            self.state = FiberState.UNLOADING
            try:
                await self._release()
            finally:
                self.state = FiberState.DISPOSED

    def _dependencies_ready(self) -> bool:
        services = self.ctx._scope.services
        return all(name in services for name in self.inject)

    def _validated(self, config: object) -> object:
        return validate_config(getattr(self.plugin, "Config", None), config)

    async def _load(self, config: object) -> None:
        self.state = FiberState.LOADING
        self.error = None
        try:
            callback = _plugin_callback(self.plugin)
            result = callback(self.context, config)
            if inspect.isawaitable(result):
                result = await cast(Awaitable[object], result)
            if result is not None:
                await self.context.effect(lambda: result, f"plugin({self.name!r})")
            self.config = config
            self.state = FiberState.ACTIVE
        except BaseException as error:  # noqa: BLE001 - activation boundary: roll back partial effects
            self.error = error
            try:
                await self.effects.close()
            except BaseException as cleanup_error:  # noqa: BLE001 - aggregate activation and rollback failures
                self.error = BaseExceptionGroup(
                    f"plugin {self.name!r} activation and rollback failed",
                    [error, cleanup_error],
                )
            self.state = FiberState.FAILED

    async def _release(self) -> None:
        errors: list[BaseException] = []
        for child in reversed(self.children):
            try:
                await child.dispose()
            except BaseException as error:  # noqa: BLE001 - keep releasing sibling children
                errors.append(error)
        self.children.clear()
        try:
            await self.effects.close()
        except BaseException as error:  # noqa: BLE001 - aggregate child and effect failures
            errors.append(error)
        if errors:
            raise BaseExceptionGroup(f"fiber {self.name!r} cleanup failed", errors)

    async def _run(self) -> None:
        while self._requested:
            self._requested = False
            await self.refresh()


def _plugin_callback(plugin: Plugin) -> Callable[[Context, object], object]:
    apply = getattr(plugin, "apply", None)
    if callable(apply):
        return cast(Callable[[Context, object], object], apply)
    if callable(plugin):
        return cast(Callable[[Context, object], object], plugin)
    raise TypeError(f"plugin {plugin!r} is neither callable nor has apply()")


def _collect_cleanups(result: object, label: str) -> tuple[Cleanup, ...]:
    if result is None:
        return ()
    if callable(result):
        return (cast(Cleanup, result),)
    if isinstance(result, str | bytes):
        raise TypeError(f"{label} returned a non-callable cleanup")
    if isinstance(result, Iterable):
        cleanups: list[Cleanup] = []
        for item in cast(Iterable[object], result):
            if item is None:
                continue
            if not callable(item):
                raise TypeError(f"{label} returned a non-callable cleanup")
            cleanups.append(cast(Cleanup, item))
        return tuple(cleanups)
    raise TypeError(f"{label} returned a non-callable cleanup")


def validate_config(validator: object, value: object) -> object:
    """Run a plugin's ``Config`` validator, if it declares one."""

    if validator is None:
        return value
    validate = getattr(validator, "validate", None)
    if callable(validate):
        return validate(value)
    if callable(validator):
        return validator(value)
    raise TypeError("Config must be callable or expose validate()")


class Scope:
    """Root-shared service store and fiber registry."""

    def __init__(self) -> None:
        self.services: dict[str, object] = {}
        self.fibers: list[Fiber] = []

    def request(self) -> None:
        for fiber in tuple(self.fibers):
            fiber.request()

    def remove(self, name: str, value: object) -> None:
        if self.services.get(name) is value:
            del self.services[name]
            self.request()


class Context:
    """Root or derived scope that plugins receive."""

    filter = object()

    def __init__(self) -> None:
        self.root = self
        self._scope = Scope()
        self._meta: dict[object, object] = {}
        self.fiber: RootFiber | Fiber = RootFiber(self)

    @classmethod
    def derive(cls, parent: Context, fiber: Fiber | None = None) -> Context:
        context = object.__new__(cls)
        context.root = parent.root
        context._scope = parent._scope
        context._meta = dict(parent._meta)
        context.fiber = fiber or parent.fiber
        return context

    @staticmethod
    def is_context(value: object) -> bool:
        return isinstance(value, Context)

    def extend(self, meta: Mapping[object, object] | None = None) -> Context:
        context = self.derive(self)
        for key, value in (meta or {}).items():
            if isinstance(key, str):
                setattr(context, key, value)
            else:
                context._meta[key] = value
        return context

    def metadata(self, key: object) -> object | None:
        return self._meta.get(key)

    def get(self, name: str, strict: bool = True) -> object | None:
        """Look up a provided service; this chapter only needs availability."""

        return self._scope.services.get(name)

    def has(self, name: str) -> bool:
        return name in self._scope.services

    def provide(self, name: str, value: object) -> Disposer:
        """Publish a service owned by the current fiber."""

        if name in self._scope.services:
            raise RuntimeError(f"service {name!r} is already provided")
        self._scope.services[name] = value

        async def release() -> None:
            self._scope.remove(name, value)

        disposer = self.fiber.effects.install(release, f"provide({name!r})")
        self._scope.request()
        return disposer

    async def effect(
        self,
        execute: Callable[[], object],
        label: str = "effect",
    ) -> Disposer:
        """Run a setup callback and own everything it returns."""

        result = execute()
        if inspect.isawaitable(result):
            result = await cast(Awaitable[object], result)
        cleanups = _collect_cleanups(result, label)

        async def release() -> None:
            errors: list[BaseException] = []
            for cleanup in reversed(cleanups):
                try:
                    outcome = cleanup()
                    if inspect.isawaitable(outcome):
                        await cast(Awaitable[object], outcome)
                except BaseException as error:  # noqa: BLE001 - keep running remaining cleanups
                    errors.append(error)
            if errors:
                raise BaseExceptionGroup(f"{label} cleanup failed", errors)

        disposer = self.fiber.effects.install(release, label)
        return disposer

    def plugin(
        self,
        plugin: Plugin,
        config: object = None,
        *,
        name: str | None = None,
        inject: Mapping[str, object | None] | None = None,
    ) -> Fiber:
        """Create a Fiber and schedule its first refresh pass."""

        fiber = Fiber(self, plugin, config, name=name, inject=inject)
        self._scope.fibers.append(fiber)
        self._scope.request()
        return fiber

    def __getattr__(self, name: str) -> object:
        scope = self.__dict__.get("_scope")
        if scope is not None and name in scope.services:
            return scope.services[name]
        raise AttributeError(name)

    async def aclose(self) -> None:
        await self.root.fiber.dispose()


class EntryPlugin:
    """Per-entry wrapper that carries declarative inject metadata and children."""

    def __init__(self, loader: Loader, entry: Entry, plugin: Plugin) -> None:
        self.loader = loader
        self.entry = entry
        self.plugin = plugin
        self.name = entry.parsed.module
        self.inject = normalize_inject(getattr(plugin, "inject", None)) | dict(
            entry.parsed.inject
        )
        self.Config = getattr(plugin, "Config", None)

    async def apply(self, context: Context, config: object) -> None:
        """Activate the real plugin, own its result, then mount child entries."""

        result = _plugin_callback(self.plugin)(context, config)
        if inspect.isawaitable(result):
            result = await cast(Awaitable[object], result)
        if result is not None:
            await context.effect(
                lambda: result,
                f"loader.plugin({self.entry.id!r})",
            )
        for parsed_child in self.entry.parsed.children:
            await self.loader.mount_entry(parsed_child, context, self.entry)


class Loader:
    """Mount immutable parsed declarations through the Cordis dependency path."""

    def __init__(self, context: Context, resolver: ModuleResolver) -> None:
        self.context = context
        self.resolver = resolver
        self.entries: dict[str, Entry] = {}
        self.roots: list[Entry] = []
        self._prepared: dict[str, Plugin] = {}
        self._closed = False

    async def mount(self, parsed_entries: tuple[ParsedEntry, ...]) -> tuple[Entry, ...]:
        """Mount a new entry forest; a failure disposes what was started."""

        if self._closed:
            raise RuntimeError("loader is closed")
        if self.entries:
            raise RuntimeError("loader already has a mounted entry tree")
        try:
            await self._mount_forest(parsed_entries)
        except BaseException:
            await self._dispose_forest()
            raise
        return tuple(self.roots)

    async def mount_entry(
        self,
        parsed: ParsedEntry,
        context: Context,
        parent: Entry | None,
    ) -> Entry:
        """Create one runtime Entry and mount its Fiber when it is enabled."""

        if parsed.id in self.entries:
            raise LoaderError(
                f"duplicate runtime entry id {parsed.id!r}", parsed.location
            )
        entry = Entry(parsed=parsed, parent=parent, context=context)
        self.entries[parsed.id] = entry
        if parent is None:
            self.roots.append(entry)
        else:
            parent.children.append(entry)
        if parsed.disabled:
            return entry
        try:
            plugin = self._prepared.pop(parsed.id, None)
            if plugin is None:
                plugin = self.resolver.resolve(parsed.module, parsed.location)
            entry.plugin = plugin
            wrapper = EntryPlugin(self, entry, plugin)
            entry.fiber = context.plugin(
                wrapper,
                parsed.config,
                name=parsed.id,
                inject=wrapper.inject,
            )
            await entry.fiber.wait()
        except BaseException as error:
            entry.error = error
            raise
        return entry

    async def update(self, parsed_entries: tuple[ParsedEntry, ...]) -> None:
        """Apply config-only leaf changes atomically, else replace the tree."""

        candidates = self._flatten(parsed_entries)
        current = {entry_id: entry.parsed for entry_id, entry in self.entries.items()}
        if candidates.keys() != current.keys() or any(
            self._identity(candidates[entry_id]) != self._identity(current[entry_id])
            for entry_id in current
        ):
            await self.replace(parsed_entries)
            return

        changes = [
            (self.entries[entry_id], current[entry_id], candidates[entry_id])
            for entry_id in current
            if current[entry_id].config != candidates[entry_id].config
        ]
        if any(candidate.children for _, _, candidate in changes):
            raise LoaderError(
                "config update for an entry with children requires replacement"
            )

        applied: list[tuple[Entry, ParsedEntry]] = []
        failed: Entry | None = None
        try:
            for entry, previous, candidate in changes:
                if entry.fiber is None:
                    failed = entry
                    raise LoaderError(
                        f"entry {entry.id!r} is not active", candidate.location
                    )
                failed = entry
                await entry.fiber.update(candidate.config)
                entry.parsed = candidate
                entry.version += 1
                entry.error = None
                applied.append((entry, previous))
            for entry_id, candidate in candidates.items():
                self.entries[entry_id].parsed = candidate
        except BaseException as error:  # noqa: BLE001 - transaction boundary: any failure must roll back applied entries
            rollback_errors: list[BaseException] = []
            for entry, previous in reversed(applied):
                try:
                    if entry.fiber is not None:
                        await entry.fiber.update(previous.config)
                    entry.parsed = previous
                    entry.version += 1
                except BaseException as rollback_error:  # noqa: BLE001 - finish rolling back every entry
                    rollback_errors.append(rollback_error)
            if failed is not None:
                failed.error = error
            cause: BaseException = error
            if rollback_errors:
                cause = BaseExceptionGroup(
                    "loader update and rollback failed",
                    [error, *rollback_errors],
                )
            raise LoaderError(
                "failed to update entry configuration", cause=cause
            ) from cause

    async def replace(self, parsed_entries: tuple[ParsedEntry, ...]) -> None:
        """Replace the mounted forest and restore it if activation fails."""

        self._prepared = self._preflight(parsed_entries)
        previous = tuple(entry.parsed for entry in self.roots)
        await self._dispose_forest()
        self._reset_forest()
        try:
            await self._mount_forest(parsed_entries)
        except BaseException as error:
            self._prepared.clear()
            await self._dispose_forest()
            self._reset_forest()
            try:
                await self._mount_forest(previous)
            except BaseException as rollback_error:  # noqa: BLE001 - aggregate replacement and rollback failures
                cause = BaseExceptionGroup(
                    "loader replacement and rollback failed",
                    [error, rollback_error],
                )
                raise LoaderError(
                    "failed to replace entry tree", cause=cause
                ) from cause
            raise LoaderError("failed to replace entry tree", cause=error) from error
        finally:
            self._prepared.clear()

    async def close(self) -> None:
        """Dispose every root through normal Fiber ownership paths."""

        if self._closed:
            return
        self._closed = True
        await self._dispose_forest()

    async def _mount_forest(self, parsed_entries: tuple[ParsedEntry, ...]) -> None:
        for parsed in parsed_entries:
            await self.mount_entry(parsed, self.context, None)
        for entry in tuple(self.entries.values()):
            if entry.fiber is not None:
                await entry.fiber.wait()

    def _preflight(
        self,
        entries: tuple[ParsedEntry, ...],
        disabled: bool = False,
    ) -> dict[str, Plugin]:
        result: dict[str, Plugin] = {}
        for entry in entries:
            inactive = disabled or entry.disabled
            if not inactive:
                result[entry.id] = self.resolver.resolve(entry.module, entry.location)
            result.update(self._preflight(entry.children, inactive))
        return result

    @classmethod
    def _flatten(cls, roots: tuple[ParsedEntry, ...]) -> dict[str, ParsedEntry]:
        result: dict[str, ParsedEntry] = {}
        for entry in roots:
            result[entry.id] = entry
            result.update(cls._flatten(entry.children))
        return result

    @staticmethod
    def _identity(entry: ParsedEntry) -> tuple[object, ...]:
        return (
            entry.module,
            entry.disabled,
            dict(entry.inject),
            tuple(child.id for child in entry.children),
        )

    async def _dispose_forest(self) -> None:
        errors: list[BaseException] = []
        for entry in reversed(self.roots):
            if entry.fiber is None:
                continue
            try:
                await entry.fiber.dispose()
            except BaseException as error:  # noqa: BLE001 - dispose every remaining root
                errors.append(error)
        if errors:
            raise BaseExceptionGroup("loader close failed", errors)

    def _reset_forest(self) -> None:
        self.entries.clear()
        self.roots.clear()


Reader = Callable[[Path], tuple[ParsedEntry, ...]]


class ConfigReloader:
    """Host-driven reload adapter: re-read the source, then use Loader.update()."""

    def __init__(
        self,
        loader: Loader,
        source: Path,
        *,
        parser: MappingConfigParser | None = None,
        env: Mapping[str, str] | None = None,
        reader: Reader | None = None,
    ) -> None:
        self.loader = loader
        self.source = source.resolve()
        self.parser = parser or TomlConfigParser()
        self.env = env
        self.reader = reader
        self.current: tuple[ParsedEntry, ...] | None = None

    def read(self) -> tuple[ParsedEntry, ...]:
        """Read the current source through the injected reader or parser."""

        if self.reader is not None:
            return self.reader(self.source)
        return self.parser.load(self.source, env=self.env)

    async def start(self) -> tuple[Entry, ...]:
        """Load and mount the initial configuration."""

        parsed = self.read()
        roots = await self.loader.mount(parsed)
        self.current = parsed
        return roots

    async def reload(self) -> bool:
        """Apply a changed configuration, returning whether it differed."""

        if self.current is None:
            raise RuntimeError("config reloader has not been started")
        candidate = self.read()
        if candidate == self.current:
            return False
        await self.loader.update(candidate)
        self.current = candidate
        return True


__all__ = [
    "Cleanup",
    "ConfigReloader",
    "Context",
    "Disposer",
    "EffectScope",
    "Entry",
    "EntryPlugin",
    "Fiber",
    "FiberState",
    "InactiveEffectError",
    "Loader",
    "LoaderError",
    "LoaderSecurityError",
    "MappingConfigParser",
    "ModuleResolver",
    "ParsedEntry",
    "Plugin",
    "RootFiber",
    "Scope",
    "SourceLocation",
    "TomlConfigParser",
    "interpolate_env",
    "normalize_inject",
    "validate_config",
]
