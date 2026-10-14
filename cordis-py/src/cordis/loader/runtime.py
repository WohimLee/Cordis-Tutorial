"""Runtime mounting for parsed Loader entry trees."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import cast

from cordis.context import Context
from cordis.model import Plugin, normalize_inject

from .errors import LoaderError
from .model import Entry, ParsedEntry
from .resolver import ModuleResolver


class _EntryPlugin:
    """Per-entry plugin wrapper carrying declarative inject metadata."""

    def __init__(self, loader: Loader, entry: Entry, plugin: Plugin) -> None:
        self.loader = loader
        self.entry = entry
        self.plugin = plugin
        self.name = entry.parsed.module
        self.inject = normalize_inject(getattr(plugin, "inject", None)) | dict(entry.parsed.inject)
        self.Config = getattr(plugin, "Config", None)

    async def apply(self, context: Context, config: object) -> None:
        """Activate the real plugin, own its result, then mount its child entries."""

        result = self._invoke(context, config)
        if inspect.isawaitable(result):
            result = await cast(Awaitable[object], result)
        if result is not None:
            await context.effect(
                lambda: result,
                f"loader.plugin({self.entry.id!r})",
            )
        for parsed_child in self.entry.parsed.children:
            await self.loader.mount_entry(parsed_child, context, self.entry)

    def _invoke(self, context: Context, config: object) -> object:
        if inspect.isclass(self.plugin):
            constructor = cast(Callable[[Context, object], object], self.plugin)
            constructor(context, config)
            return None
        apply = getattr(self.plugin, "apply", None)
        if not inspect.isfunction(self.plugin) and callable(apply):
            return apply(context, config)
        callback = cast(Callable[[Context, object], object], self.plugin)
        return callback(context, config)


class Loader:
    """Mount immutable parsed declarations using the Cordis dependency scheduler."""

    def __init__(self, context: Context, resolver: ModuleResolver) -> None:
        self.context = context
        self.resolver = resolver
        self.entries: dict[str, Entry] = {}
        self.roots: list[Entry] = []
        self._prepared: dict[str, Plugin] = {}
        self._closed = False

    async def mount(self, parsed_entries: tuple[ParsedEntry, ...]) -> tuple[Entry, ...]:
        """Mount a new entry forest and wait until current Fiber work settles."""

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

    async def _mount_forest(self, parsed_entries: tuple[ParsedEntry, ...]) -> None:
        for parsed in parsed_entries:
            await self.mount_entry(parsed, self.context, None)
        for entry in tuple(self.entries.values()):
            if entry.fiber is not None:
                await entry.fiber.wait()

    async def mount_entry(
        self,
        parsed: ParsedEntry,
        context: Context,
        parent: Entry | None,
    ) -> Entry:
        existing = self.entries.get(parsed.id)
        if existing is not None:
            raise LoaderError(f"duplicate runtime entry id {parsed.id!r}", parsed.location)
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
            wrapper = _EntryPlugin(self, entry, plugin)
            entry.fiber = context.plugin(wrapper, parsed.config)
            await entry.fiber.wait()
        except BaseException as error:
            entry.error = error
            raise
        return entry

    async def update(self, parsed_entries: tuple[ParsedEntry, ...]) -> None:
        """Apply config-only leaf changes atomically across the mounted tree."""

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
            raise LoaderError("config update for an entry with children requires replacement")

        applied: list[tuple[Entry, ParsedEntry]] = []
        try:
            for entry, previous, candidate in changes:
                if entry.fiber is None:
                    raise LoaderError(f"entry {entry.id!r} is not active", candidate.location)
                await entry.fiber.update(candidate.config)
                entry.parsed = candidate
                entry.version += 1
                entry.error = None
                applied.append((entry, previous))
            for entry_id, candidate in candidates.items():
                self.entries[entry_id].parsed = candidate
        except BaseException as error:
            rollback_errors: list[BaseException] = []
            for entry, previous in reversed(applied):
                try:
                    if entry.fiber is not None:
                        await entry.fiber.update(previous.config)
                    entry.parsed = previous
                    entry.version += 1
                except BaseException as rollback_error:
                    rollback_errors.append(rollback_error)
            if changes:
                changes[len(applied)][0].error = error
            cause: BaseException = error
            if rollback_errors:
                cause = BaseExceptionGroup(
                    "loader update and rollback failed",
                    [error, *rollback_errors],
                )
            raise LoaderError("failed to update entry configuration", cause=cause) from cause

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
            except BaseException as rollback_error:
                cause = BaseExceptionGroup(
                    "loader replacement and rollback failed",
                    [error, rollback_error],
                )
                raise LoaderError("failed to replace entry tree", cause=cause) from cause
            raise LoaderError("failed to replace entry tree", cause=error) from error
        finally:
            self._prepared.clear()

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

    async def close(self) -> None:
        """Dispose every root through normal Fiber ownership paths."""

        if self._closed:
            return
        self._closed = True
        await self._dispose_forest()

    async def _dispose_forest(self) -> None:
        errors: list[BaseException] = []
        for entry in reversed(self.roots):
            if entry.fiber is None:
                continue
            try:
                await entry.fiber.dispose()
            except BaseException as error:
                errors.append(error)
        if errors:
            raise BaseExceptionGroup("loader close failed", errors)

    def _reset_forest(self) -> None:
        self.entries.clear()
        self.roots.clear()
