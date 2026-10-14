"""Lifecycle-owned reversible effects."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
from collections.abc import AsyncIterable, Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import TypeAlias, cast

from .errors import CordisError, CordisErrorCode

Cleanup: TypeAlias = Callable[[], object]
Disposable: TypeAlias = Cleanup
EffectResult: TypeAlias = (
    Cleanup | Iterable[Cleanup] | AsyncIterable[Cleanup] | Awaitable[object] | None
)
EffectSetup: TypeAlias = Callable[[], object]


@dataclass(slots=True)
class EffectMeta:
    """Read-only diagnostic metadata for an effect ownership tree."""

    label: str
    children: list[EffectMeta] = field(default_factory=lambda: list[EffectMeta]())


async def _await_cleanup(cleanup: Cleanup) -> None:
    result = cleanup()
    if inspect.isawaitable(result):
        await result


class Effect:
    """A setup operation and the cleanup callbacks it produced."""

    def __init__(self, label: str = "anonymous") -> None:
        self.meta = EffectMeta(label)
        self._cleanups: list[Cleanup] = []
        self._dispose_task: asyncio.Task[None] | None = None
        self._setup_task: asyncio.Task[Effect] | None = None
        self._started = False
        self._setup_complete = asyncio.Event()
        self._nested = False

    @property
    def disposed(self) -> bool:
        """Whether disposal has begun."""

        return self._dispose_task is not None

    @property
    def nested(self) -> bool:
        """Whether this effect is represented beneath another effect."""

        return self._nested

    def __call__(self) -> asyncio.Task[None]:
        """Begin disposal, returning the shared cleanup task."""

        if self._dispose_task is None:
            self._dispose_task = asyncio.create_task(self._dispose())
        return self._dispose_task

    def __await__(self):  # type: ignore[no-untyped-def]
        """Wait for setup and return this callable disposer."""

        return self._wait_setup().__await__()

    async def _wait_setup(self) -> Effect:
        if self._setup_task is not None:
            await asyncio.shield(self._setup_task)
        else:
            await self._setup_complete.wait()
        return self

    def attach_to(self, parent: Effect) -> None:
        """Attach diagnostic metadata to a parent effect."""

        parent.meta.children.append(self.meta)
        parent._cleanups.append(self)
        self._nested = True

    async def start(self, setup: EffectSetup) -> Effect:
        """Run setup and collect every cleanup it returns.

        If setup fails after yielding cleanups, those cleanups are rolled back
        before the original failure is re-raised.
        """

        if self._started:
            raise CordisError(CordisErrorCode.INVALID_EFFECT, "effect setup already started")
        self._started = True
        try:
            await self._collect(setup())
        except BaseException:
            self._setup_complete.set()
            try:
                await self.dispose()
            except BaseExceptionGroup:
                pass
            raise
        finally:
            self._setup_complete.set()
        return self

    def start_deferred(self, result: object) -> None:
        """Start collection for an asynchronous result without blocking its caller."""

        if self._started:
            raise CordisError(CordisErrorCode.INVALID_EFFECT, "effect setup already started")
        self._started = True

        async def collect() -> Effect:
            try:
                await self._collect(result)
            except BaseException:
                self._setup_complete.set()
                try:
                    await self.dispose()
                except BaseExceptionGroup:
                    pass
                raise
            finally:
                self._setup_complete.set()
            return self

        self._setup_task = asyncio.create_task(collect())

        def consume_failure(task: asyncio.Task[Effect]) -> None:
            if not task.cancelled():
                task.exception()

        self._setup_task.add_done_callback(consume_failure)

    def start_sync(self, setup: EffectSetup) -> Effect:
        """Run a setup that must produce only synchronous result shapes."""

        if self._started:
            raise CordisError(CordisErrorCode.INVALID_EFFECT, "effect setup already started")
        self._started = True
        try:
            self._collect_sync(setup())
        except BaseException:
            cleanups, self._cleanups = self._cleanups, []
            for cleanup in reversed(cleanups):
                result = cleanup()
                if inspect.isawaitable(result):
                    cast(Awaitable[object], result).close()  # type: ignore[attr-defined]
            raise
        finally:
            self._setup_complete.set()
        return self

    def _collect_sync(self, result: object) -> None:
        if result is None:
            return
        if callable(result):
            self._cleanups.append(cast(Cleanup, result))
            return
        if inspect.iscoroutine(result):
            result.close()
            raise TypeError("Invalid effect")
        if inspect.isawaitable(result) or isinstance(result, AsyncIterable):
            raise TypeError("Invalid effect")
        if isinstance(result, Iterable) and not isinstance(result, (str, bytes, bytearray)):
            for cleanup in cast(Iterable[object], result):
                self._append_cleanup(cleanup)
            return
        raise TypeError("Invalid effect")

    async def _collect(self, result: object) -> None:
        if callable(result):
            self._cleanups.append(cast(Cleanup, result))
            return
        if inspect.isawaitable(result):
            await self._collect(await cast(Awaitable[object], result))
            return
        if result is None:
            return
        if isinstance(result, AsyncIterable):
            async for cleanup in cast(AsyncIterable[object], result):
                self._append_cleanup(cleanup)
            return
        if isinstance(result, Iterable) and not isinstance(result, (str, bytes, bytearray)):
            for cleanup in cast(Iterable[object], result):
                self._append_cleanup(cleanup)
            return
        raise TypeError("Invalid effect")

    def _append_cleanup(self, cleanup: object) -> None:
        if not callable(cleanup):
            raise TypeError("Invalid effect")
        self._cleanups.append(cast(Cleanup, cleanup))

    async def dispose(self) -> None:
        """Run collected cleanups in reverse order exactly once."""

        await asyncio.shield(self())

    async def _dispose(self) -> None:
        await self._setup_complete.wait()
        errors: list[BaseException] = []
        while self._cleanups:
            cleanup = self._cleanups.pop()
            try:
                await _await_cleanup(cleanup)
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                errors.append(error)
        if errors:
            raise BaseExceptionGroup(f"effect {self.meta.label!r} cleanup failed", errors)


_current_effect: contextvars.ContextVar[Effect | None] = contextvars.ContextVar(
    "cordis_current_effect",
    default=None,
)


class EffectScope:
    """Own a collection of Effects and close them in reverse order."""

    def __init__(self) -> None:
        self._effects: list[Effect] = []
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def closed(self) -> bool:
        """Whether the scope no longer accepts effects."""

        return self._closed

    @property
    def effects(self) -> tuple[EffectMeta, ...]:
        """Return diagnostic metadata for currently owned effects."""

        return tuple(
            effect.meta for effect in self._effects if not effect.disposed and not effect.nested
        )

    async def install(self, setup: EffectSetup, label: str = "anonymous") -> Effect:
        """Create, publish, and start an owned effect."""

        effect = self.install_auto(setup, label)
        try:
            await effect
        except BaseException:
            self._effects.remove(effect)
            raise
        if self._closed:
            await effect.dispose()
            raise CordisError(CordisErrorCode.INACTIVE_EFFECT)
        return effect

    def install_auto(self, setup: EffectSetup, label: str = "anonymous") -> Effect:
        """Install an effect and defer collection only when setup is asynchronous."""

        if self._closed:
            raise CordisError(CordisErrorCode.INACTIVE_EFFECT)
        effect = Effect(label)
        parent = _current_effect.get()
        if parent is not None:
            effect.attach_to(parent)
        self._effects.append(effect)
        token = _current_effect.set(effect)
        try:
            result = setup()
            if not callable(result) and (
                inspect.isawaitable(result) or isinstance(result, AsyncIterable)
            ):
                effect.start_deferred(cast(object, result))
            else:
                effect.start_sync(lambda: result)
        except BaseException:
            self._effects.remove(effect)
            raise
        finally:
            _current_effect.reset(token)
        return effect

    def install_sync(self, setup: EffectSetup, label: str = "anonymous") -> Effect:
        """Install an effect whose setup is entirely synchronous."""

        if self._closed:
            raise CordisError(CordisErrorCode.INACTIVE_EFFECT)
        effect = Effect(label)
        parent = _current_effect.get()
        if parent is not None:
            effect.attach_to(parent)
        self._effects.append(effect)
        token = _current_effect.set(effect)
        try:
            effect.start_sync(setup)
        except BaseException:
            self._effects.remove(effect)
            raise
        finally:
            _current_effect.reset(token)
        return effect

    async def close(self) -> None:
        """Close every owned effect exactly once."""

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
            except BaseException as error:
                errors.append(error)
        if errors:
            raise BaseExceptionGroup("effect scope cleanup failed", errors)
