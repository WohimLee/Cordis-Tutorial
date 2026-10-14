"""Lifecycle-owned event hooks and dispatch modes."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypeAlias, TypedDict, cast

if TYPE_CHECKING:
    from .context import Context

Listener = Callable[..., object]
ListenerDisposer = Callable[[], object]
DispatchMode: TypeAlias = Literal["emit", "parallel", "serial", "bail", "waterfall"]


def isBailed(value: object) -> bool:
    """Return whether a listener result stops bail-style dispatch."""

    return value is not None and value is not False


EventOptions = TypedDict("EventOptions", {"prepend": bool, "global": bool}, total=False)


@dataclass(frozen=True, slots=True)
class Hook:
    """One registered listener and its owning Context."""

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
        """Register a listener and return its synchronous disposer."""

        context.fiber.assertActive()
        listener = context.reflect.bind(listener)
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

        dispose = self.on(
            context,
            name,
            callback,
            prepend=prepend,
            global_=global_,
        )
        return dispose

    def _callbacks(
        self,
        name: object,
        dispatch_context: Context | None = None,
    ) -> tuple[Listener, ...]:
        filter_ = (
            None if dispatch_context is None else dispatch_context.metadata(dispatch_context.filter)
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
        from .context import Context

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
        task = asyncio.ensure_future(awaitable)
        self._tasks.add(task)

        def finish(completed: asyncio.Task[object]) -> None:
            self._tasks.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(finish)
        return task

    def emit_safe(self, name: object, *args: object) -> tuple[BaseException, ...]:
        """Notify observers without allowing them to interrupt framework cleanup."""

        errors: list[BaseException] = []
        for callback in self._callbacks(name):
            try:
                result = callback(*args)
                if inspect.isawaitable(result):
                    if inspect.iscoroutine(result):
                        result.close()
                    raise TypeError(f"safe event listener for {name!r} returned an awaitable")
            except BaseException as error:
                errors.append(error)
        return tuple(errors)

    def waterfall_sync(
        self,
        name: str,
        *args: object,
        next_: Callable[[], object],
    ) -> object:
        """Compose synchronous listeners around a final callback."""

        callbacks = self._callbacks(name)

        def dispatch(index: int) -> object:
            result = (
                next_()
                if index == len(callbacks)
                else callbacks[index](*args, lambda: dispatch(index + 1))
            )
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise TypeError(f"sync waterfall listener for {name!r} returned an awaitable")
            return result

        return dispatch(0)

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
        """Compose listeners as async around-middleware."""

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

    async def waterfall(self, name: object, *args: object, next_: Callable[[], object]) -> object:
        return await self.service.waterfall(name, *args, next_=next_)

    def waterfall_sync(self, name: str, *args: object, next_: Callable[[], object]) -> object:
        return self.service.waterfall_sync(name, *args, next_=next_)

    def emit_safe(self, name: object, *args: object) -> tuple[BaseException, ...]:
        return self.service.emit_safe(name, *args)

    def __getattr__(self, name: str) -> object:
        return getattr(self.service, name)
