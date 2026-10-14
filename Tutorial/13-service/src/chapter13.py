"""Service registration, caller tracking and accessors for tutorial chapter 13.

Self-contained teaching module: it imports only the standard library and never
imports ``cordis``.  Chapters 05-11 already established Reflect, isolation and
dependency epochs; this chapter keeps the smallest version of those mechanisms
that the Service abstraction actually needs, then separates the three
responsibilities that are easy to confuse:

* :class:`ReflectService` stores implementations and notifies dependents;
* :class:`Service` is a registrable object with lifecycle and config hooks;
* :class:`ServiceView` binds one resolved value to the caller's Context.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, NotRequired, TypeAlias, TypedDict, cast

Cleanup = Callable[[], object]
InjectSpec: TypeAlias = Sequence[str] | Mapping[str, object]
_current_context: contextvars.ContextVar[Context | None] = contextvars.ContextVar(
    "tutorial_service_context",
    default=None,
)


class CordisErrorCode(StrEnum):
    """The three error codes this chapter needs; the real runtime has more."""

    DUPLICATE_SERVICE = "DUPLICATE_SERVICE"
    MISSING_SERVICE = "MISSING_SERVICE"
    INACTIVE_EFFECT = "INACTIVE_EFFECT"


class CordisError(RuntimeError):
    def __init__(self, code: CordisErrorCode, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code.value)


class Effect:
    """A single owned cleanup produced by a synchronous setup."""

    def __init__(self, label: str = "anonymous") -> None:
        self.label = label
        self._cleanup: Cleanup | None = None
        self._dispose_task: asyncio.Task[None] | None = None

    def __call__(self) -> asyncio.Task[None]:
        if self._dispose_task is None:
            self._dispose_task = asyncio.create_task(self._dispose())
        return self._dispose_task

    async def dispose(self) -> None:
        await asyncio.shield(self())

    async def _dispose(self) -> None:
        cleanup, self._cleanup = self._cleanup, None
        if cleanup is None:
            return
        result = cleanup()
        if inspect.isawaitable(result):
            await cast("asyncio.Future[object]", result)


class EffectScope:
    """Owns every Effect of one Fiber and closes them in reverse order."""

    def __init__(self) -> None:
        self._effects: list[Effect] = []
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(effect.label for effect in self._effects)

    @property
    def closed(self) -> bool:
        return self._closed

    def install_sync(
        self, setup: Callable[[], object], label: str = "anonymous"
    ) -> Effect:
        if self._closed:
            raise CordisError(
                CordisErrorCode.INACTIVE_EFFECT, "closed scope cannot own an Effect"
            )
        result = setup()
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise TypeError("install_sync setup must not return an awaitable")
        if result is not None and not callable(result):
            raise TypeError("invalid Effect cleanup")
        effect = Effect(label)
        effect._cleanup = cast(Cleanup | None, result)
        self._effects.append(effect)
        return effect

    async def close(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        errors: list[BaseException] = []
        while self._effects:
            effect = self._effects.pop()
            try:
                await effect.dispose()
            except asyncio.CancelledError:
                raise
            except BaseException as error:  # noqa: BLE001 - cleanup aggregation: keep cleaning after any failure
                errors.append(error)
        if errors:
            raise BaseExceptionGroup("EffectScope cleanup failed", errors)


class ServiceProperty(TypedDict):
    type: Literal["service"]


class AccessorProperty(TypedDict):
    type: NotRequired[Literal["accessor"]]
    get: Callable[[Context], object]
    set: NotRequired[Callable[[Context, object], object]]


class Property:
    """Python namespace for Cordis property declaration variants."""

    Service = ServiceProperty
    Accessor = AccessorProperty


@dataclass(slots=True)
class Impl:
    """A service value and the Fiber that owns it."""

    name: str
    label: object
    value: object
    fiber: Fiber
    check: Callable[[], bool] | None = None


@dataclass(frozen=True, slots=True)
class Accessor:
    """Computed Context property owned by a Fiber."""

    name: str
    getter: Callable[[Context], object]
    setter: Callable[[Context, object], object] | None
    fiber: Fiber


class FiberState(StrEnum):
    PENDING = "PENDING"
    LOADING = "LOADING"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"
    UNLOADING = "UNLOADING"
    DISPOSED = "DISPOSED"


class RootFiber:
    """Permanent owner of top-level Fibers until Context shutdown."""

    uid = 0
    name = "root"

    def __init__(self, context: Context) -> None:
        self.ctx = self.context = context
        self.state = FiberState.ACTIVE
        self.effects = EffectScope()
        self.store: dict[str, Impl] = {}
        self.provided_names: set[str] = set()

    def assert_active(self) -> None:
        if self.state is FiberState.DISPOSED:
            raise CordisError(CordisErrorCode.INACTIVE_EFFECT)

    def get_effects(self) -> tuple[str, ...]:
        return self.effects.labels

    async def dispose(self) -> None:
        if self.state is FiberState.DISPOSED:
            return
        self.state = FiberState.UNLOADING
        try:
            await self.effects.close()
        finally:
            self.state = FiberState.DISPOSED


class Fiber:
    """One plugin mount whose activation depends on resolvable services."""

    def __init__(
        self,
        parent: Context,
        name: str,
        setup: object,
        config: object = None,
        dependencies: Sequence[str] = (),
        intercepts: Mapping[str, object] | None = None,
    ) -> None:
        self.parent = parent
        self.root = parent.root
        self.uid = parent.root._next_uid()
        self.name = name
        self.setup = setup
        self.config = config
        self.dependencies = tuple(dict.fromkeys(dependencies))
        self.ctx = self.context = Context.derive(parent, self)
        for dependency, intercept in (intercepts or {}).items():
            if intercept is not None:
                self.context.intercepts.setdefault(dependency, []).append(intercept)
        self.state = FiberState.PENDING
        self.store: dict[str, Impl] | None = None
        self.provided_names: set[str] = set()
        self.error: BaseException | None = None
        self.effects = EffectScope()
        self._epoch: tuple[int, ...] | None = None
        self._failed_epoch: tuple[int, ...] | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._refresh_requested = False
        self._lock = asyncio.Lock()
        self._dispose_requested = False
        for dependency in self.dependencies:
            self.root.reflect.service.register_dependent(dependency, self)
        self.parent.fiber.effects.install_sync(
            lambda: self.dispose, f"ctx.plugin({name!r})"
        )

    def __await__(self):  # type: ignore[no-untyped-def]
        return self.wait().__await__()

    def start(self) -> Fiber:
        self.request_refresh()
        return self

    def assert_active(self) -> None:
        if self._dispose_requested or self.state is FiberState.DISPOSED:
            raise CordisError(CordisErrorCode.INACTIVE_EFFECT)

    def get_effects(self) -> tuple[str, ...]:
        return self.effects.labels

    def request_refresh(self) -> None:
        """Coalesce dependency notifications into one serialized refresh loop."""

        if self._dispose_requested:
            return
        self._refresh_requested = True
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def wait(self) -> Fiber:
        while self._refresh_task is not None:
            task = self._refresh_task
            await asyncio.shield(task)
            if task is self._refresh_task and not self._refresh_requested:
                break
        if self.error is not None:
            raise self.error
        return self

    async def _refresh_loop(self) -> None:
        while self._refresh_requested and not self._dispose_requested:
            self._refresh_requested = False
            async with self._lock:
                await self._refresh_once()

    def _resolve(self) -> tuple[dict[str, Impl], tuple[int, ...]] | None:
        resolved: dict[str, Impl] = {}
        epoch: list[int] = []
        for name in self.dependencies:
            implementation = self.root.reflect.implementation(self.context, name)
            if implementation is None or implementation.fiber.uid is None:
                return None
            resolved[name] = implementation
            epoch.append(implementation.fiber.uid)
        return resolved, tuple(epoch)

    def _resolve_epoch(self) -> tuple[int, ...] | None:
        resolution = self._resolve()
        return None if resolution is None else resolution[1]

    async def _refresh_once(self) -> None:
        resolution = self._resolve()
        if resolution is None:
            if self.state in {FiberState.ACTIVE, FiberState.FAILED}:
                await self._unload()
            return

        dependencies, epoch = resolution
        if self.state is FiberState.ACTIVE and epoch == self._epoch:
            return
        if self.state is FiberState.FAILED and epoch == self._failed_epoch:
            return
        if self.state in {FiberState.ACTIVE, FiberState.FAILED}:
            await self._unload(settle_pending=False)
        if not self._dispose_requested:
            await self._activate(dependencies, epoch)

    def _invoke(self) -> object:
        target = self.setup
        if inspect.isclass(target):
            constructor = cast(Callable[[object, object], object], target)
            instance = constructor(self.context, self.config)
            initialize = getattr(instance, "init", None)
            return initialize() if callable(initialize) else None
        apply = getattr(target, "apply", None)
        if not inspect.isfunction(target) and callable(apply):
            return apply(self.context, self.config)
        if not callable(target):
            raise TypeError("invalid plugin")
        return cast(Callable[[object, object], object], target)(
            self.context, self.config
        )

    async def _activate(
        self,
        dependencies: dict[str, Impl],
        epoch: tuple[int, ...],
    ) -> None:
        self.state = FiberState.LOADING
        self.store = dependencies
        self._epoch = epoch
        self.error = None
        self.effects = EffectScope()
        try:
            result = self._invoke()
            if inspect.isawaitable(result):
                result = await cast("asyncio.Future[object]", result)
            if result is not None:
                if not callable(result):
                    raise TypeError("plugin result must be a cleanup callable or None")
                self.effects.install_sync(lambda: result, f"plugin({self.name!r})")
            if self._dispose_requested or self._resolve_epoch() != epoch:
                await self._unload()
                return
            self.state = FiberState.ACTIVE
            for name in tuple(self.provided_names):
                self.root.reflect.service.notify_soon(name)
        except BaseException as error:  # noqa: BLE001 - cancellation boundary: roll back partial activation
            self.error = error
            self._failed_epoch = epoch
            try:
                await self.effects.close()
            except BaseException as cleanup_error:  # noqa: BLE001 - aggregate activation and rollback errors
                self.error = BaseExceptionGroup(
                    f"plugin {self.name!r} activation and rollback failed",
                    [error, cleanup_error],
                )
            self.store = None
            self._epoch = None
            self.state = FiberState.FAILED

    async def _unload(self, *, settle_pending: bool = True) -> None:
        self.state = FiberState.UNLOADING
        try:
            await self.effects.close()
        finally:
            self.store = None
            self.provided_names.clear()
            self._epoch = None
            self.error = None
            if self._dispose_requested:
                self.state = FiberState.DISPOSED
            elif settle_pending:
                self.state = FiberState.PENDING

    async def dispose(self) -> None:
        """Permanently unload this Fiber and stop receiving notifications."""

        if self.state is FiberState.DISPOSED:
            return
        self._dispose_requested = True
        async with self._lock:
            try:
                if self.state is not FiberState.DISPOSED:
                    await self._unload()
            finally:
                self.root.reflect.service.unregister(self)


class ReflectService:
    """Store service implementations by isolation label and wake dependents."""

    def __init__(self, root: Context) -> None:
        self.ctx = self.root = root
        self._labels: dict[str, object] = {}
        self._implementations: dict[object, Impl] = {}
        self._accessors: dict[str, Accessor] = {}
        self._service_names: set[str] = set()
        self._dependents: dict[str, list[Fiber]] = {}

    def for_context(self, context: Context) -> ReflectServiceView:
        return ReflectServiceView(self, context)

    def label(self, context: Context, name: str) -> object:
        label = context.isolation.get(name)
        if label is not None:
            return label
        return self._labels.setdefault(name, object())

    def implementation(
        self, context: Context, name: str, *, strict: bool = True
    ) -> Impl | None:
        """Resolve strictly: provider must be ACTIVE and its check must pass."""

        implementation = self._implementations.get(self.label(context, name))
        if implementation is None:
            return None
        if strict and implementation.fiber.state is not FiberState.ACTIVE:
            return None
        if strict and implementation.check is not None and not implementation.check():
            return None
        return implementation

    def get(self, context: Context, name: str, *, strict: bool = True) -> object | None:
        implementation = self.implementation(context, name, strict=strict)
        return None if implementation is None else implementation.value

    def set(self, context: Context, name: str, value: object) -> bool:
        accessor = self._accessors.get(name)
        if accessor is not None:
            if accessor.setter is None:
                raise AttributeError(f"Context accessor {name!r} is read-only")
            return accessor.setter(context, value) is not False
        implementation = self.implementation(context, name, strict=False)
        if implementation is None:
            raise CordisError(
                CordisErrorCode.MISSING_SERVICE, f"service {name!r} is unavailable"
            )
        if implementation.fiber is not context.fiber:
            raise PermissionError(f"service {name!r} is owned by another Fiber")
        implementation.value = value
        return True

    def provide(
        self,
        context: Context,
        name: str,
        value: object,
        check: Callable[[], bool] | None = None,
    ) -> Effect:
        """Register a service as a synchronous Effect owned by the current Fiber."""

        context.fiber.assert_active()
        label = self.label(context, name)
        if name in self._accessors:
            raise TypeError(f"Context property {name!r} is already an accessor")
        implementation = Impl(name, label, value, context.fiber, check)

        def setup() -> object:
            if label in self._implementations:
                raise CordisError(
                    CordisErrorCode.DUPLICATE_SERVICE,
                    f"service {name!r} already has a provider in this scope",
                )
            self._implementations[label] = implementation
            self._service_names.add(name)
            context.fiber.provided_names.add(name)

            async def cleanup() -> None:
                if self._implementations.get(label) is implementation:
                    del self._implementations[label]
                context.fiber.provided_names.discard(name)
                await self.notify_label(name, label)

            return cleanup

        effect = context.fiber.effects.install_sync(setup, f"ctx.provide({name!r})")
        if context.fiber.state is FiberState.ACTIVE:
            self.notify_soon(name)
        return effect

    def accessor_record(self, name: str) -> Accessor | None:
        return self._accessors.get(name)

    def accessor(
        self, context: Context, name: str, options: AccessorProperty
    ) -> Effect:
        getter = options["get"]
        setter = cast("Callable[[Context, object], object] | None", options.get("set"))
        record = Accessor(name, getter, setter, context.fiber)

        def setup() -> object:
            if name in self._accessors or name in self._service_names:
                raise TypeError(f"Context property {name!r} is already declared")
            self._accessors[name] = record

            def cleanup() -> None:
                if self._accessors.get(name) is record:
                    del self._accessors[name]

            return cleanup

        return context.fiber.effects.install_sync(setup, f"ctx.accessor({name!r})")

    def mixin(
        self,
        context: Context,
        source: str | object,
        members: Sequence[str] | Mapping[str, str],
    ) -> Effect:
        """Register accessors that forward selected members of a service."""

        entries = (
            list(members.items())
            if isinstance(members, Mapping)
            else [(n, n) for n in members]
        )
        records: list[Accessor] = []
        for source_name, target_name in entries:

            def getter(receiver: Context, key: str = source_name) -> object:
                target = receiver.get(source) if isinstance(source, str) else source
                return getattr(target, key)

            def setter(
                receiver: Context, value: object, key: str = source_name
            ) -> None:
                target = receiver.get(source) if isinstance(source, str) else source
                setattr(target, key, value)

            records.append(Accessor(target_name, getter, setter, context.fiber))

        def setup() -> object:
            names = [record.name for record in records]
            if len(set(names)) != len(names) or any(
                name in self._accessors or name in self._labels for name in names
            ):
                raise TypeError("mixin target property is already declared")
            self._accessors.update((record.name, record) for record in records)

            def cleanup() -> None:
                for record in records:
                    if self._accessors.get(record.name) is record:
                        del self._accessors[record.name]

            return cleanup

        return context.fiber.effects.install_sync(setup, "ctx.mixin()")

    def implementations(self) -> tuple[Impl, ...]:
        """Return a side-effect-free snapshot for diagnostics."""

        return tuple(self._implementations.values())

    def register_dependent(self, name: str, fiber: Fiber) -> None:
        self._dependents.setdefault(name, []).append(fiber)

    def unregister(self, fiber: Fiber) -> None:
        for fibers in self._dependents.values():
            if fiber in fibers:
                fibers.remove(fiber)

    def dependents(self, name: str) -> tuple[Fiber, ...]:
        return tuple(self._dependents.get(name, ()))

    def refresh_scope(self, name: str, label: object) -> tuple[Fiber, ...]:
        """Request refresh for dependents that resolve this service in ``label``."""

        fibers = tuple(
            fiber
            for fiber in self.dependents(name)
            if self.label(fiber.context, name) is label
        )
        for fiber in fibers:
            fiber.request_refresh()
        return fibers

    def notify_soon(self, name: str) -> None:
        """Schedule a dependency refresh without blocking synchronous registration."""

        label = self.label(self.root, name)
        asyncio.get_running_loop().create_task(self.notify_label(name, label))

    async def notify_label(self, name: str, label: object) -> tuple[Fiber, ...]:
        fibers = self.refresh_scope(name, label)
        if fibers:
            await asyncio.gather(
                *(fiber.wait() for fiber in fibers), return_exceptions=True
            )
        return fibers


class ReflectServiceView:
    """Context-bound facade over the single shared Reflect store."""

    def __init__(self, service: ReflectService, context: Context) -> None:
        self.service, self.ctx = service, context

    def get(self, name: str, strict: bool = True) -> object | None:
        return self.service.get(self.ctx, name, strict=strict)

    def set(self, name: str, value: object) -> bool:
        return self.service.set(self.ctx, name, value)

    def provide(
        self,
        name: str,
        value: object,
        check: Callable[[], bool] | None = None,
    ) -> Effect:
        return self.service.provide(self.ctx, name, value, check)

    def accessor(self, name: str, options: AccessorProperty) -> Effect:
        return self.service.accessor(self.ctx, name, options)

    def mixin(
        self, source: str | object, members: Sequence[str] | Mapping[str, str]
    ) -> Effect:
        return self.service.mixin(self.ctx, source, members)

    def label(self, context: Context, name: str) -> object:
        return self.service.label(context, name)

    def implementation(
        self, context: Context, name: str, *, strict: bool = True
    ) -> Impl | None:
        return self.service.implementation(context, name, strict=strict)

    def accessor_record(self, name: str) -> Accessor | None:
        return self.service.accessor_record(name)

    def implementations(self) -> tuple[Impl, ...]:
        return self.service.implementations()

    def dependents(self, name: str) -> tuple[Fiber, ...]:
        return self.service.dependents(name)

    async def notify(self, name: str) -> tuple[Fiber, ...]:
        return await self.service.notify_label(name, self.service.label(self.ctx, name))

    def __getattr__(self, name: str) -> object:
        return getattr(self.service, name)


class ServiceView:
    """Forward a Service while binding method calls to a consumer Context."""

    __slots__ = ("_context", "_target")

    def __init__(self, target: Service, context: Context) -> None:
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_context", context)

    def __getattr__(self, name: str) -> object:
        value = getattr(self._target, name)
        if not callable(value):
            return value
        callback = value

        def bound(*args: object, **kwargs: object) -> object:
            token = _current_context.set(self._context)
            try:
                return callback(*args, **kwargs)
            finally:
                _current_context.reset(token)

        return bound

    def __setattr__(self, name: str, value: object) -> None:
        setattr(self._target, name, value)

    def __call__(self, *args: object, **kwargs: object) -> object:
        token = _current_context.set(self._context)
        try:
            callback = cast(Callable[..., object], self._target)
            return callback(*args, **kwargs)
        finally:
            _current_context.reset(token)


class Service:
    """Register an instance under a stable Context service name."""

    provide: str | None = None
    Config: object | None = None

    def __init__(self, context: Context, name: str | None = None) -> None:
        self.context = context
        self.name = name or self.provide or type(self).__name__.lower()
        context.provide(self.name, self, self.available)

    def available(self) -> bool:
        """Return whether strict consumers may use this service."""

        return True

    @property
    def caller_context(self) -> Context:
        """Context through which the current service method was invoked."""

        return _current_context.get() or self.context

    def extend(self, **properties: object) -> Service:
        """Create an unregistered derived service sharing this instance's state."""

        service = copy.copy(self)
        vars(service).update(properties)
        return service

    def resolve_config(
        self,
        base: Mapping[str, object] | None = None,
        head: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Merge service intercept configuration from outer to inner Contexts."""

        context = _current_context.get() or self.context
        configs: list[Mapping[str, object]] = []
        for config in (base, *context.intercepts.get(self.name, ()), head):
            if config is None:
                continue
            if not isinstance(config, Mapping):
                raise TypeError(f"intercept config for {self.name!r} must be a mapping")
            configs.append(cast("Mapping[str, object]", config))
        merge = getattr(self.Config, "merge", None)
        if callable(merge):
            return cast("dict[str, object]", merge(*configs))
        result: dict[str, object] = {}
        for config in configs:
            result.update(config)
        return result


def bind_service(value: object, context: Context) -> object:
    """Bind a Service value to the Context through which it was resolved."""

    if isinstance(value, Service):
        return ServiceView(value, context)
    return value


def _normalize_inject(dependencies: InjectSpec) -> dict[str, object]:
    if isinstance(dependencies, Mapping):
        return {str(name): config for name, config in dependencies.items()}
    return {str(name): None for name in dependencies}


class Context:
    """Root or derived view over the tutorial runtime."""

    filter = object()

    def __init__(self) -> None:
        self.root = self
        self._metadata: dict[object, object] = {}
        self.isolation: dict[str, object] = {}
        self.intercepts: dict[str, list[object]] = {}
        self._uid = 0
        self.fiber: RootFiber | Fiber = RootFiber(self)
        reflect = ReflectService(self)
        self.reflect = reflect.for_context(self)

    @classmethod
    def derive(cls, parent: Context, fiber: Fiber | None = None) -> Context:
        context = object.__new__(cls)
        context.__dict__ = parent.__dict__.copy()
        context.root = parent.root
        context._metadata = dict(parent._metadata)
        context.isolation = dict(parent.isolation)
        context.intercepts = {
            name: list(values) for name, values in parent.intercepts.items()
        }
        context.fiber = fiber or parent.fiber
        context.reflect = parent.root.reflect.service.for_context(context)
        return context

    def _next_uid(self) -> int:
        self._uid += 1
        return self._uid

    def extend(self, meta: Mapping[object, object] | None = None) -> Context:
        context = self.derive(self)
        if meta is not None:
            for name, value in meta.items():
                if isinstance(name, str):
                    setattr(context, name, value)
                else:
                    context._metadata[name] = value
        return context

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

    def metadata(self, key: object) -> object | None:
        return self._metadata.get(key)

    def plugin(self, plugin: object, config: object = None) -> Fiber:
        name = getattr(plugin, "name", None) or getattr(
            plugin, "__name__", type(plugin).__name__
        )
        spec = _normalize_inject(getattr(plugin, "inject", None) or ())
        return Fiber(self, str(name), plugin, config, tuple(spec), dict(spec)).start()

    def inject(self, dependencies: InjectSpec, callback: object) -> Fiber:
        spec = _normalize_inject(dependencies)
        name = getattr(callback, "__name__", type(callback).__name__)
        return Fiber(
            self,
            str(name),
            callback,
            None,
            tuple(spec),
            {dependency: spec[dependency] for dependency in spec},
        ).start()

    def get(self, name: str, strict: bool = True) -> object | None:
        accessor = self.reflect.accessor_record(name)
        if accessor is not None:
            return accessor.getter(self)
        implementation = (self.fiber.store or {}).get(name)
        if strict and implementation is not None:
            return bind_service(implementation.value, self)
        return bind_service(self.reflect.get(name, strict), self)

    def __getattr__(self, name: str) -> object | None:
        return self.get(name)

    def set(self, name: str, value: object) -> bool:
        return self.reflect.set(name, value)

    def provide(
        self,
        name: str,
        value: object,
        check: Callable[[], bool] | None = None,
    ) -> Effect:
        return self.reflect.provide(name, value, check)

    def accessor(self, name: str, options: AccessorProperty) -> Effect:
        return self.reflect.accessor(name, options)

    def mixin(
        self, source: str | object, members: Sequence[str] | Mapping[str, str]
    ) -> Effect:
        return self.reflect.mixin(source, members)

    async def aclose(self) -> None:
        await self.root.fiber.dispose()
