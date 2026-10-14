"""Scoped dependency container presented to plugins."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING

from .effect import Effect, EffectSetup
from .events import EventOptions, Listener, ListenerDisposer
from .model import InjectSpec, Plugin

if TYPE_CHECKING:
    from .events import EventsServiceView
    from .fiber import Fiber, RootFiber
    from .logger import Exporter, LoggerService, LoggerServiceView
    from .reflect import AccessorProperty, ReflectServiceView
    from .registry import RegistryServiceView


class Context:
    """Root or derived view over a Cordis runtime."""

    filter = object()

    def __init__(self) -> None:
        from .events import EventsService
        from .fiber import RootFiber
        from .logger import LoggerService
        from .reflect import ReflectService
        from .registry import RegistryService

        self.root = self
        self.baseUrl: str | None = None
        self._metadata: dict[object, object] = {}
        self.isolation: dict[str, object] = {}
        self.intercepts: dict[str, list[object]] = {}
        self.fiber: Fiber | RootFiber = RootFiber(self)
        registry = RegistryService(self)
        reflect = ReflectService(self)
        events = EventsService(self)
        self.registry: RegistryServiceView = registry.for_context(self)
        self.reflect: ReflectServiceView = reflect.for_context(self)
        self.events: EventsServiceView = events.for_context(self)
        self.logger: LoggerService | LoggerServiceView = LoggerService(self)

    @classmethod
    def derive(cls, parent: Context, fiber: Fiber | None = None) -> Context:
        context = object.__new__(cls)
        context.__dict__ = parent.__dict__.copy()
        context.root = parent.root
        context._metadata = dict(parent._metadata)
        context.isolation = dict(parent.isolation)
        context.intercepts = {name: list(values) for name, values in parent.intercepts.items()}
        context.fiber = fiber or parent.fiber
        context.registry = parent.root.registry.service.for_context(context)
        context.reflect = parent.root.reflect.service.for_context(context)
        context.events = parent.root.events.service.for_context(context)
        context.logger = parent.root.logger.for_context(context)
        return context

    def extend(self, meta: Mapping[object, object] | None = None) -> Context:
        """Create a child Context that shares the current scope."""

        context = self.derive(self)
        if meta is not None:
            for name, value in meta.items():
                if isinstance(name, str):
                    setattr(context, name, value)
                else:
                    context._metadata[name] = value
        return context

    @staticmethod
    def is_context(value: object) -> bool:
        """Return whether *value* is a Cordis Context."""

        return isinstance(value, Context)

    def metadata(self, key: object) -> object | None:
        """Read non-string metadata inherited through :meth:`extend`."""

        return self._metadata.get(key)

    def isolate(self, name: str, label: object | None = None) -> Context:
        """Create a child Context with an independent service scope."""

        context = self.derive(self)
        context.isolation[name] = label if label is not None else object()
        return context

    def intercept(self, name: str, config: object) -> Context:
        """Create a child Context with additional service-specific configuration."""

        context = self.derive(self)
        context.intercepts.setdefault(name, []).append(config)
        return context

    def plugin(self, plugin: Plugin, config: object = None) -> Fiber:
        """Mount a plugin under this Context."""

        return self.registry.plugin(plugin, config)

    def inject(self, dependencies: InjectSpec, callback: Plugin) -> Fiber:
        """Mount a callback while all declared services are available."""

        return self.registry.inject(dependencies, callback)

    def get(self, name: str, strict: bool = True) -> object | None:
        """Resolve a service explicitly."""

        from .service import bind_service

        def resolve() -> object:
            accessor = self.reflect.accessor_record(name)
            if accessor is not None:
                return accessor.getter(self)
            implementation = (self.fiber.store or {}).get(name)
            if strict and implementation is not None:
                return bind_service(implementation.value, self)
            return bind_service(self.reflect.get(name, strict), self)

        error = RuntimeError(f'cannot get property "{name}" without inject')
        return self.events.waterfall_sync("internal/get", self, name, error, next_=resolve)

    def __getattr__(self, name: str) -> object | None:
        return self.get(name)

    def set(self, name: str, value: object) -> bool:
        """Set a declared accessor or a service owned by this Fiber."""

        return bool(
            self.events.waterfall_sync(
                "internal/set",
                self,
                name,
                value,
                RuntimeError(f'cannot set property "{name}" without provide'),
                next_=lambda: self.reflect.set(name, value),
            )
        )

    def provide(
        self,
        name: str,
        value: object,
        check: Callable[[], bool] | None = None,
    ) -> Effect:
        """Provide a service owned by the current Fiber."""

        return self.reflect.provide(name, value, check)

    def accessor(
        self,
        name: str,
        options: AccessorProperty,
    ) -> Effect:
        """Declare a computed Context property."""

        return self.reflect.accessor(name, options)

    def mixin(
        self,
        source: str | object,
        members: Sequence[str] | Mapping[str, str],
    ) -> Effect:
        """Expose selected members of a service or object on Context."""

        return self.reflect.mixin(source, members)

    def bind(self, callback: Callable[..., object]) -> Callable[..., object]:
        """Bind callback diagnostics to this Context."""

        return self.reflect.bind(callback)

    def exporter(self, exporter: Exporter) -> Effect:
        """Register a lifecycle-owned log exporter."""

        return self.logger.exporter(exporter)

    def effect(self, setup: EffectSetup, label: str = "anonymous") -> Effect:
        """Install a callable, awaitable Effect disposer on the current Fiber."""

        return self.fiber.effects.install_auto(setup, label)

    def on(
        self,
        name: object,
        listener: Listener,
        options: bool | EventOptions | None = None,
    ) -> ListenerDisposer:
        """Register a lifecycle-owned event listener."""

        return self.events.on(name, listener, options)

    def once(
        self,
        name: object,
        listener: Listener,
        options: bool | EventOptions | None = None,
    ) -> ListenerDisposer:
        """Register a lifecycle-owned one-shot listener."""

        return self.events.once(name, listener, options)

    def emit(self, name: object, *args: object) -> None:
        """Synchronously notify all event listeners."""

        self.events.emit(name, *args)

    def bail(self, name: object, *args: object) -> object:
        """Return the first synchronous bail value."""

        return self.events.bail(name, *args)

    async def serial(self, name: object, *args: object) -> object:
        """Await listeners in order until one returns a bail value."""

        return await self.events.serial(name, *args)

    async def parallel(self, name: object, *args: object) -> None:
        """Run listeners concurrently and wait for completion."""

        await self.events.parallel(name, *args)

    async def waterfall(
        self,
        name: object,
        *args: object,
        next_: Listener,
    ) -> object:
        """Run async around-middleware around a final callback."""

        return await self.events.waterfall(name, *args, next_=next_)

    async def aclose(self) -> None:
        """Dispose the root Fiber and every owned child."""

        await self.root.fiber.dispose()
