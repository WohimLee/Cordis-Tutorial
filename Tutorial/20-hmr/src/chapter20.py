"""Safe replacement of running plugins for the tutorial Cordis runtime (20).

This chapter is the smallest runtime that can answer one question: *who is
allowed to replace a running plugin, and what happens to the resources of the
generation that was replaced?*  Everything is self-contained and importable
with the standard library only; it deliberately does **not** import ``cordis``
or any earlier tutorial chapter.

The pieces, in the order the chapter builds them:

* a tiny teaching Core (``EffectScope``/``Disposer``, ``Fiber`` with an
  activation epoch, ``Context`` with a service table) that is smaller than the
  one in chapter 17 because this chapter is not about Fibers;
* ``ManualClock`` + ``debounce_burst``: the host decides *when* something
  changed, the clock decides *when the burst is over*, and nothing here sleeps;
* ``ChangeEvent``: the **host** feeds change notifications in.  The teaching
  implementation never watches files, never reads mtimes and never touches
  ``importlib``;
* ``MappingModuleSource``: a mapping-driven fake module source that can be
  asked for a concrete version, so "v2 is broken, keep v1" needs no real
  module reload;
* ``HotReloader``: debounce -> classify -> config update or candidate
  replacement, with the transaction boundary that keeps the previous
  generation alive when a candidate fails;
* ``EpochState``/``HotReloadTarget``: the observable proof that a replaced
  generation's effects, listeners and tasks were all released, exactly once.

``cordis-py`` currently ships only a host-driven reload foundation
(``ConfigReloader.start()``/``reload()``).  There is no OS watcher, no file
mtime monitoring and no module-cache invalidation anywhere in it, so this
chapter is a *teaching candidate contract*, not evidence of equivalence with
``@deepseek-ai/cordis-plugin-hmr`` 1.0.15.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import cast

Callback = Callable[["Context", object], object]
Cleanup = Callable[[], object]
Listener = Callable[[object], object]


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class HotReloadError(Exception):
    """Base error for the teaching hot-reload runtime."""

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        self.cause = cause
        super().__init__(message)


class InvalidChangeError(HotReloadError):
    """A host-supplied :class:`ChangeEvent` cannot be applied."""


class StaleEpochError(HotReloadError):
    """A callback, listener, task or scope outlived its own generation."""


class ExecutionError(HotReloadError):
    """A lifecycle step failed: resolution, activation or cleanup."""

    def __init__(
        self,
        message: str,
        *,
        phase: str,
        cause: BaseException | None = None,
    ) -> None:
        self.phase = phase
        super().__init__(message, cause=cause)


# --------------------------------------------------------------------------- #
# Teaching Core: effects, fibers, context
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class GenerationSnapshot:
    """What one finished generation released, kept after the target moves on."""

    generation: int
    entry: str
    cleanup_counts: Mapping[str, int]
    effects: bool
    listeners: bool
    tasks: bool

    @property
    def clean(self) -> bool:
        """True when the generation released effects, listeners and tasks."""

        return not (self.effects or self.listeners or self.tasks)

    def released(self, label: str) -> int:
        """How many times one cleanup label ran for this generation."""

        return self.cleanup_counts.get(label, 0)


@dataclass(slots=True)
class EpochState:
    """Observable cleanup state of one plugin generation."""

    generation: int
    entry: str
    effects: bool = True
    listeners: bool = True
    tasks: bool = True

    def mark_clean(self) -> None:
        self.effects = False
        self.listeners = False
        self.tasks = False

    def check(self) -> None:
        """Raise when any resource of this generation is still registered."""

        leaked = [
            name
            for name, alive in (
                ("effects", self.effects),
                ("listeners", self.listeners),
                ("tasks", self.tasks),
            )
            if alive
        ]
        if leaked:
            raise AssertionError(f"generation {self.generation} leaked {leaked}")


class Disposer:
    """Idempotent, awaitable cleanup handle owned by exactly one scope."""

    def __init__(self, execute: Callable[[], object], label: str) -> None:
        self.label = label
        self.runs = 0
        self._execute = execute
        self._ran = False

    def dispose(self) -> None:
        if self._ran:
            return
        self._ran = True
        self.runs += 1
        self._execute()


class EffectScope:
    """Ordered cleanup stack: reverse order, idempotent, fully awaited."""

    def __init__(self) -> None:
        self._items: list[Disposer] = []
        self.closed = False

    def install(self, execute: Callable[[], object], label: str) -> Disposer:
        if self.closed:
            raise StaleEpochError("scope is already closed")
        disposer = Disposer(execute, label)
        self._items.append(disposer)
        return disposer

    def __len__(self) -> int:
        return len(self._items)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        errors: list[BaseException] = []
        for disposer in reversed(self._items):
            try:
                disposer.dispose()
            except BaseException as error:  # noqa: BLE001 - cleanup continues after the first failure
                errors.append(error)
        self._items.clear()
        if errors:
            raise BaseExceptionGroup("effect cleanup failed", errors)


def close_scope(scope: EffectScope) -> BaseException | None:
    """Close a scope, returning the aggregated cleanup failure instead of raising."""

    try:
        scope.close()
    except BaseException as error:  # noqa: BLE001 - the caller decides how to report it
        return error
    return None


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


class Fiber:
    """One plugin activation with a monotonically increasing epoch."""

    _counter = 0

    def __init__(self, context: Context, callback: Callback, config: object) -> None:
        Fiber._counter += 1
        self.uid = Fiber._counter
        self.context = context
        self.callback = callback
        self.config = config
        self.epoch = 0
        self.target: HotReloadTarget | None = None
        self.scope: EffectScope | None = None
        self.disposers: list[Disposer] = []

    def activate(
        self,
        config: object,
        *,
        name: str,
        target: HotReloadTarget | None = None,
        scope: EffectScope | None = None,
    ) -> tuple[EffectScope, list[Disposer]]:
        """Run one activation epoch and collect what it owns.

        The caller supplies the *fresh* scope (and, on first activation, the
        generation target) so that no two epochs ever share one cleanup stack.
        If activation fails, everything this attempt installed is released
        before the exception propagates and the caller keeps the old epoch.
        """

        owner = self.target if target is None else target
        if owner is None:
            raise StaleEpochError("fiber has no generation target")
        effects = EffectScope() if scope is None else scope
        disposers: list[Disposer] = []
        install = owner.bind(effects)
        self.context._bind(owner, effects, install, generation=owner.generation)
        try:
            result = self.callback(self.context, config)
            if inspect.isawaitable(result):
                raise TypeError("async plugin callbacks are out of scope in chapter 20")
            if result is not None:
                cleanup = cast(Cleanup, result)
                if not callable(cleanup):
                    raise ExecutionError(
                        f"plugin {name!r} returned a non-callable cleanup",
                        phase="activate",
                    )

                def release() -> None:
                    outcome = cleanup()
                    if inspect.isawaitable(outcome):
                        raise TypeError("async cleanups are out of scope in chapter 20")

                disposers.append(install(release, f"plugin: {name}"))
        except BaseException:
            self.context._detach()
            close_scope(effects)
            raise
        self.config = config
        self.target = owner
        self.scope = effects
        self.disposers = disposers
        self.epoch += 1
        return effects, disposers


@dataclass(slots=True)
class _ListenerRecord:
    generation: int
    callback: Listener


class Scope:
    """Root-shared service store, listener registry and service epoch."""

    def __init__(self) -> None:
        self.services: dict[str, object] = {}
        self.service_epoch = 0
        self.listeners: dict[int, _ListenerRecord] = {}
        self.next_listener_id = 0


class Context:
    """The scope one generation receives; a root context in this chapter."""

    def __init__(self, target: HotReloadTarget | None = None) -> None:
        self.root = self
        self._scope = Scope()
        self.registry: dict[str, object] = {}
        self.effects = EffectScope()
        # The generation label this context was created for.  It is kept even
        # after ``_detach`` so a stale context can still refuse new work.
        self._label: HotReloadTarget | None = target
        self._target: HotReloadTarget | None = None
        self._scope_effects: EffectScope | None = None
        self._install: Callable[[Cleanup, str], Disposer] | None = None
        self._generation = 0
        self._next_effect = 0

    # -- ownership ---------------------------------------------------------- #

    def _bind(
        self,
        target: HotReloadTarget,
        scope: EffectScope,
        install: Callable[[Cleanup, str], Disposer],
        *,
        generation: int,
    ) -> None:
        """Point this context at the generation it is currently serving."""
        self._target = target
        self._scope_effects = scope
        self._install = install
        self._generation = generation

    def _detach(self) -> None:
        self._target = None
        self._scope_effects = None
        self._install = None

    def _owner_target(self) -> HotReloadTarget:
        if self._target is None:
            raise StaleEpochError("context has no active generation")
        return self._target

    def _owner_install(self) -> Callable[[Cleanup, str], Disposer]:
        if self._install is None:
            raise StaleEpochError("context has no active generation")
        return self._install

    # -- services ----------------------------------------------------------- #

    def provide(self, name: str, value: object) -> None:
        """Publish a service whose lifetime is this generation."""

        previous = self._scope.services.get(name)
        self._scope.services[name] = value
        self._scope.service_epoch += 1
        self._owner_target()

        def withdraw() -> None:
            if previous is None:
                self._scope.services.pop(name, None)
            else:
                self._scope.services[name] = previous
            self._scope.service_epoch += 1

        def release() -> None:
            # A service that a later generation already replaced is not ours
            # to withdraw, so its release is not charged to this epoch.
            if self._scope.services.get(name) is value:
                withdraw()

        self._owner_install()(release, f"provide: {name}")

    def get(self, name: str) -> object | None:
        return self._scope.services.get(name)

    def has(self, name: str) -> bool:
        return name in self._scope.services

    # -- registry and listeners --------------------------------------------- #

    def label(self) -> HotReloadTarget | None:
        """The generation label this context was created for, if any."""

        return self._label

    def set(self, key: str, value: object) -> None:
        """Register one generation-owned value in the context registry."""

        self._owner_target().registry[key] = value
        self.registry[key] = value

    def listener(self, callback: Listener) -> Listener:
        """Register an event subscription owned by the current generation."""

        if not callable(callback):
            raise TypeError("listener callback must be callable")
        target = self._owner_target()
        listener_id = self._scope.next_listener_id
        self._scope.next_listener_id += 1
        self._scope.listeners[listener_id] = _ListenerRecord(
            target.generation, callback
        )

        def release() -> None:
            record = self._scope.listeners.get(listener_id)
            if record is not None and record.generation == target.generation:
                self._scope.listeners.pop(listener_id, None)

        target.register_listener(listener_id, callback)
        self._owner_install()(release, f"listener: {listener_id}")
        return callback

    def listener_count(self) -> int:
        """How many subscriptions the shared scope currently holds."""

        return len(self._scope.listeners)

    def emit(self, payload: object) -> list[object]:
        """Fire the subscriptions of the live generation only.

        A callback registered by a replaced generation is never invoked again,
        even before its disposer has actually run.
        """

        generation = self._owner_target().generation
        results: list[object] = []
        for record in tuple(self._scope.listeners.values()):
            if record.generation != generation:
                continue
            results.append(record.callback(payload))
        return results

    def effect(self, setup: Callable[[], object]) -> None:
        """Run a setup callback now and own the cleanup it returns.

        The argument is the *setup*: it runs immediately.  Its return value is
        the cleanup, which only runs when this generation is released.
        """

        cleanup = setup()
        if inspect.isawaitable(cleanup):
            raise TypeError("async effect callbacks are out of scope in chapter 20")
        if cleanup is None:
            return
        if not callable(cleanup):
            raise TypeError("effect setup must return a cleanup callable")
        self._next_effect += 1
        self._owner_install()(cast(Cleanup, cleanup), f"effect: {self._next_effect}")

    def aclose(self) -> None:
        """Close the root scope once; repeated calls are no-ops."""

        self.effects.close()
        self._scope.listeners.clear()
        self.registry.clear()


# --------------------------------------------------------------------------- #
# Manual clock and debounce
# --------------------------------------------------------------------------- #


class TaskHandle:
    """One scheduled callback owned by the generation that scheduled it.

    ``cancel()`` is what makes HMR safe: a scheduled callback that has been
    cancelled never runs, even if its deadline has already been computed.
    """

    def __init__(
        self, due_ms: int, sequence: int, callback: Callable[[], object]
    ) -> None:
        self.due_ms = due_ms
        self.sequence = sequence
        self.callback = callback
        self.cancelled = False
        self.fired = 0
        self._cancel: Callable[[], None] | None = None

    def cancel(self) -> None:
        if self.cancelled:
            return
        self.cancelled = True
        if self._cancel is not None:
            self._cancel()

    def invoke(self) -> object:
        """Run the callback unless the handle was cancelled in the meantime."""

        if self.cancelled:
            return None
        self.cancelled = True
        self.fired += 1
        return self.callback()


class ManualClock:
    """A deterministic virtual clock; no real time and no ``asyncio.sleep``."""

    def __init__(self) -> None:
        self._now_ms = 0
        self._sequence = 0
        self._timers: list[tuple[int, int, Callable[[], object]]] = []
        self.closed = False

    @property
    def now_ms(self) -> int:
        return self._now_ms

    def call_later(self, delay_ms: int, callback: Callable[[], object]) -> TaskHandle:
        if self.closed:
            raise HotReloadError("clock is closed")
        if delay_ms < 0:
            raise ValueError("delay_ms must not be negative")
        self._sequence += 1
        handle = TaskHandle(self._now_ms + delay_ms, self._sequence, callback)
        handle._cancel = lambda: self._drop(handle)
        self._timers.append((handle.due_ms, handle.sequence, handle.invoke))
        return handle

    def advance(self, delta_ms: int) -> None:
        """Move virtual time forward and run every timer that becomes due."""

        if self.closed:
            raise HotReloadError("clock is closed")
        if delta_ms < 0:
            raise ValueError("delta_ms must not be negative")
        target = self._now_ms + delta_ms
        for _ in range(10_000):
            self._timers.sort(key=lambda item: (item[0], item[1]))
            if not self._timers or self._timers[0][0] > target:
                break
            due_ms, _, fire = self._timers.pop(0)
            self._now_ms = due_ms
            fire()
        else:
            raise HotReloadError("virtual clock did not settle; a timer looped")
        self._now_ms = target

    def pending(self) -> int:
        return len(self._timers)

    def close(self) -> None:
        self.closed = True
        self._timers.clear()

    def _drop(self, handle: TaskHandle) -> None:
        self._timers = [item for item in self._timers if item[2] != handle.invoke]


def debounce_burst(
    clock: ManualClock,
    callback: Callable[[], object],
    *,
    delay_ms: int,
) -> Callable[[], TaskHandle]:
    """Return a ``schedule`` function that merges a burst into one call."""

    handle: TaskHandle | None = None

    def schedule() -> TaskHandle:
        nonlocal handle
        if handle is not None:
            handle.cancel()
        handle = clock.call_later(delay_ms, callback)
        return handle

    return schedule


# --------------------------------------------------------------------------- #
# Host-fed change events
# --------------------------------------------------------------------------- #


_MISSING = object()


class ChangeKind(StrEnum):
    CONFIG = "config"
    MODULE = "module"


def _no_payload() -> object:
    return _MISSING


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    """One host-fed notification; the tutorial never detects changes itself."""

    kind: ChangeKind
    entry: str
    module: str | None = None
    version: int | None = None
    payload: object = field(default_factory=_no_payload)

    def __post_init__(self) -> None:
        if not self.entry:
            raise InvalidChangeError("change event requires a non-empty entry id")
        if self.kind is ChangeKind.CONFIG and not self.has_payload:
            raise InvalidChangeError("config change event requires a payload")
        if self.kind is ChangeKind.MODULE and not self.module:
            raise InvalidChangeError("module change event requires a module name")

    @property
    def has_payload(self) -> bool:
        return self.payload is not _MISSING


@dataclass(frozen=True, slots=True)
class ChangeBatch:
    """The merged result of one debounce window."""

    events: tuple[ChangeEvent, ...]

    @property
    def entry(self) -> str:
        return self.events[-1].entry

    @property
    def structural(self) -> bool:
        """Any module event in the burst wins over config-only events."""

        return any(event.kind is ChangeKind.MODULE for event in self.events)

    @property
    def kind(self) -> ChangeKind:
        return ChangeKind.MODULE if self.structural else ChangeKind.CONFIG

    @property
    def module(self) -> str | None:
        """The module of the last module event in the burst."""

        for event in reversed(self.events):
            if event.kind is ChangeKind.MODULE:
                return event.module
        return None

    @property
    def version(self) -> int | None:
        """The version of the last module event, if the host pinned one."""

        for event in reversed(self.events):
            if event.kind is ChangeKind.MODULE:
                return event.version
        return None

    @property
    def payload(self) -> object:
        """The payload of the last payload-carrying event in the burst."""

        for event in reversed(self.events):
            if event.has_payload:
                return event.payload
        return _MISSING


# --------------------------------------------------------------------------- #
# Mapping-driven module source
# --------------------------------------------------------------------------- #


class MappingModuleSource:
    """A mapping-driven fake module source with explicit candidate versions."""

    def __init__(self, table: Mapping[str, Mapping[int, object]]) -> None:
        self._table: dict[str, dict[int, object]] = {
            name: dict(versions) for name, versions in table.items()
        }
        self._pinned: dict[str, int] = {}

    def versions(self, name: str) -> tuple[int, ...]:
        return tuple(sorted(self._table.get(name, {})))

    def deployed(self, name: str) -> int | None:
        """The version the host currently claims is loaded for this module."""

        if name in self._pinned:
            return self._pinned[name]
        versions = self.versions(name)
        return versions[0] if versions else None

    def pin(self, name: str, version: int | None) -> None:
        """The host says "this is the code currently on disk for this module"."""

        if version is None:
            self._pinned.pop(name, None)
            return
        if version not in self._table.get(name, {}):
            raise KeyError(f"{name!r} has no version {version}")
        self._pinned[name] = version

    def add(self, name: str, version: int, module: object) -> None:
        """The host published a new candidate module version."""

        self._table.setdefault(name, {})[version] = module

    def resolve(self, name: str, version: int | None = None) -> Callback:
        """Return the callback for one module version or raise a located error.

        ``version=None`` means "whatever the host currently deploys"; a
        :class:`ChangeEvent` that pins a version overrides that choice, which is
        how a candidate replacement is expressed without touching
        ``importlib``.
        """

        versions = self._table.get(name)
        if not versions:
            raise ExecutionError(f"cannot resolve module {name!r}", phase="resolve")
        if version is None:
            version = self.deployed(name)
        module = versions.get(cast(int, version))
        if module is None:
            raise ExecutionError(
                f"module {name!r} has no version {version}", phase="resolve"
            )
        callback = getattr(module, "apply", module)
        if not callable(callback):
            raise ExecutionError(
                f"module {name!r} v{version} is not callable", phase="resolve"
            )
        return cast(Callback, callback)


# --------------------------------------------------------------------------- #
# Generation state
# --------------------------------------------------------------------------- #


class HotReloadTarget:
    """The single transactional owner of one entry's generations.

    A target outlives the code that activates it: one ``HotReloader`` keeps one
    target per entry and re-labels it for every new generation.  Three
    ledgers make "released exactly once" observable:

    * ``cleanup_counts`` is the running ledger of the resources installed by
      the current epoch;
    * ``retired[generation]`` freezes that ledger when the epoch ends;
    * ``history`` records the mode of every finished generation, so a test can
      still ask what generation N released after N+1 is already running.
    """

    def __init__(self) -> None:
        self.generation = 0
        self.entry = ""
        self.registry: dict[str, object] = {}
        self.cleanup_counts: dict[str, int] = {}
        self.retired: dict[int, dict[str, int]] = {}
        self.history: list[GenerationSnapshot] = []
        self.errors: list[BaseException] = []
        self.listeners: dict[int, Listener] = {}
        self.tasks: list[TaskHandle] = []
        self.state = EpochState(generation=0, entry="")
        self._scope: EffectScope | None = None
        self._install: Callable[[Cleanup, str], Disposer] | None = None

    def begin(self, entry: str, generation: int) -> None:
        """Re-label this target for a new generation, archiving the old one."""

        if self.generation:
            self.history.append(self._snapshot())
            self.retired[self.generation] = dict(self.cleanup_counts)
        self.generation = generation
        self.entry = entry
        self.registry = {}
        self.cleanup_counts = {}
        self.errors = []
        self.listeners = {}
        self.tasks = []
        self._scope = None
        self._install = None
        self.state = EpochState(generation=generation, entry=entry)

    def bind(self, scope: EffectScope) -> Callable[[Cleanup, str], Disposer]:
        """Make ``scope`` the cleanup stack of the current epoch.

        Returns the installer every resource of the epoch must use, so there is
        exactly one ledger: ``cleanup_counts`` records that a label was
        released once, and the scope records what is still owned.  The ledger
        is captured *per scope*, so a config update and a replacement both stay
        attributable to the generation they belong to.
        """

        ledger = self.cleanup_counts

        def install(cleanup: Cleanup, label: str) -> Disposer:
            def release() -> None:
                try:
                    outcome = cleanup()
                    if inspect.isawaitable(outcome):
                        raise TypeError("async cleanups are out of scope in chapter 20")
                finally:
                    # A cleanup that raises is still *released exactly once*,
                    # so its failure shows up in both the ledger and the error.
                    ledger[label] = ledger.get(label, 0) + 1

            return scope.install(release, label)

        self._scope = scope
        self._install = install
        return install

    def mark_disposed(self) -> None:
        self._scope = None
        self._install = None
        self.state.mark_clean()

    def release_tasks(self) -> int:
        """Cancel every task scheduled by this epoch; return how many had run."""

        released = sum(1 for handle in self.tasks if handle.fired)
        for handle in self.tasks:
            handle.cancel()
        self.tasks.clear()
        return released

    def schedule(
        self,
        clock: ManualClock,
        delay_ms: int,
        callback: Callable[[], object],
    ) -> TaskHandle:
        """Schedule a task and make it part of this epoch's resources."""

        handle = clock.call_later(delay_ms, callback)
        self.tasks.append(handle)
        return handle

    def register_listener(self, listener_id: int, callback: Listener) -> None:
        self.listeners[listener_id] = callback

    def _snapshot(self) -> GenerationSnapshot:
        return GenerationSnapshot(
            generation=self.generation,
            entry=self.entry,
            cleanup_counts=dict(self.cleanup_counts),
            effects=self.state.effects,
            listeners=self.state.listeners,
            tasks=self.state.tasks,
        )


# --------------------------------------------------------------------------- #
# Hot reloader
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ParsedEntry:
    """The declarative half of one entry."""

    id: str
    module: str
    version: int | None = None
    config: object = None
    disabled: bool = False


@dataclass(slots=True)
class GenerationEntry:
    """The runtime half of one entry in one generation."""

    parsed: ParsedEntry
    target: HotReloadTarget
    callback: Callback
    fiber: Fiber | None
    context: Context | None
    scope: EffectScope
    disposers: list[Disposer] = field(default_factory=list)
    version: int = 0


class HotReloader:
    """Debounce host events, then run config updates or candidate replacements."""

    def __init__(
        self,
        source: MappingModuleSource,
        clock: ManualClock,
        *,
        debounce_ms: int = 100,
    ) -> None:
        if debounce_ms < 0:
            raise ValueError("debounce_ms must not be negative")
        self.source = source
        self.clock = clock
        self.debounce_ms = debounce_ms
        self.generations: dict[str, GenerationEntry] = {}
        self.errors: list[BaseException] = []
        self.batches: list[ChangeBatch] = []
        self.closed = False
        self._generation_counter = 0
        self._pending: list[ChangeEvent] = []
        self._timer: TaskHandle | None = None
        self._schedule = debounce_burst(clock, self._drain, delay_ms=debounce_ms)

    # -- lifecycle ---------------------------------------------------------- #

    def mount(self, entries: tuple[ParsedEntry, ...]) -> tuple[GenerationEntry, ...]:
        """Install the first generation of every entry."""

        if self.closed:
            raise HotReloadError("hot reloader is closed")
        if self.generations:
            raise HotReloadError("hot reloader already has a mounted generation")
        return self._install(entries)

    def generation(self, entry: str) -> int | None:
        current = self.generations.get(entry)
        return None if current is None else current.target.generation

    def target(self, entry: str) -> HotReloadTarget | None:
        current = self.generations.get(entry)
        return None if current is None else current.target

    def live(self, entry: str) -> Callback | None:
        """The callback that is executing right now, if any."""

        current = self.generations.get(entry)
        return None if current is None else current.callback

    def entry(self, entry: str) -> GenerationEntry:
        """The live generation record of one entry, after any replacement."""

        return self._current(entry)

    def context(self, entry: str) -> Context:
        """The context handed to the live callback of one entry."""

        current = self._current(entry)
        if current.context is None:
            raise HotReloadError(f"entry {entry!r} is disabled and has no context")
        return current.context

    def emit(self, entry: str, payload: object) -> list[object]:
        """Deliver one event to the live generation of one entry."""

        return self.context(entry).emit(payload)

    def schedule(
        self,
        entry: str,
        delay_ms: int,
        callback: Callable[[], object],
    ) -> TaskHandle:
        """Schedule a generation-owned task on the virtual clock."""

        if self.closed:
            raise HotReloadError("hot reloader is closed")
        return self._current(entry).target.schedule(self.clock, delay_ms, callback)

    def close(self) -> None:
        """Release every generation once; repeated calls are no-ops."""

        if self.closed:
            return
        self.closed = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._pending.clear()
        self.batches.clear()
        errors = self._dispose_all()
        self.errors.extend(errors)
        if errors:
            raise BaseExceptionGroup("hot reloader close failed", errors)

    # -- host input --------------------------------------------------------- #

    def on_change(self, event: ChangeEvent) -> None:
        """Accept one host-fed change and (re)arm the debounce window."""

        if self.closed:
            raise HotReloadError("hot reloader is closed")
        self._pending.append(event)
        self._timer = self._schedule()

    def on_changes(self, events: Iterable[ChangeEvent]) -> None:
        for event in events:
            self.on_change(event)

    @property
    def pending_events(self) -> int:
        return len(self._pending)

    def drain(self, *, advance: bool = True) -> int:
        """Apply everything the virtual clock has released, in release order.

        ``advance=True`` (the default) first moves the virtual clock past the
        current debounce deadline, so a test does not have to mirror the
        debounce window to observe the merged batch.  ``drain(advance=False)``
        only applies callbacks the clock has *already* fired, which is how the
        debounce tests inspect the window itself.
        """

        before = len(self.batches)
        if advance and self._pending:
            self.clock.advance(self.debounce_ms)
        return len(self.batches) - before

    def advance(self, delta_ms: int) -> int:
        """Move the virtual clock, apply whatever it releases, return the batch count."""

        before = len(self.batches)
        self.clock.advance(delta_ms)
        return len(self.batches) - before

    def _drain(self) -> None:
        self.flush()

    def flush(self) -> ChangeBatch | None:
        """Merge the pending events into one batch and hand it to the router."""

        if not self._pending:
            self._timer = None
            return None
        batch = ChangeBatch(tuple(self._pending))
        self._pending.clear()
        self._timer = None
        self.batches.append(batch)
        self.handle(batch)
        return batch

    # -- classification and routing ----------------------------------------- #

    def classify(self, batch: ChangeBatch) -> ChangeKind:
        return batch.kind

    def handle(self, batch: ChangeBatch) -> None:
        """Route one merged burst; failures are recorded instead of leaking."""

        try:
            if batch.structural:
                self.replace(batch)
            else:
                self.update_config(batch)
        except BaseException as error:  # noqa: BLE001 - reload failures must not kill the host
            self.errors.append(error)

    def update_config(self, batch: ChangeBatch) -> GenerationEntry:
        """Config-only change: build a new epoch on the *same* generation.

        The order is deliberate: validate, release the old epoch, then activate
        the new one.  A rejected payload therefore never touches the running
        epoch, and the observable trace is always ``stop(old)`` before
        ``start(new)`` -- there is never a window with two live epochs.
        """

        current = self._current(batch.entry)
        payload = batch.payload
        if payload is _MISSING:
            raise InvalidChangeError(
                f"config change for {batch.entry!r} has no payload"
            )
        validated = validate_config(getattr(current.callback, "Config", None), payload)
        fiber = current.fiber
        if fiber is None:
            raise ExecutionError(
                f"entry {current.parsed.id!r} is disabled and cannot be updated",
                phase="update",
            )
        previous_config = current.parsed.config
        previous_scope = current.scope
        cleanup_error = close_scope(previous_scope)
        try:
            scope, disposers = fiber.activate(
                validated, name=current.parsed.id, scope=EffectScope()
            )
        except BaseException as error:
            try:
                restored_scope, restored = fiber.activate(
                    previous_config, name=current.parsed.id, scope=EffectScope()
                )
            except BaseException as rollback_error:  # noqa: BLE001 - report both failures
                cause = BaseExceptionGroup(
                    "update and rollback failed", [error, rollback_error]
                )
                raise ExecutionError(
                    f"failed to update config of {current.parsed.id!r} and could not restore it",
                    phase="update",
                    cause=cause,
                ) from cause
            current.scope, current.disposers = restored_scope, restored
            cause = error if cleanup_error is None else cleanup_error
            raise ExecutionError(
                f"failed to update config of {current.parsed.id!r}; old epoch restored",
                phase="update",
                cause=cause,
            ) from error
        current.scope, current.disposers = scope, disposers
        current.parsed.config = validated
        current.version += 1
        if cleanup_error is not None:
            current.target.errors.append(cleanup_error)
            raise ExecutionError(
                f"config update of {current.parsed.id!r} left the previous epoch dirty",
                phase="cleanup",
                cause=cleanup_error,
            ) from cleanup_error
        return current

    def replace(self, batch: ChangeBatch) -> GenerationEntry:
        """Structural or module change: candidate replacement with rollback.

        The sequence is the whole point of the transaction:

        1. *resolve* the candidate while the live generation is untouched, so a
           misspelled or invalid module interrupts nothing;
        2. *release* the previous generation exactly once;
        3. *activate* the candidate; if activation fails, mount the previous
           declaration again so the entry keeps serving.
        """

        current = self._current(batch.entry)
        payload = batch.payload
        raw_config = current.parsed.config if payload is _MISSING else payload
        # Validation also runs before anything is released.
        validated = validate_config(
            getattr(current.callback, "Config", None), raw_config
        )
        candidate = ParsedEntry(
            id=current.parsed.id,
            module=batch.module or current.parsed.module,
            version=batch.version,
            config=validated,
            disabled=current.parsed.disabled,
        )
        try:
            callback = self.source.resolve(candidate.module, candidate.version)
        except BaseException as error:
            raise ExecutionError(
                f"failed to build candidate for {candidate.id!r}",
                phase="resolve",
                cause=error,
            ) from error

        previous = current.parsed
        target = current.target
        previous_generation = target.generation
        cleanup_error = self._dispose_generation(current)
        try:
            built = self._build(candidate, callback, target, current)
        except BaseException as error:  # noqa: BLE001 - transaction boundary: restore the previous generation
            rollback_errors: list[BaseException] = [error]
            if cleanup_error is not None:
                rollback_errors.append(cleanup_error)
            try:
                restored_callback = self.source.resolve(
                    previous.module, previous.version
                )
                restored = self._build(previous, restored_callback, target, current)
            except BaseException as rollback_error:  # noqa: BLE001 - report both failures
                rollback_errors.append(rollback_error)
                # The entry has nothing serving it any more, so it must not
                # keep claiming the generation that was just released.
                target.generation = previous_generation
            else:
                self.generations[candidate.id] = restored
                # The restore reuses the generation number the entry already had;
                # a failed attempt must not make the live entry look newer.
                target.generation = previous_generation
                if len(rollback_errors) == 1:
                    raise ExecutionError(
                        f"failed to replace {candidate.id!r}; restored the previous generation",
                        phase="activate",
                        cause=error,
                    ) from error
            cause = BaseExceptionGroup(
                "replacement and rollback failed", rollback_errors
            )
            raise ExecutionError(
                f"failed to replace {candidate.id!r}", phase="activate", cause=cause
            ) from cause
        self.generations[candidate.id] = built
        if cleanup_error is not None:
            raise ExecutionError(
                f"generation {target.generation} of {candidate.id!r} failed to clean up",
                phase="cleanup",
                cause=cleanup_error,
            ) from cleanup_error
        return built

    def _current(self, entry: str) -> GenerationEntry:
        current = self.generations.get(entry)
        if current is None:
            raise HotReloadError(f"entry {entry!r} is not mounted")
        return current

    # -- building generations ----------------------------------------------- #

    def _build(
        self,
        parsed: ParsedEntry,
        callback: Callback,
        target: HotReloadTarget | None = None,
        previous: GenerationEntry | None = None,
    ) -> GenerationEntry:
        """Build one generation of an already-resolved entry.

        ``target`` is the entry's permanent label for a replacement; a first
        mount creates one.  The generation counter is only consumed when the
        build succeeds, so a failed candidate never burns a generation number.
        """

        number = self._generation_counter + 1
        owner = HotReloadTarget() if target is None else target
        previous_generation = (
            owner.generation if previous is None else previous.target.generation
        )
        previous_entry = owner.entry if previous is None else previous.parsed.id
        owner.begin(parsed.id, number)
        context: Context | None = None
        fiber: Fiber | None = None
        scope = EffectScope()
        disposers: list[Disposer] = []
        try:
            if not parsed.disabled:
                context = Context(owner)
                fiber = Fiber(context, callback, parsed.config)
                fiber.target = owner
                scope, disposers = fiber.activate(parsed.config, name=parsed.id)
        except BaseException:
            if context is not None:
                context._detach()
            owner.release_tasks()
            close_scope(scope)
            owner.mark_disposed()
            # A failed candidate leaves no trace: no generation number, no
            # ledger entry, and the entry keeps the label it already had.
            owner.cleanup_counts.clear()
            owner.generation = previous_generation
            owner.entry = previous_entry
            raise
        if not parsed.disabled:
            owner.registry["module"] = parsed.module
            owner.registry["version"] = parsed.version
        self._generation_counter = number
        return GenerationEntry(
            parsed=parsed,
            target=owner,
            callback=callback,
            fiber=fiber,
            context=context,
            scope=scope,
            disposers=disposers,
        )

    def _install(
        self,
        entries: tuple[ParsedEntry, ...],
    ) -> tuple[GenerationEntry, ...]:
        installed: dict[str, GenerationEntry] = {}
        created: list[GenerationEntry] = []
        try:
            for parsed in entries:
                if parsed.id in self.generations or parsed.id in installed:
                    raise InvalidChangeError(f"duplicate entry id {parsed.id!r}")
                generation = self._build(
                    parsed, self.source.resolve(parsed.module, parsed.version)
                )
                installed[parsed.id] = generation
                created.append(generation)
        except BaseException as error:
            errors: list[BaseException] = [error]
            for generation in reversed(created):
                installed.pop(generation.parsed.id, None)
                cleanup_error = self._sweep(generation.target)
                if cleanup_error is not None:
                    errors.append(cleanup_error)
            if len(errors) == 1:
                raise
            raise BaseExceptionGroup("mount failed", errors) from error
        self.generations.update(installed)
        return tuple(created)

    def _dispose_generation(self, generation: GenerationEntry) -> BaseException | None:
        """Release one generation exactly once; returns the cleanup failure."""

        if generation.context is not None:
            generation.context._detach()
        generation.target.release_tasks()
        error = close_scope(generation.scope)
        generation.target.mark_disposed()
        if error is not None:
            generation.target.errors.append(error)
        return error

    def _sweep(self, target: HotReloadTarget) -> BaseException | None:
        """Release a target that was never installed anywhere."""

        target.release_tasks()
        scope = target._scope
        target.mark_disposed()
        if scope is None:
            return None
        return close_scope(scope)

    def _dispose_all(self) -> list[BaseException]:
        errors: list[BaseException] = []
        installed, self.generations = self.generations, {}
        for entry in list(installed.values()):
            error = self._dispose_generation(entry)
            if error is not None:
                errors.append(error)
        return errors


__all__ = [
    "Callback",
    "ChangeBatch",
    "ChangeEvent",
    "ChangeKind",
    "Cleanup",
    "Context",
    "Disposer",
    "EffectScope",
    "EpochState",
    "ExecutionError",
    "Fiber",
    "GenerationEntry",
    "HotReloadError",
    "HotReloadTarget",
    "HotReloader",
    "InvalidChangeError",
    "Listener",
    "ManualClock",
    "MappingModuleSource",
    "ParsedEntry",
    "Scope",
    "StaleEpochError",
    "TaskHandle",
    "close_scope",
    "debounce_burst",
    "validate_config",
]
