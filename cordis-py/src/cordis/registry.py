"""Plugin normalization and runtime registry."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, ItemsView, KeysView, ValuesView
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .errors import CordisError, CordisErrorCode
from .model import InjectSpec, Plugin, PluginSpec, normalize_inject

if TYPE_CHECKING:
    from .context import Context
    from .fiber import Fiber


def _new_fiber_set() -> set[Fiber]:
    return set()


@dataclass(slots=True)
class PluginRuntime:
    """Shared registry record for one normalized plugin callback."""

    spec: PluginSpec
    fibers: set[Fiber] = field(default_factory=_new_fiber_set)

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def callback(self) -> object:
        return self.spec.callback

    @property
    def Config(self) -> object | None:
        return self.spec.validator


class RegistryService:
    """Create Fibers and index mounted plugin definitions."""

    def __init__(self, root: Context) -> None:
        self.ctx = self.root = root
        self._counter = 0
        self._runtimes: dict[object, PluginRuntime] = {}
        self._dependents: dict[str, set[Fiber]] = {}
        self._disposals: set[asyncio.Task[None]] = set()

    def for_context(self, context: Context) -> RegistryServiceView:
        return RegistryServiceView(self, context)

    @property
    def size(self) -> int:
        """Number of distinct mounted plugin definitions."""

        return len(self._runtimes)

    @property
    def counter(self) -> int:
        """Allocate the next Fiber uid."""

        self._counter += 1
        return self._counter

    def resolve(self, plugin: Plugin) -> object | None:
        """Resolve a supported plugin shape to its identifying callback."""

        if inspect.isclass(plugin) or inspect.isfunction(plugin):
            return plugin
        apply = getattr(plugin, "apply", None)
        return apply if callable(apply) else None

    def _resolve_spec(self, plugin: Plugin, inject: object = None) -> PluginSpec:
        callback = self.resolve(plugin)
        if callback is None:
            raise CordisError(CordisErrorCode.INVALID_PLUGIN)
        raw_name = getattr(plugin, "name", None) or getattr(plugin, "__name__", None)
        name = raw_name if isinstance(raw_name, str) else plugin.__class__.__name__
        declared_inject = getattr(plugin, "inject", None) if inject is None else inject
        dependencies = normalize_inject(declared_inject)
        validator = getattr(plugin, "Config", None)
        return PluginSpec(callback, name, dependencies, validator)

    def get(self, plugin: Plugin) -> PluginRuntime | None:
        """Return the shared runtime for a mounted plugin."""

        callback = self.resolve(plugin)
        return None if callback is None else self._runtimes.get(callback)

    def has(self, plugin: Plugin) -> bool:
        """Return whether a plugin has a registered runtime."""

        return self.get(plugin) is not None

    def delete(self, plugin: Plugin) -> PluginRuntime | None:
        """Remove a plugin runtime and request disposal of all its Fibers."""

        callback = self.resolve(plugin)
        runtime = None if callback is None else self._runtimes.pop(callback, None)
        if runtime is None:
            return None
        for fiber in tuple(runtime.fibers):
            task = asyncio.create_task(fiber.dispose())
            self._disposals.add(task)
            task.add_done_callback(self._disposals.discard)
        return runtime

    def keys(self) -> KeysView[object]:
        return self._runtimes.keys()

    def values(self) -> ValuesView[PluginRuntime]:
        return self._runtimes.values()

    def entries(self) -> ItemsView[object, PluginRuntime]:
        return self._runtimes.items()

    def forEach(
        self,
        callback: Callable[[PluginRuntime, object], object],
    ) -> None:
        """Visit every runtime and identifying callback."""

        for key, runtime in self._runtimes.items():
            callback(runtime, key)

    def inject(self, context: Context, dependencies: InjectSpec, callback: Plugin) -> Fiber:
        """Mount a callback with explicit service dependencies."""

        return self._mount(context, self._resolve_spec(callback, dependencies), None)

    def plugin(self, context: Context, plugin: Plugin, config: object = None) -> Fiber:
        """Mount a plugin under a Context and return its Fiber immediately."""

        context.fiber.assertActive()
        spec = self._resolve_spec(plugin)
        return self._mount(context, spec, config)

    def _mount(self, context: Context, spec: PluginSpec, config: object) -> Fiber:
        """Mount one normalized plugin spec through the shared lifecycle path."""

        from .fiber import Fiber

        runtime = self._runtimes.get(spec.callback)
        if runtime is None:
            runtime = PluginRuntime(spec)
            self._runtimes[spec.callback] = runtime
        fiber = Fiber(self.counter, context, runtime, config)
        runtime.fibers.add(fiber)
        for name in spec.inject:
            self._dependents.setdefault(name, set()).add(fiber)
        fiber.bootstrap()
        context.root.events.emit_safe("internal/plugin", fiber)
        return fiber

    def dependents(self, name: str) -> tuple[Fiber, ...]:
        """Return a stable snapshot of Fibers depending on a service name."""

        return tuple(self._dependents.get(name, ()))

    def runtimes(self) -> tuple[PluginRuntime, ...]:
        """Return a side-effect-free runtime snapshot."""

        return tuple(self._runtimes.values())

    def remove(self, fiber: Fiber) -> None:
        """Remove a disposed Fiber from every registry index."""

        runtime = fiber.runtime
        runtime.fibers.discard(fiber)
        for name in runtime.spec.inject:
            fibers = self._dependents.get(name)
            if fibers is None:
                continue
            fibers.discard(fiber)
            if not fibers:
                del self._dependents[name]
        if not runtime.fibers:
            self._runtimes.pop(runtime.spec.callback, None)


class RegistryServiceView:
    def __init__(self, service: RegistryService, context: Context) -> None:
        self.service, self.ctx = service, context

    def inject(self, dependencies: InjectSpec, callback: Plugin) -> Fiber:
        return self.service.inject(self.ctx, dependencies, callback)

    def plugin(self, plugin: Plugin, config: object = None) -> Fiber:
        return self.service.plugin(self.ctx, plugin, config)

    @property
    def size(self) -> int:
        return self.service.size

    @property
    def counter(self) -> int:
        return self.service.counter

    def resolve(self, plugin: Plugin) -> object | None:
        return self.service.resolve(plugin)

    def get(self, plugin: Plugin) -> PluginRuntime | None:
        return self.service.get(plugin)

    def has(self, plugin: Plugin) -> bool:
        return self.service.has(plugin)

    def delete(self, plugin: Plugin) -> PluginRuntime | None:
        return self.service.delete(plugin)

    def keys(self) -> KeysView[object]:
        return self.service.keys()

    def values(self) -> ValuesView[PluginRuntime]:
        return self.service.values()

    def entries(self) -> ItemsView[object, PluginRuntime]:
        return self.service.entries()

    def forEach(self, callback: Callable[[PluginRuntime, object], object]) -> None:
        self.service.forEach(callback)

    def dependents(self, name: str) -> tuple[Fiber, ...]:
        return self.service.dependents(name)

    def runtimes(self) -> tuple[PluginRuntime, ...]:
        return self.service.runtimes()

    def remove(self, fiber: Fiber) -> None:
        self.service.remove(fiber)

    def __getattr__(self, name: str) -> object:
        return getattr(self.service, name)
