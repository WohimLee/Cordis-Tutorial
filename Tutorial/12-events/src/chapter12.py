"""Lifecycle-owned event hooks and the five dispatch modes for tutorial chapter 12.

The module is self-contained: it imports only the standard library and never
imports ``cordis``.  Chapters 03-11 already built Fiber/Effect ownership; this
chapter keeps the smallest version of those two mechanisms that the event
system needs, then layers ``EventsService`` on top of them.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, TypeAlias, TypedDict, cast

Cleanup = Callable[[], object]
Listener = Callable[..., object]
ListenerDisposer = Callable[[], object]
Plugin = Callable[[object, object], object]
DispatchMode: TypeAlias = Literal["emit", "parallel", "serial", "bail", "waterfall"]


# ``global`` is a Python keyword, so the exact Cordis options mapping can only be
# declared with the functional TypedDict form.  The stored :class:`Hook` field is
# then spelled ``global_`` (the documented Python language difference).
EventOptions = TypedDict("EventOptions", {"prepend": bool, "global": bool}, total=False)


def isBailed(value: object) -> bool:
    """Return whether a listener result stops bail-style dispatch.

    Only ``None`` and ``False`` keep dispatching.  ``0``, ``""`` and every other
    object bail, which is why this helper must not use truthiness.
    """

    return value is not None and value is not False


class InactiveEffectError(RuntimeError):
    """Raised when a disposed owner tries to create a new long-lived resource."""


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
            await cast(Awaitable[object], result)


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
        """Run a synchronous setup now and own whatever cleanup it returns."""

        if self._closed:
            raise InactiveEffectError("closed scope cannot own a new Effect")
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

    def assert_active(self) -> None:
        if self.state is FiberState.DISPOSED:
            raise InactiveEffectError("inactive effect")

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
    """One plugin mount: the owner of every listener that plugin registers."""

    def __init__(
        self,
        parent: Context,
        name: str,
        plugin: object,
        config: object = None,
    ) -> None:
        self.parent = parent
        self.ctx = self.context = Context.derive(parent, self)
        self.name = name
        self.plugin = plugin
        self.config = config
        self.state = FiberState.PENDING
        self.error: BaseException | None = None
        self.effects = EffectScope()
        self._task: asyncio.Task[None] | None = None
        self._dispose_requested = False
        self.parent.fiber.effects.install_sync(
            lambda: self.dispose,
            f"ctx.plugin({name!r})",
        )

    def __await__(self):  # type: ignore[no-untyped-def]
        return self.wait().__await__()

    def start(self) -> Fiber:
        if not self._dispose_requested:
            self._task = asyncio.create_task(self._activate())
        return self

    async def wait(self) -> Fiber:
        task = self._task
        if task is not None:
            await asyncio.shield(task)
        if self.error is not None:
            raise self.error
        return self

    async def _activate(self) -> None:
        self.state = FiberState.LOADING
        self.error = None
        try:
            result = self.plugin(self.context, self.config)
            if inspect.isawaitable(result):
                result = await cast(Awaitable[object], result)
            if result is not None:
                if not callable(result):
                    raise TypeError("plugin result must be a cleanup callable or None")
                self.effects.install_sync(lambda: result, f"plugin({self.name!r})")
            if self._dispose_requested:
                await self._unload()
                return
            self.state = FiberState.ACTIVE
        except BaseException as error:  # noqa: BLE001 - cancellation boundary: roll back partial activation
            self.error = error
            try:
                await self.effects.close()
            except BaseException as cleanup_error:  # noqa: BLE001 - aggregate activation and rollback errors
                self.error = BaseExceptionGroup(
                    f"plugin {self.name!r} activation and rollback failed",
                    [error, cleanup_error],
                )
            self.state = FiberState.FAILED

    async def dispose(self) -> None:
        if self.state is FiberState.DISPOSED:
            return
        self._dispose_requested = True
        task = self._task
        if task is not None and not task.done():
            await asyncio.shield(task)
        await self._unload()

    async def _unload(self) -> None:
        if self.state is FiberState.DISPOSED:
            return
        self.state = FiberState.UNLOADING
        try:
            await self.effects.close()
        finally:
            self.state = FiberState.DISPOSED

    def get_effects(self) -> tuple[str, ...]:
        return self.effects.labels

    def assert_active(self) -> None:
        """Reject new owned listeners once final disposal has begun."""

        if self._dispose_requested or self.state is FiberState.DISPOSED:
            raise InactiveEffectError("inactive effect")


@dataclass(frozen=True, slots=True)
class Hook:
    """One registered listener and the Context that owns it."""

    ctx: Context
    callback: Listener
    global_: bool = False


class EventsService:
    """Shared event bus for a root Context and all derived Contexts."""

    def __init__(self, root: Context) -> None:
        self.ctx = self.root = root
        self._hooks: dict[object, list[Hook]] = {}
        self._tasks: set[asyncio.Task[object]] = set()

    def for_context(self, context: Context) -> EventsServiceView:
        return EventsServiceView(self, context)

    def on(
        self,
        context: Context,
        name: object,
        listener: Listener,
        *,
        prepend: bool = False,
        global_: bool = False,
    ) -> ListenerDisposer:
        """Register a listener owned by ``context.fiber`` and return its disposer."""

        context.fiber.assert_active()
        replacement = self.bail(
            context,
            "internal/listener",
            name,
            listener,
            {"prepend": prepend, "global": global_},
        )
        if replacement is not None:
            if not callable(replacement):
                raise TypeError("internal/listener replacement must be callable")
            return cast(ListenerDisposer, replacement)
        hook = Hook(context, listener, global_)
        hooks = self._hooks.setdefault(name, [])

        def cleanup() -> bool | None:
            try:
                hooks.remove(hook)
            except ValueError:
                return None
            if not hooks:
                self._hooks.pop(name, None)
            return True

        def setup() -> ListenerDisposer:
            hooks.insert(0 if prepend else len(hooks), hook)
            return cleanup

        context.fiber.effects.install_sync(setup, f"ctx.on({name!r})")

        def dispose() -> None:
            cleanup()

        return dispose

    def once(
        self,
        context: Context,
        name: object,
        listener: Listener,
        *,
        prepend: bool = False,
        global_: bool = False,
    ) -> ListenerDisposer:
        """Register a listener that removes itself before its first call."""

        def callback(*args: object) -> object:
            dispose()
            return listener(*args)

        dispose = self.on(context, name, callback, prepend=prepend, global_=global_)
        return dispose

    def _callbacks(
        self,
        name: object,
        dispatch_context: Context | None = None,
    ) -> tuple[Listener, ...]:
        filter_ = (
            None
            if dispatch_context is None
            else dispatch_context.metadata(dispatch_context.filter)
        )
        if filter_ is not None and not callable(filter_):
            raise TypeError("Context.filter metadata must be callable")
        return tuple(
            hook.callback
            for hook in self._hooks.get(name, ())
            if hook.global_ or filter_ is None or filter_(hook.ctx)
        )

    def _resolve_dispatch(
        self,
        name: object,
        args: tuple[object, ...],
    ) -> tuple[Context | None, object, tuple[object, ...]]:
        if not isinstance(name, Context):
            return None, name, args
        if not args:
            raise TypeError("dispatch Context must be followed by an event name")
        return name, args[0], args[1:]

    def _report_dispatch(
        self,
        mode: str,
        name: object,
        args: tuple[object, ...],
        dispatch_context: Context | None,
    ) -> None:
        if not isinstance(name, str) or not name.startswith("internal/"):
            self.emit_safe("internal/dispatch", mode, name, args, dispatch_context)

    def _schedule(self, awaitable: Awaitable[object]) -> asyncio.Task[object]:
        """Keep fire-and-forget work alive and retrieve its result exactly once."""

        task = asyncio.ensure_future(awaitable)
        self._tasks.add(task)

        def finish(completed: asyncio.Task[object]) -> None:
            self._tasks.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(finish)
        return task

    @property
    def pending_tasks(self) -> tuple[asyncio.Task[object], ...]:
        return tuple(self._tasks)

    def emit_safe(self, name: object, *args: object) -> tuple[BaseException, ...]:
        """Notify observers without letting one observer interrupt cleanup."""

        errors: list[BaseException] = []
        for callback in self._callbacks(name):
            try:
                result = callback(*args)
                if inspect.isawaitable(result):
                    if inspect.iscoroutine(result):
                        result.close()
                    raise TypeError(
                        f"safe event listener for {name!r} returned an awaitable"
                    )
            except BaseException as error:  # noqa: BLE001 - observer isolation: never interrupt dispatch
                errors.append(error)
        return tuple(errors)

    def emit(self, name: object, *args: object) -> None:
        """Synchronously notify every listener in registration order."""

        dispatch_context, name, args = self._resolve_dispatch(name, args)
        self._report_dispatch("emit", name, args, dispatch_context)
        for callback in self._callbacks(name, dispatch_context):
            result = callback(*args)
            if inspect.isawaitable(result):
                self._schedule(cast(Awaitable[object], result))

    def bail(self, name: object, *args: object) -> object:
        """Return the first synchronous bail value."""

        dispatch_context, name, args = self._resolve_dispatch(name, args)
        self._report_dispatch("bail", name, args, dispatch_context)
        for callback in self._callbacks(name, dispatch_context):
            result = callback(*args)
            if inspect.isawaitable(result):
                result = self._schedule(cast(Awaitable[object], result))
            if isBailed(result):
                return result
        return None

    async def serial(self, name: object, *args: object) -> object:
        """Await listeners in order and return the first bail value."""

        dispatch_context, name, args = self._resolve_dispatch(name, args)
        self._report_dispatch("serial", name, args, dispatch_context)
        for callback in self._callbacks(name, dispatch_context):
            result = callback(*args)
            if inspect.isawaitable(result):
                result = await cast(Awaitable[object], result)
            if isBailed(result):
                return result
        return None

    async def parallel(self, name: object, *args: object) -> None:
        """Run all listeners concurrently and aggregate failures."""

        dispatch_context, name, args = self._resolve_dispatch(name, args)
        self._report_dispatch("emit", name, args, dispatch_context)

        async def invoke(callback: Listener) -> None:
            result = callback(*args)
            if inspect.isawaitable(result):
                await cast(Awaitable[object], result)

        results = await asyncio.gather(
            *(invoke(callback) for callback in self._callbacks(name, dispatch_context)),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            raise BaseExceptionGroup(f"parallel event {name!r} failed", errors)

    async def waterfall(
        self,
        name: object,
        *args: object,
        next_: Callable[[], object],
    ) -> object:
        """Compose listeners as around-middleware around a final callback."""

        dispatch_context, name, args = self._resolve_dispatch(name, args)
        self._report_dispatch("waterfall", name, args, dispatch_context)
        callbacks = self._callbacks(name, dispatch_context)

        async def dispatch(index: int) -> object:
            if index == len(callbacks):
                result = next_()
            else:
                result = callbacks[index](*args, lambda: dispatch(index + 1))
            if inspect.isawaitable(result):
                return await cast(Awaitable[object], result)
            return result

        return await dispatch(0)


class EventsServiceView:
    """Context-bound facade over the single shared :class:`EventsService`."""

    def __init__(self, service: EventsService, context: Context) -> None:
        self.service, self.ctx = service, context

    def on(
        self,
        name: object,
        listener: Listener,
        options: bool | EventOptions | None = None,
    ) -> ListenerDisposer:
        config = {"prepend": options} if isinstance(options, bool) else (options or {})
        return self.service.on(
            self.ctx,
            name,
            listener,
            prepend=config.get("prepend", False),
            global_=config.get("global", False),
        )

    def once(
        self,
        name: object,
        listener: Listener,
        options: bool | EventOptions | None = None,
    ) -> ListenerDisposer:
        config = {"prepend": options} if isinstance(options, bool) else (options or {})
        return self.service.once(
            self.ctx,
            name,
            listener,
            prepend=config.get("prepend", False),
            global_=config.get("global", False),
        )

    def emit(self, name: object, *args: object) -> None:
        self.service.emit(name, *args)

    def bail(self, name: object, *args: object) -> object:
        return self.service.bail(name, *args)

    async def serial(self, name: object, *args: object) -> object:
        return await self.service.serial(name, *args)

    async def parallel(self, name: object, *args: object) -> None:
        await self.service.parallel(name, *args)

    async def waterfall(
        self, name: object, *args: object, next_: Callable[[], object]
    ) -> object:
        return await self.service.waterfall(name, *args, next_=next_)

    def __getattr__(self, name: str) -> object:
        return getattr(self.service, name)


class Context:
    """Root or derived view over the tutorial event bus."""

    filter = object()

    def __init__(self) -> None:
        self.root = self
        self._metadata: dict[object, object] = {}
        self.fiber: RootFiber | Fiber = RootFiber(self)
        self.events = EventsService(self).for_context(self)

    @classmethod
    def derive(cls, parent: Context, fiber: Fiber | None = None) -> Context:
        context = object.__new__(cls)
        context.__dict__ = parent.__dict__.copy()
        context.root = parent.root
        context._metadata = dict(parent._metadata)
        context.fiber = fiber or parent.fiber
        context.events = parent.root.events.service.for_context(context)
        return context

    def extend(self, meta: Mapping[object, object] | None = None) -> Context:
        context = self.derive(self)
        if meta is not None:
            for name, value in meta.items():
                if isinstance(name, str):
                    setattr(context, name, value)
                else:
                    context._metadata[name] = value
        return context

    def metadata(self, key: object) -> object | None:
        return self._metadata.get(key)

    def plugin(self, plugin: object, config: object = None) -> Fiber:
        name = getattr(plugin, "name", None) or getattr(
            plugin, "__name__", type(plugin).__name__
        )
        return Fiber(self, str(name), plugin, config).start()

    def on(
        self,
        name: object,
        listener: Listener,
        options: bool | EventOptions | None = None,
    ) -> ListenerDisposer:
        return self.events.on(name, listener, options)

    def once(
        self,
        name: object,
        listener: Listener,
        options: bool | EventOptions | None = None,
    ) -> ListenerDisposer:
        return self.events.once(name, listener, options)

    def emit(self, name: object, *args: object) -> None:
        self.events.emit(name, *args)

    def bail(self, name: object, *args: object) -> object:
        return self.events.bail(name, *args)

    async def serial(self, name: object, *args: object) -> object:
        return await self.events.serial(name, *args)

    async def parallel(self, name: object, *args: object) -> None:
        await self.events.parallel(name, *args)

    async def waterfall(
        self, name: object, *args: object, next_: Callable[[], object]
    ) -> object:
        return await self.events.waterfall(name, *args, next_=next_)

    async def aclose(self) -> None:
        await self.root.fiber.dispose()
