"""Minimum harness: one closed loop over the whole teaching stack (chapter 21).

This is the graduation chapter, so it rebuilds everything it needs in a smaller
form instead of importing the previous chapters:

* ``EffectScope`` / ``Disposer`` / ``Fiber`` / ``Context`` own resources and
  define what a new dependency epoch means;
* ``ManualClock`` / ``TimerService`` make time deterministic and provide the
  debounce used by HMR;
* ``ConfigComposer`` composes YAML files through relative includes and
  id-addressed overlays and keeps the source path of every entry;
* ``Loader`` mounts the composed forest through ``Context.plugin()`` only, decides
  between a config-only update and a structural replacement, and rolls back on
  failure;
* ``LocalModuleSource`` turns ``name:attribute`` specifiers into plugin objects
  loaded from real files under ``src/plugins/``;
* ``ChangeSource`` / ``HotReloader`` implement host-driven HMR: the host decides
  that something changed, the harness debounces the burst and applies it;
* ``MiniHarness`` wires the stack together and reports a shutdown snapshot that
  must return to zero.

The teaching implementation is deliberately smaller than the formal one, and the
chapter text states the boundaries: ``cordis-py`` has no Timer asset at all and
only a host-driven ``ConfigReloader``; nothing here is evidence of DSH Timer
1.1.2 or DSH HMR 1.0.15 equivalence.
"""

from __future__ import annotations

import asyncio
import heapq
import importlib.util
import inspect
import sys
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import cast

import yaml

Cleanup = Callable[[], object]
Plugin = object
_MAX_FIRES = 10_000
_STABILIZE_LIMIT = 1_000


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class HarnessError(Exception):
    """A harness contract violation, optionally wrapping a cause."""

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        self.message = message
        self.cause = cause
        super().__init__(message)


class ConfigError(HarnessError):
    """A configuration document could not be composed."""


class ResolveError(HarnessError):
    """A module specifier could not be resolved to a plugin object."""


class InactiveError(RuntimeError):
    """A disposed owner tried to create a new long-lived resource."""


@dataclass(frozen=True, slots=True)
class PluginRef:
    """One resolved plugin: a module, an entry point and the module's metadata."""

    specifier: str
    path: Path
    module: object
    attribute: str
    callback: Callable[[Context, object], object]
    inject: Mapping[str, object | None] = field(
        default_factory=lambda: MappingProxyType({})
    )
    Config: object = None


# --------------------------------------------------------------------------- #
# Deterministic time
# --------------------------------------------------------------------------- #


@dataclass(order=True, slots=True)
class _Scheduled:
    """One clock entry; ``at`` and ``sequence`` decide the firing order."""

    at: float
    sequence: int
    callback: Callable[[], None] = field(compare=False)
    label: str = field(default="timer", compare=False)
    cancelled: bool = field(default=False, compare=False)


class ManualClock:
    """A virtual clock: nothing fires until ``advance()`` is called."""

    def __init__(self) -> None:
        self._now = 0.0
        self._queue: list[_Scheduled] = []
        self._sequence = 0
        self.errors: list[tuple[str, BaseException]] = []

    @property
    def now(self) -> float:
        return self._now

    @property
    def pending(self) -> int:
        return sum(1 for item in self._queue if not item.cancelled)

    def call_at(
        self, delay: float, callback: Callable[[], None], label: str
    ) -> _Scheduled:
        """Schedule ``callback`` after ``delay`` virtual seconds."""

        if delay < 0:
            raise ValueError("delay must not be negative")
        self._sequence += 1
        item = _Scheduled(self._now + delay, self._sequence, callback, label)
        heapq.heappush(self._queue, item)
        return item

    def cancel(self, item: _Scheduled) -> None:
        """Cancel an entry; removal from the heap happens lazily."""

        item.cancelled = True

    def advance(self, seconds: float) -> int:
        """Move virtual time forward and fire every entry that is now due.

        A callback that fails is recorded in ``errors`` and does not stop the
        other entries.  Callbacks that schedule new work relative to the current
        virtual time participate in the same ``advance()`` call, which is what
        makes ``interval`` and ``debounce`` deterministic.
        """

        if seconds < 0:
            raise ValueError("cannot advance the clock backwards")
        target = self._now + seconds
        fires = 0
        while True:
            item = self._queue[0] if self._queue else None
            if item is None or item.at > target:
                break
            heapq.heappop(self._queue)
            if item.cancelled:
                continue
            self._now = max(self._now, item.at)
            fires += 1
            if fires > _MAX_FIRES:
                raise HarnessError("clock advance exceeded the fire budget")
            try:
                item.callback()
            except BaseException as error:  # noqa: BLE001 - keep firing other entries
                self.errors.append((item.label, error))
        self._now = target
        self._compact()
        return fires

    def _compact(self) -> None:
        pending = self.pending
        if len(self._queue) > 64 and len(self._queue) > 2 * pending:
            self._queue = [item for item in self._queue if not item.cancelled]
            heapq.heapify(self._queue)


class TimerHandle:
    """One timeout or interval, owned by the effects of its calling fiber."""

    def __init__(
        self,
        clock: ManualClock,
        label: str,
        *,
        repeating: bool,
    ) -> None:
        self.clock = clock
        self.label = label
        self.repeating = repeating
        self.item: _Scheduled | None = None
        self.fires = 0
        self._disposer: Disposer | None = None

    @property
    def active(self) -> bool:
        return self.item is not None and not self.item.cancelled

    def cancel(self) -> None:
        """Cancel the schedule; calling it twice is a no-op."""

        if self.item is not None:
            self.clock.cancel(self.item)

    async def dispose(self) -> None:
        """Cancel and detach from the owner's effects exactly once."""

        if self._disposer is not None:
            await self._disposer.dispose()


class Debounced:
    """A callback that runs once after a quiet period; each ``arm()`` restarts it."""

    def __init__(
        self,
        clock: ManualClock,
        delay: float,
        callback: Callable[[], None],
        label: str,
    ) -> None:
        self.clock = clock
        self.delay = delay
        self.callback = callback
        self.label = label
        self.fires = 0
        self._item: _Scheduled | None = None
        self._disposer: Disposer | None = None

    def arm(self) -> None:
        """(Re)start the quiet period."""

        if self._disposer is not None and self._disposer.closed:
            raise InactiveError(f"owner is disposed; cannot arm {self.label!r}")
        if self._item is not None:
            self.clock.cancel(self._item)
        self._item = self.clock.call_at(self.delay, self._fire, self.label)

    def _fire(self) -> None:
        self._item = None
        self.fires += 1
        self.callback()

    async def dispose(self) -> None:
        if self._disposer is not None:
            await self._disposer.dispose()


class TimerService:
    """Ownership-safe timeout, interval, sleep and debounce over a manual clock."""

    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock

    @property
    def pending(self) -> int:
        return self.clock.pending

    def timeout(
        self,
        owner: Context,
        delay: float,
        callback: Callable[[], None],
        label: str = "timeout",
    ) -> TimerHandle:
        handle = TimerHandle(self.clock, label, repeating=False)

        def fire() -> None:
            handle.fires += 1
            handle.cancel()
            callback()

        handle.item = self.clock.call_at(delay, fire, label)
        handle._disposer = owner.fiber.effects.install(lambda: handle.cancel(), label)
        return handle

    def interval(
        self,
        owner: Context,
        period: float,
        callback: Callable[[], None],
        label: str = "interval",
    ) -> TimerHandle:
        """A fixed-rate repeating timer.

        The next tick is scheduled *before* the callback runs, so a failing
        callback cannot silently lose the interval: it keeps its cadence, the
        error is recorded by the clock, and the failure stays isolated to its own
        tick.
        """

        if period <= 0:
            raise ValueError("interval period must be positive")
        handle = TimerHandle(self.clock, label, repeating=True)

        def fire() -> None:
            handle.fires += 1
            handle.item = self.clock.call_at(period, fire, label)
            callback()

        handle.item = self.clock.call_at(period, fire, label)
        handle._disposer = owner.fiber.effects.install(lambda: handle.cancel(), label)
        return handle

    def debounce(
        self,
        owner: Context,
        delay: float,
        callback: Callable[[], None],
        label: str = "debounce",
    ) -> Debounced:
        if delay < 0:
            raise ValueError("debounce delay must not be negative")
        debounced = Debounced(self.clock, delay, callback, label)
        debounced._disposer = owner.fiber.effects.install(
            lambda: debounced._item and self.clock.cancel(debounced._item), label
        )
        return debounced

    async def sleep(self, owner: Context, delay: float, label: str = "sleep") -> None:
        """Await virtual time; disposing the owner cancels the sleep."""

        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()

        def fire() -> None:
            if not future.done():
                future.set_result(None)

        item = self.clock.call_at(delay, fire, label)

        def release() -> None:
            self.clock.cancel(item)
            if not future.done():
                future.cancel()

        disposer = owner.fiber.effects.install(release, label)
        try:
            await future
        finally:
            await disposer.dispose()


# --------------------------------------------------------------------------- #
# Effects, fibers, context
# --------------------------------------------------------------------------- #


class Disposer:
    """Idempotent, awaitable cleanup handle owned by exactly one scope."""

    def __init__(self, execute: Callable[[], object], label: str) -> None:
        self.label = label
        self._execute = execute
        self._task: asyncio.Task[None] | None = None
        self.closed = False

    def __call__(self) -> Awaitable[None]:
        return self.dispose()

    async def dispose(self) -> None:
        if self.closed:
            if self._task is not None:
                await asyncio.shield(self._task)
            return
        self.closed = True

        async def run() -> None:
            result = self._execute()
            if inspect.isawaitable(result):
                await cast(Awaitable[object], result)

        self._task = asyncio.ensure_future(run())
        await asyncio.shield(self._task)


class EffectScope:
    """Ordered cleanup stack: reverse order, idempotent, fully awaited."""

    def __init__(self) -> None:
        self._items: list[Disposer] = []
        self.closed = False

    def install(self, execute: Callable[[], object], label: str) -> Disposer:
        if self.closed:
            raise InactiveError(f"owner is disposed; cannot install {label!r}")
        disposer = Disposer(execute, label)
        self._items.append(disposer)
        return disposer

    def __len__(self) -> int:
        return len(self._items)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        items, self._items = self._items, []
        errors: list[BaseException] = []
        for disposer in reversed(items):
            try:
                await disposer.dispose()
            except BaseException as error:  # noqa: BLE001 - finish the remaining cleanups
                errors.append(error)
        if errors:
            raise BaseExceptionGroup("effect cleanup failed", errors)


class FiberState(StrEnum):
    """Portable Fiber states used by the teaching runtime."""

    PENDING = "PENDING"
    LOADING = "LOADING"
    ACTIVE = "ACTIVE"
    UNLOADING = "UNLOADING"
    FAILED = "FAILED"
    DISPOSED = "DISPOSED"


class RootFiber:
    """The root owner: effects without a plugin activation."""

    name = "root"
    state = FiberState.ACTIVE

    def __init__(self) -> None:
        self.effects = EffectScope()

    async def dispose(self) -> None:
        if self.state is FiberState.DISPOSED:
            return
        self.state = FiberState.DISPOSED
        await self.effects.close()


class Fiber:
    """One plugin activation, owned by one parent context."""

    _counter = 0

    def __init__(
        self,
        parent: Context,
        plugin: Plugin,
        config: object,
        *,
        name: str,
        inject: Mapping[str, object] | None = None,
    ) -> None:
        Fiber._counter += 1
        self.uid = Fiber._counter
        self.parent = parent
        self._scope = parent._scope
        self.context = Context.derive(parent, self)
        self.plugin = plugin
        self.name = name
        self.inject = normalize_inject(inject)
        self.config = config
        self.state = FiberState.PENDING
        self.version = 0
        self.error: BaseException | None = None
        self.effects = EffectScope()
        self.children: list[Fiber] = []
        parent_fiber = parent.fiber
        if isinstance(parent_fiber, Fiber):
            parent_fiber.children.append(self)

    def _validator(self) -> object:
        return getattr(self.plugin, "Config", None)

    def _ready(self) -> bool:
        services = self._scope.services
        return all(name in services for name in self.inject)

    async def refresh(self) -> None:
        """Re-evaluate dependencies: activate, or unload into a new epoch."""

        if self.state is FiberState.PENDING and self._ready():
            await self.load()
            return
        if self.state is FiberState.ACTIVE and not self._ready():
            failure: BaseException | None = None
            try:
                await self._release()
            except BaseException as error:  # noqa: BLE001 - the state transition still happens
                failure = error
            self.state = FiberState.PENDING
            self.version += 1
            if failure is not None:
                raise failure

    async def load(self) -> None:
        """Run the plugin once; a failure rolls back every partial effect."""

        if self.state is not FiberState.PENDING:
            return
        if not self._ready():
            return
        self.state = FiberState.LOADING
        self.error = None
        try:
            self.config = validate_config(self._validator(), self.config)
            callback = _plugin_callback(self.plugin)
            result = callback(self.context, self.config)
            if inspect.isawaitable(result):
                result = await cast(Awaitable[object], result)
            if result is not None:
                await self.context.effect(lambda: result, f"plugin({self.name!r})")
        except BaseException as error:
            scope, self.effects = self.effects, EffectScope()
            self.error = error
            try:
                await scope.close()
            except BaseException as cleanup_error:  # noqa: BLE001 - aggregate rollback failures
                self.error = BaseExceptionGroup(
                    f"fiber {self.name!r} activation and rollback failed",
                    [error, cleanup_error],
                )
            self.state = FiberState.FAILED
            raise self.error from error
        self.state = FiberState.ACTIVE

    async def update(self, config: object) -> None:
        """Validate and apply configuration through one epoch change."""

        if self.state is FiberState.DISPOSED:
            raise InactiveError("cannot update a disposed fiber")
        validated = validate_config(self._validator(), config)
        if not self._ready():
            self.config = validated
            return
        await self._release()
        self.config = validated
        self.state = FiberState.PENDING
        # Dependents must observe the gap before this service comes back, so they
        # unload into a new epoch instead of keeping a stale service reference.
        await self.context.stabilize()
        if self.state is FiberState.PENDING:
            await self.load()
        self.version += 1

    async def dispose(self) -> None:
        """Dispose children first, then own effects; never reactivate."""

        if self.state is FiberState.DISPOSED:
            return
        self.state = FiberState.UNLOADING
        errors: list[BaseException] = []
        for child in reversed(self.children):
            try:
                await child.dispose()
            except BaseException as error:  # noqa: BLE001 - dispose every sibling
                errors.append(error)
        self.children.clear()
        try:
            await self.effects.close()
        except BaseException as error:  # noqa: BLE001 - aggregate the whole subtree
            errors.append(error)
        self.state = FiberState.DISPOSED
        self._scope.unregister(self)
        if errors:
            raise BaseExceptionGroup(f"fiber {self.name!r} cleanup failed", errors)

    async def _release(self) -> None:
        """Drop the current epoch's resources and prepare a fresh effect scope."""

        errors: list[BaseException] = []
        for child in reversed(self.children):
            try:
                await child.dispose()
            except BaseException as error:  # noqa: BLE001 - release every child
                errors.append(error)
        self.children.clear()
        scope, self.effects = self.effects, EffectScope()
        try:
            await scope.close()
        except BaseException as error:  # noqa: BLE001 - aggregate child and effect failures
            errors.append(error)
        if errors:
            raise BaseExceptionGroup(f"fiber {self.name!r} release failed", errors)


class Events:
    """Minimal owner-aware event bus: listeners are effects, not globals."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable[[object], None]]] = {}

    def on(self, name: str, handler: Callable[[object], None]) -> Callable[[], None]:
        handlers = self._handlers.setdefault(name, [])
        handlers.append(handler)

        def release() -> None:
            if handler in handlers:
                handlers.remove(handler)

        return release

    def emit(self, name: str, payload: object) -> None:
        for handler in tuple(self._handlers.get(name, ())):
            handler(payload)

    def count(self) -> int:
        return sum(len(handlers) for handlers in self._handlers.values())


class Scope:
    """Shared service store, live fiber registry and pending refresh queue."""

    def __init__(self) -> None:
        self.services: dict[str, object] = {}
        self.fibers: list[Fiber] = []
        self.events = Events()
        self.errors: list[tuple[str, BaseException]] = []
        self._pending: list[Fiber] = []

    def register(self, fiber: Fiber) -> None:
        self.fibers.append(fiber)

    def unregister(self, fiber: Fiber) -> None:
        if fiber in self.fibers:
            self.fibers.remove(fiber)

    def request(self) -> None:
        """Queue every live fiber for one refresh pass."""

        for fiber in self.fibers:
            if fiber not in self._pending:
                self._pending.append(fiber)

    def take(self) -> Fiber | None:
        return self._pending.pop(0) if self._pending else None

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
        self.fiber: RootFiber | Fiber = RootFiber()

    @classmethod
    def derive(cls, parent: Context, fiber: Fiber) -> Context:
        context = object.__new__(cls)
        context.root = parent.root
        context._scope = parent._scope
        context._meta = dict(parent._meta)
        context.fiber = fiber
        return context

    @staticmethod
    def is_context(value: object) -> bool:
        return isinstance(value, Context)

    def metadata(self, key: object) -> object | None:
        return self._meta.get(key)

    @property
    def events(self) -> Events:
        """The one event bus of this tree; the ``events`` service is this object."""

        return self._scope.events

    def get(self, name: str) -> object | None:
        """Look up a provided service; ``None`` means "not available now"."""

        return self._scope.services.get(name)

    def has(self, name: str) -> bool:
        return name in self._scope.services

    def provide(self, name: str, value: object) -> Disposer:
        """Publish a service owned by the current fiber."""

        if name in self._scope.services:
            raise HarnessError(f"service {name!r} is already provided")
        self._scope.services[name] = value

        def release() -> None:
            self._scope.remove(name, value)

        disposer = self.fiber.effects.install(release, f"provide({name!r})")
        self._scope.request()
        return disposer

    def on(self, name: str, handler: Callable[[object], None]) -> Disposer:
        """Subscribe to an event; the subscription belongs to this fiber."""

        release = self._scope.events.on(name, handler)
        return self.fiber.effects.install(release, f"on({name!r})")

    async def effect(
        self, execute: Callable[[], object], label: str = "effect"
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
                except BaseException as error:  # noqa: BLE001 - run the remaining cleanups
                    errors.append(error)
            if errors:
                raise BaseExceptionGroup(f"{label} cleanup failed", errors)

        return self.fiber.effects.install(release, label)

    async def plugin(
        self,
        plugin: Plugin,
        config: object = None,
        *,
        name: str,
        inject: Mapping[str, object] | None = None,
    ) -> Fiber:
        """Create a fiber and run its first dependency refresh."""

        fiber = Fiber(self, plugin, config, name=name, inject=inject)
        self._scope.register(fiber)
        try:
            await fiber.refresh()
        except BaseException:
            try:
                await fiber.dispose()
            except BaseException as cleanup_error:  # noqa: BLE001 - activation error wins
                # The failure the caller must see is the activation error, but the
                # rollback failure is recorded rather than silently dropped.
                self._scope.errors.append((fiber.name, cleanup_error))
            raise
        return fiber

    async def stabilize(self) -> None:
        """Drain the refresh queue until no dependency change is left."""

        guard = 0
        while True:
            fiber = self._scope.take()
            if fiber is None:
                return
            guard += 1
            if guard > _STABILIZE_LIMIT:
                raise HarnessError("dependency stabilization did not converge")
            try:
                await fiber.refresh()
            except BaseException as error:  # noqa: BLE001 - record and keep draining
                self._scope.errors.append((fiber.name, error))

    async def aclose(self) -> None:
        await self.root.fiber.dispose()


def normalize_inject(value: object) -> dict[str, object]:
    """Normalize a plugin ``inject`` declaration into a name-keyed mapping."""

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


def _plugin_callback(plugin: Plugin) -> Callable[[Context, object], object]:
    apply = getattr(plugin, "apply", None)
    if callable(apply):
        return cast(Callable[[Context, object], object], apply)
    entry = getattr(plugin, "plugin", None)
    if callable(entry):
        return cast(Callable[[Context, object], object], entry)
    if callable(plugin):
        return cast(Callable[[Context, object], object], plugin)
    raise TypeError(f"plugin {plugin!r} is neither callable nor has apply()/plugin")


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


# --------------------------------------------------------------------------- #
# Configuration composition
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SourceLocation:
    """Where a declaration came from; ``line`` is ``None`` when unknown."""

    path: Path
    line: int | None = None

    def __str__(self) -> str:
        return str(self.path) if self.line is None else f"{self.path}:{self.line}"


@dataclass(frozen=True, slots=True)
class ParsedEntry:
    """One immutable plugin declaration with a stable id."""

    id: str
    module: str
    config: object = None
    disabled: bool = False
    inject: Mapping[str, object | None] = field(
        default_factory=lambda: MappingProxyType({})
    )
    children: tuple[ParsedEntry, ...] = ()
    location: SourceLocation = field(
        default_factory=lambda: SourceLocation(Path("<memory>"))
    )


class MappingAdapter:
    """Read one configuration file into a mapping without interpreting it."""

    def read(self, source: Path) -> Mapping[str, object]:  # pragma: no cover - protocol
        raise NotImplementedError


class YamlMappingAdapter(MappingAdapter):
    """PyYAML adapter; only ``safe_load`` is used, so no Python objects are built."""

    def read(self, source: Path) -> Mapping[str, object]:
        try:
            document = yaml.safe_load(source.read_text())
        except yaml.YAMLError as error:
            raise ValueError(str(error)) from error
        if document is None:
            return {}
        if not isinstance(document, Mapping):
            raise TypeError("top-level YAML document must be a mapping")
        return cast(Mapping[str, object], document)


_SOURCE = "_source"
_OVERLAY_FIELDS = frozenset({"module", "config", "disabled", "inject"})
_MERGED_FIELDS = frozenset({"config", "inject"})


class ConfigComposer:
    """Expand relative includes, apply id-addressed overlays, keep provenance."""

    def __init__(self, adapter: MappingAdapter | None = None) -> None:
        self.adapter = adapter or YamlMappingAdapter()

    def load(self, source: Path) -> tuple[ParsedEntry, ...]:
        resolved = source.resolve()
        return self._build(self._compose(resolved, ()), resolved)

    def _compose(
        self,
        source: Path,
        stack: tuple[Path, ...],
    ) -> list[dict[str, object]]:
        if source in stack:
            chain = " -> ".join(str(path) for path in (*stack, source))
            raise ConfigError(f"include cycle detected: {chain}")
        document = self._read(source, stack)
        plugins: list[dict[str, object]] = []
        for included in self._includes(document, source):
            plugins.extend(self._compose(included, (*stack, source)))
        plugins.extend(self._annotate(document.get("plugins"), source))
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
            raise ConfigError(detail, cause=error) from error
        except (TypeError, ValueError) as error:
            raise ConfigError(f"invalid configuration: {error}", cause=error) from error
        if not isinstance(document, Mapping):
            raise ConfigError("configuration document must be a mapping")
        return cast(Mapping[str, object], document)

    @staticmethod
    def _includes(document: Mapping[str, object], source: Path) -> tuple[Path, ...]:
        value = document.get("include")
        if value is None:
            return ()
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            values = cast(list[str], value)
        else:
            raise ConfigError(
                f"{source}: 'include' must be a path or an array of paths"
            )
        return tuple((source.parent / item).resolve() for item in values)

    @classmethod
    def _annotate(cls, plugins: object, source: Path) -> list[dict[str, object]]:
        if plugins is None:
            return []
        if not isinstance(plugins, list):
            raise ConfigError(f"{source}: top-level 'plugins' must be an array")
        result: list[dict[str, object]] = []
        for value in plugins:
            if not isinstance(value, Mapping):
                raise ConfigError(f"{source}: plugin entry must be a mapping")
            entry = dict(cast(Mapping[str, object], value))
            entry.setdefault(_SOURCE, source)
            children = entry.get("plugins", [])
            if isinstance(children, list):
                entry["plugins"] = cls._annotate(children, source)
            result.append(entry)
        return result

    @classmethod
    def _index(
        cls,
        plugins: list[dict[str, object]],
        source: Path,
    ) -> dict[str, dict[str, object]]:
        index: dict[str, dict[str, object]] = {}
        for entry in plugins:
            entry_id = entry.get("id")
            if isinstance(entry_id, str):
                if entry_id in index:
                    where = cls._location(entry, source)
                    raise ConfigError(
                        f"{where}: duplicate plugin entry id {entry_id!r}"
                    )
                index[entry_id] = entry
            children = entry.get("plugins", [])
            if isinstance(children, list):
                for child_id, child in cls._index(children, source).items():
                    if child_id in index:
                        where = cls._location(child, source)
                        raise ConfigError(
                            f"{where}: duplicate plugin entry id {child_id!r}"
                        )
                    index[child_id] = child
        return index

    @classmethod
    def _location(cls, entry: Mapping[str, object], fallback: Path) -> Path:
        path = entry.get(_SOURCE, fallback)
        return path if isinstance(path, Path) else fallback

    def _apply_overlays(
        self,
        plugins: list[dict[str, object]],
        overlays: object,
        source: Path,
    ) -> None:
        if overlays is None:
            return
        if not isinstance(overlays, Mapping):
            raise ConfigError(f"{source}: 'overlays' must be a mapping")
        index = self._index(plugins, source)
        for entry_id, patch in cast(Mapping[object, object], overlays).items():
            if not isinstance(entry_id, str) or not isinstance(patch, Mapping):
                raise ConfigError(
                    f"{source}: overlay keys must be entry ids and values mappings"
                )
            target = index.get(entry_id)
            if target is None:
                raise ConfigError(
                    f"{source}: overlay target {entry_id!r} does not exist"
                )
            raw = cast(Mapping[str, object], patch)
            unknown = [key for key in raw if key not in _OVERLAY_FIELDS]
            if unknown:
                raise ConfigError(
                    f"{source}: overlay for {entry_id!r} has unsupported field"
                    f" {unknown[0]!r}"
                )
            for key, item in raw.items():
                if key == "module":
                    target[_SOURCE] = source
                if key in _MERGED_FIELDS and isinstance(item, Mapping):
                    previous = target.get(key)
                    if not isinstance(previous, Mapping):
                        previous = {}
                    target[key] = _deep_merge(
                        cast(Mapping[str, object], previous),
                        cast(Mapping[str, object], item),
                    )
                else:
                    target[key] = item

    def _build(
        self,
        plugins: list[dict[str, object]],
        source: Path,
    ) -> tuple[ParsedEntry, ...]:
        entries: list[ParsedEntry] = []
        for value in plugins:
            where = self._location(value, source)
            entry_id = value.get("id")
            if not isinstance(entry_id, str) or not entry_id:
                raise ConfigError(f"{where}: plugin entry requires a non-empty 'id'")
            module = value.get("module")
            if not isinstance(module, str) or not module:
                raise ConfigError(f"{where}: plugin entry requires a 'module'")
            disabled = value.get("disabled", False)
            if not isinstance(disabled, bool):
                raise ConfigError(f"{where}: plugin 'disabled' must be boolean")
            inject = value.get("inject", {})
            if not isinstance(inject, Mapping):
                raise ConfigError(f"{where}: plugin 'inject' must be a mapping")
            children = value.get("plugins", [])
            if not isinstance(children, list):
                raise ConfigError(f"{where}: nested 'plugins' must be an array")
            entries.append(
                ParsedEntry(
                    id=entry_id,
                    module=module,
                    config=value.get("config"),
                    disabled=disabled,
                    inject=dict(cast(Mapping[str, object], inject)),
                    children=self._build(
                        cast(list[dict[str, object]], children), source
                    ),
                    location=SourceLocation(where),
                )
            )
        return tuple(entries)


def _deep_merge(
    base: Mapping[str, object],
    overlay: Mapping[str, object],
) -> dict[str, object]:
    result: dict[str, object] = dict(base)
    for key, value in overlay.items():
        previous = result.get(key)
        if isinstance(previous, Mapping) and isinstance(value, Mapping):
            result[key] = _deep_merge(
                cast(Mapping[str, object], previous),
                cast(Mapping[str, object], value),
            )
        else:
            result[key] = value
    return result


def replace_module(
    entries: tuple[ParsedEntry, ...],
    entry_id: str,
    specifier: str,
    config: object = None,
    *,
    has_config: bool = False,
) -> tuple[ParsedEntry, ...]:
    """Return the forest with one entry's module (and optional config) replaced."""

    found = False

    def visit(entry: ParsedEntry) -> ParsedEntry:
        nonlocal found
        if entry.id == entry_id:
            found = True
            return replace(
                entry,
                module=specifier,
                config=config if has_config else entry.config,
            )
        return replace(entry, children=tuple(visit(child) for child in entry.children))

    result = tuple(visit(entry) for entry in entries)
    if not found:
        raise HarnessError(f"unknown entry id {entry_id!r}")
    return result


# --------------------------------------------------------------------------- #
# Module source
# --------------------------------------------------------------------------- #


class LocalModuleSource:
    """Resolve ``name:attribute`` specifiers to real files under a plugins dir.

    A specifier names a *module* plus the entry point inside it.  The module also
    declares ``inject`` and ``Config``, which is what lets the Loader build the
    dependency contract of the entry before activating it.

    The teaching source caches the imported module and never refreshes the
    process-level ``sys.modules`` entry: in the formal design that responsibility
    belongs to the host, which then calls ``Loader.replace()`` explicitly.
    """

    def __init__(self, directory: Path, *extra: Path) -> None:
        self.directories = (directory.resolve(), *(path.resolve() for path in extra))
        self._cache: dict[Path, object] = {}
        self._imports = 0

    def path_for(self, specifier: str) -> Path:
        name, separator, attribute = specifier.partition(":")
        if not name or not separator or not attribute:
            raise ResolveError(
                f"module specifier {specifier!r} must look like 'name:attribute'"
            )
        for directory in self.directories:
            candidate = directory / f"{name}.py"
            if candidate.is_file():
                return candidate
        raise ResolveError(f"cannot resolve plugin module {specifier!r}")

    def resolve(self, specifier: str) -> PluginRef:
        path = self.path_for(specifier)
        module = self._cache.get(path)
        if module is None:
            module = self._import(path)
            self._cache[path] = module
        attribute = specifier.partition(":")[2]
        target = getattr(module, attribute, None)
        if not callable(target):
            raise ResolveError(f"{path} has no callable entry point {attribute!r}")
        return PluginRef(
            specifier=specifier,
            path=path,
            module=module,
            attribute=attribute,
            callback=cast(Callable[[Context, object], object], target),
            inject=normalize_inject(getattr(module, "inject", None)),
            Config=getattr(module, "Config", None),
        )

    def _import(self, path: Path) -> object:
        self._imports += 1
        name = f"cordis_tutorial_chapter21_{path.stem}_{self._imports}"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:  # pragma: no cover - defensive
            raise ResolveError(f"cannot import plugin module {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module


# --------------------------------------------------------------------------- #
# Loader
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Entry:
    """Mutable runtime state for one stable parsed entry id."""

    parsed: ParsedEntry
    plugin: Plugin | None = None
    fiber: Fiber | None = None
    version: int = 0
    parent: Entry | None = None
    context: Context | None = None
    children: list[Entry] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.parsed.id


class EntryPlugin:
    """Per-entry adapter that adds declarative inject metadata and children."""

    def __init__(self, loader: Loader, entry: Entry, ref: PluginRef) -> None:
        self.loader = loader
        self.entry = entry
        self.ref = ref
        self.inject = dict(ref.inject) | dict(entry.parsed.inject)
        self.Config = ref.Config

    async def apply(self, context: Context, config: object) -> None:
        result = self.ref.callback(context, config)
        if inspect.isawaitable(result):
            result = await cast(Awaitable[object], result)
        if result is not None:
            await context.effect(lambda: result, f"loader.plugin({self.entry.id!r})")
        for parsed_child in self.entry.parsed.children:
            await self.loader.mount_entry(parsed_child, context, self.entry)


class Loader:
    """Mount parsed declarations and own update/replace transactions."""

    def __init__(self, context: Context, modules: LocalModuleSource) -> None:
        self.context = context
        self.modules = modules
        self.entries: dict[str, Entry] = {}
        self.roots: list[Entry] = []
        self.cleanup_errors: list[BaseException] = []
        self.updates = 0
        self.replacements = 0
        self._prepared: dict[str, PluginRef] = {}
        self._closed = False

    async def mount(self, entries: tuple[ParsedEntry, ...]) -> tuple[Entry, ...]:
        """Mount a forest; a failure disposes everything that was started."""

        if self._closed:
            raise HarnessError("loader is closed")
        if self.entries:
            raise HarnessError("loader already has a mounted tree")
        try:
            await self._mount_forest(entries)
        except BaseException:
            await self._dispose_forest()
            self._reset_forest()
            raise
        return tuple(self.roots)

    async def mount_entry(
        self,
        parsed: ParsedEntry,
        context: Context,
        parent: Entry | None,
    ) -> Entry:
        if parsed.id in self.entries:
            raise HarnessError(f"duplicate runtime entry id {parsed.id!r}")
        entry = Entry(parsed=parsed, parent=parent, context=context)
        self.entries[parsed.id] = entry
        if parent is None:
            self.roots.append(entry)
        else:
            parent.children.append(entry)
        if parsed.disabled:
            return entry
        prepared = self._prepared.pop(parsed.id, None)
        ref = prepared if prepared is not None else self.modules.resolve(parsed.module)
        entry.plugin = ref
        wrapper = EntryPlugin(self, entry, ref)
        entry.fiber = await context.plugin(
            wrapper,
            parsed.config,
            name=parsed.id,
            inject=wrapper.inject,
        )
        return entry

    async def update(self, entries: tuple[ParsedEntry, ...]) -> None:
        """Apply a config-only update in place, or replace the forest."""

        candidates = self._flatten(entries)
        current = {entry_id: entry.parsed for entry_id, entry in self.entries.items()}
        if candidates.keys() != current.keys() or any(
            self._identity(candidates[entry_id]) != self._identity(current[entry_id])
            for entry_id in current
        ):
            await self.replace(entries)
            return
        changes = [
            (self.entries[entry_id], current[entry_id], candidates[entry_id])
            for entry_id in current
            if current[entry_id].config != candidates[entry_id].config
        ]
        if any(candidate.children for _, _, candidate in changes):
            raise HarnessError(
                "config update for an entry with children requires replacement"
            )
        applied: list[tuple[Entry, ParsedEntry]] = []
        try:
            for entry, previous, candidate in changes:
                if entry.fiber is None:
                    raise HarnessError(f"entry {entry.id!r} is not active")
                await entry.fiber.update(candidate.config)
                entry.parsed = candidate
                entry.version += 1
                applied.append((entry, previous))
            for entry_id, candidate in candidates.items():
                self.entries[entry_id].parsed = candidate
        except BaseException as error:  # noqa: BLE001 - transaction boundary
            rollback_errors: list[BaseException] = []
            for entry, previous in reversed(applied):
                try:
                    if entry.fiber is not None:
                        await entry.fiber.update(previous.config)
                    entry.parsed = previous
                    entry.version += 1
                except BaseException as rollback_error:  # noqa: BLE001 - roll back every entry
                    rollback_errors.append(rollback_error)
            cause: BaseException = error
            if rollback_errors:
                cause = BaseExceptionGroup(
                    "loader update and rollback failed", [error, *rollback_errors]
                )
            raise HarnessError("failed to update configuration", cause=cause) from cause
        self.updates += 1
        await self.context.stabilize()

    async def replace(self, entries: tuple[ParsedEntry, ...]) -> None:
        """Replace the forest and restore the previous one when activation fails."""

        self._prepared = self._preflight(entries)
        previous = tuple(entry.parsed for entry in self.roots)
        try:
            await self._dispose_forest()
            self._reset_forest()
            try:
                await self._mount_forest(entries)
            except BaseException as error:
                self._prepared.clear()
                await self._dispose_forest()
                self._reset_forest()
                try:
                    await self._mount_forest(previous)
                except BaseException as rollback_error:  # noqa: BLE001 - aggregate both failures
                    cause = BaseExceptionGroup(
                        "replacement and rollback failed", [error, rollback_error]
                    )
                    raise HarnessError(
                        "failed to replace entry tree", cause=cause
                    ) from cause
                raise HarnessError(
                    f"replacement rejected: {error}", cause=error
                ) from error
        finally:
            self._prepared.clear()
        self.replacements += 1

    async def close(self) -> None:
        """Dispose every root through the normal fiber ownership path."""

        if self._closed:
            return
        self._closed = True
        await self._dispose_forest()
        self._reset_forest()

    async def _mount_forest(self, entries: tuple[ParsedEntry, ...]) -> None:
        for parsed in entries:
            await self.mount_entry(parsed, self.context, None)
        await self.context.stabilize()

    def _preflight(
        self,
        entries: tuple[ParsedEntry, ...],
        disabled: bool = False,
    ) -> dict[str, PluginRef]:
        result: dict[str, PluginRef] = {}
        for entry in entries:
            inactive = disabled or entry.disabled
            if not inactive:
                result[entry.id] = self.modules.resolve(entry.module)
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
        for entry in reversed(self.roots):
            if entry.fiber is None:
                continue
            try:
                await entry.fiber.dispose()
            except BaseException as error:  # noqa: BLE001 - dispose every remaining root
                self.cleanup_errors.append(error)

    def _reset_forest(self) -> None:
        self.entries.clear()
        self.roots.clear()


# --------------------------------------------------------------------------- #
# Host-driven HMR
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    """One change the host decided to report; the harness never detects them."""

    kind: str
    entry: str | None = None
    specifier: str | None = None
    config: object = None
    has_config: bool = False


class ChangeSource:
    """Host-owned change feed with explicit subscriptions; no OS watcher."""

    def __init__(self) -> None:
        self._handlers: list[Callable[[ChangeEvent], None]] = []

    @property
    def subscription_count(self) -> int:
        return len(self._handlers)

    def subscribe(self, handler: Callable[[ChangeEvent], None]) -> Disposer:
        self._handlers.append(handler)

        def release() -> None:
            if handler in self._handlers:
                self._handlers.remove(handler)

        return Disposer(release, "change source subscription")

    def publish(self, event: ChangeEvent) -> None:
        for handler in tuple(self._handlers):
            handler(event)


class ActivationGate:
    """A latch that lets a test hold a candidate in ``LOADING`` state."""

    def __init__(self) -> None:
        self.arrived = asyncio.Event()
        self._released = asyncio.Event()
        self._open = True
        self._released.set()

    def close(self) -> None:
        self._open = False
        self._released = asyncio.Event()

    def release(self) -> None:
        self._open = True
        self._released.set()

    async def wait(self) -> None:
        if self._open:
            return
        self.arrived.set()
        await self._released.wait()


class Trace:
    """Ordered records shared by the plugins; the harness owns the instance."""

    def __init__(self) -> None:
        self.records: list[str] = []

    def add(self, record: str) -> None:
        self.records.append(record)

    def count(self, prefix: str) -> int:
        return sum(1 for record in self.records if record.startswith(prefix))

    def order(self, record: str) -> int:
        return self.records.index(record)


class HotReloader:
    """Debounce host changes, prefer config updates, replace on module changes."""

    def __init__(self, harness: MiniHarness, *, debounce: float = 0.05) -> None:
        self.harness = harness
        self.debounce = debounce
        self.current: tuple[ParsedEntry, ...] | None = None
        self.pending_events: list[ChangeEvent] = []
        self.applied = 0
        self.config_updates = 0
        self.replacements = 0
        self.rejected = 0
        self.last_error: HarnessError | None = None
        self._dirty = False
        self._subscription: Disposer | None = None
        self._debounced = harness.timer.debounce(
            harness.context, debounce, self._mark_dirty, label="hmr.debounce"
        )

    async def start(self) -> None:
        """Compose the root file, mount the forest, then subscribe to changes."""

        parsed = self.harness.composer.load(self.harness.config_path)
        await self.harness.loader.mount(parsed)
        self.current = parsed
        self._subscription = self.harness.source.subscribe(self.notify)

    def notify(self, event: ChangeEvent) -> None:
        """Receive one host change and restart the debounce window."""

        if event.kind not in {"config", "module"}:
            raise HarnessError(f"unsupported change kind {event.kind!r}")
        self.pending_events.append(event)
        self._debounced.arm()

    def _mark_dirty(self) -> None:
        self._dirty = True

    async def flush(self) -> int:
        """Apply the debounced batch; return how many batches were applied."""

        if not self._dirty or not self.pending_events:
            return 0
        self._dirty = False
        batch, self.pending_events = self.pending_events, []
        self.applied += 1
        try:
            await self._apply(batch)
        except HarnessError as error:
            self.last_error = error
            self.rejected += 1
        return 1

    async def _apply(self, batch: list[ChangeEvent]) -> None:
        module_events = [event for event in batch if event.kind == "module"]
        if module_events:
            await self._replace_module(module_events[-1])
            return
        await self._update_config()

    async def _replace_module(self, event: ChangeEvent) -> None:
        if self.current is None:
            raise HarnessError("hot reloader has not been started")
        if event.entry is None or event.specifier is None:
            raise HarnessError("module change needs an entry id and a specifier")
        candidate = replace_module(
            self.current,
            event.entry,
            event.specifier,
            event.config,
            has_config=event.has_config,
        )
        await self.harness.loader.replace(candidate)
        self.current = candidate
        self.replacements += 1

    async def _update_config(self) -> None:
        if self.current is None:
            raise HarnessError("hot reloader has not been started")
        candidate = self.harness.composer.load(self.harness.config_path)
        if candidate == self.current:
            return
        await self.harness.loader.update(candidate)
        self.current = candidate
        self.config_updates += 1

    async def close(self) -> None:
        if self._subscription is not None:
            await self._subscription.dispose()
            self._subscription = None
        await self._debounced.dispose()
        self.pending_events.clear()
        self._dirty = False


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


class MiniHarness:
    """Wire the teaching stack together and expose an observable snapshot."""

    def __init__(
        self,
        config: Path,
        *,
        plugins_dir: Path | None = None,
        debounce: float = 0.05,
    ) -> None:
        self.config_path = config.resolve()
        self.plugins_dir = (
            plugins_dir
            if plugins_dir is not None
            else Path(__file__).resolve().parent / "plugins"
        )
        self.clock = ManualClock()
        self.timer = TimerService(self.clock)
        self.trace = Trace()
        self.gate = ActivationGate()
        self.source = ChangeSource()
        self.context = Context()
        self.context.provide("timer", self.timer)
        self.context.provide("events", self.context.events)
        self.context.provide("trace", self.trace)
        self.context.provide("gate", self.gate)
        self.modules = LocalModuleSource(self.plugins_dir)
        self.composer = ConfigComposer()
        self.loader = Loader(self.context, self.modules)
        self.reloader = HotReloader(self, debounce=debounce)
        self._closed = False

    @property
    def events(self) -> Events:
        """The harness-owned event bus; listeners are effects of their fibers."""

        return self.context.events

    async def start(self) -> None:
        await self.reloader.start()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.reloader.close()
        await self.loader.close()
        await self.context.aclose()

    def request(self, payload: str) -> str:
        """The stable request entry; ``PENDING`` means the route is not ready yet."""

        requests = self.context.get("requests")
        if requests is None:
            return "PENDING"
        return cast(str, requests.request(payload))

    def snapshot(self) -> dict[str, int]:
        """Every counter that must return to zero after ``close()``."""

        fibers = self.context._scope.fibers
        return {
            "services": len(self.context._scope.services),
            "fibers": len(fibers),
            "entries": len(self.loader.entries),
            "listeners": self.events.count(),
            "timer_handles": self.timer.pending,
            "watcher_subscriptions": self.source.subscription_count,
            "effects": len(self.context.fiber.effects)
            + sum(len(fiber.effects) for fiber in fibers),
            "pending_events": len(self.reloader.pending_events),
        }


__all__ = [
    "ActivationGate",
    "ChangeEvent",
    "ChangeSource",
    "ConfigComposer",
    "ConfigError",
    "Context",
    "Debounced",
    "Disposer",
    "EffectScope",
    "Entry",
    "EntryPlugin",
    "Events",
    "Fiber",
    "FiberState",
    "HarnessError",
    "HotReloader",
    "InactiveError",
    "Loader",
    "LocalModuleSource",
    "ManualClock",
    "MappingAdapter",
    "MiniHarness",
    "ParsedEntry",
    "PluginRef",
    "ResolveError",
    "RootFiber",
    "Scope",
    "SourceLocation",
    "TimerHandle",
    "TimerService",
    "Trace",
    "YamlMappingAdapter",
    "normalize_inject",
    "replace_module",
    "validate_config",
]
