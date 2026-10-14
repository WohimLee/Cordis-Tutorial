"""Dependency-driven plugin lifecycle instances."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from enum import StrEnum
from functools import wraps
from typing import TYPE_CHECKING, cast

from .config import validate_config
from .effect import EffectMeta, EffectScope
from .errors import CordisError, CordisErrorCode
from .model import METHOD_INJECT

if TYPE_CHECKING:
    from .context import Context
    from .reflect import Impl
    from .registry import PluginRuntime


def resolveConfig(runtime: PluginRuntime, config: object) -> object:
    """Validate plugin config using the runtime's canonical declaration."""

    return validate_config(runtime.spec.validator, config, runtime.spec.name)


class FiberState(StrEnum):
    """Stable and transitional plugin lifecycle states."""

    PENDING = "PENDING"
    LOADING = "LOADING"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"
    UNLOADING = "UNLOADING"
    DISPOSED = "DISPOSED"


class RootFiber:
    """Permanent owner of top-level plugin Fibers until Context shutdown."""

    uid = 0
    name = "root"

    def __init__(self, context: Context) -> None:
        self.ctx = self.context = context
        self.state = FiberState.ACTIVE
        self.effects = EffectScope()
        self.store: dict[str, Impl] = {}
        self.provided_names: set[str] = set()

    def assertActive(self) -> None:
        if self.state is FiberState.DISPOSED:
            raise CordisError(CordisErrorCode.INACTIVE_EFFECT)

    def getEffects(self) -> tuple[EffectMeta, ...]:
        """Return diagnostic metadata for live Effects."""

        return self.effects.effects

    async def dispose(self) -> None:
        if self.state is FiberState.DISPOSED:
            return
        self.state = FiberState.UNLOADING
        await self.effects.close()
        self.state = FiberState.DISPOSED


class Fiber:
    """One plugin mount and its dependency-controlled activation epochs."""

    def __init__(
        self,
        uid: int,
        parent: Context,
        runtime: PluginRuntime,
        config: object,
    ) -> None:
        from .context import Context

        self.uid: int | None = uid
        self.parent = parent
        self.runtime = runtime
        self._config = config
        self.config = config
        self.ctx = self.context = Context.derive(parent, self)
        for name, intercept in runtime.spec.inject.items():
            if intercept is not None:
                self.context.intercepts.setdefault(name, []).append(intercept)
        self.state = FiberState.PENDING
        self.effects = EffectScope()
        self.store: dict[str, Impl] | None = None
        self.provided_names: set[str] = set()
        self.error: BaseException | None = None
        self._epoch: tuple[int, ...] | None = None
        self._failed_epoch: tuple[int, ...] | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._refresh_requested = False
        self._lifecycle_lock = asyncio.Lock()
        self._dispose_requested = False
        self._force_restart = False

    @property
    def name(self) -> str:
        """Diagnostic plugin name."""

        return self.runtime.spec.name

    def __await__(self):  # type: ignore[no-untyped-def]
        return self.wait().__await__()

    def assertActive(self) -> None:
        """Reject new owned resources after final disposal begins."""

        if self._dispose_requested or self.state is FiberState.DISPOSED:
            raise CordisError(CordisErrorCode.INACTIVE_EFFECT)

    def getEffects(self) -> tuple[EffectMeta, ...]:
        """Return diagnostic metadata for live Effects."""

        return self.effects.effects

    @property
    def inject(self) -> dict[str, object | None]:
        """Resolved dependency declaration for this plugin runtime."""

        return dict(self.runtime.spec.inject)

    @property
    def inertia(self) -> asyncio.Task[None] | None:
        """Current lifecycle transition task, if any."""

        task = self._refresh_task
        return task if task is not None and not task.done() else None

    def bootstrap(self) -> None:
        """Publish parent ownership and request initial dependency evaluation."""

        self.parent.fiber.effects.install_sync(
            lambda: self.dispose,
            f"ctx.plugin({self.name!r})",
        )
        self.request_refresh()

    def _set_state(self, state: FiberState) -> None:
        old_state = self.state
        if old_state is state:
            return
        self.state = state
        self.parent.root.events.emit_safe("internal/status", self, old_state)

    def request_refresh(self) -> None:
        """Schedule dependency evaluation, coalescing repeated notifications."""

        if self._dispose_requested:
            return
        self._refresh_requested = True
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def wait(self) -> Fiber:
        """Wait until all currently requested lifecycle work settles."""

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
            async with self._lifecycle_lock:
                await self._refresh_once()

    def _resolve_dependencies(self) -> tuple[dict[str, Impl], tuple[int, ...]] | None:
        resolved: dict[str, Impl] = {}
        epoch: list[int] = []
        for name in self.runtime.spec.inject:
            implementation = self.parent.root.reflect.implementation(self.context, name)
            if implementation is None:
                return None
            resolved[name] = implementation
            uid = implementation.fiber.uid
            if uid is None:
                return None
            epoch.append(uid)
        return resolved, tuple(epoch)

    async def _refresh_once(self) -> None:
        resolution = self._resolve_dependencies()
        if resolution is None:
            if self.state in {FiberState.ACTIVE, FiberState.FAILED}:
                await self._unload()
            return

        dependencies, epoch = resolution
        force_restart = self._force_restart
        self._force_restart = False
        if self.state is FiberState.ACTIVE and epoch == self._epoch and not force_restart:
            return
        if self.state is FiberState.FAILED and epoch == self._failed_epoch and not force_restart:
            return
        if self.state in {FiberState.ACTIVE, FiberState.FAILED}:
            await self._unload(settle_pending=False)
        if self._dispose_requested:
            return
        await self._activate(dependencies, epoch)

    async def _activate(
        self,
        dependencies: dict[str, Impl],
        epoch: tuple[int, ...],
    ) -> None:
        self._set_state(FiberState.LOADING)
        self.store = dependencies
        self._epoch = epoch
        self.error = None
        self.effects = EffectScope()
        try:
            self.config = await self.context.waterfall(
                "internal/config",
                self._config,
                next_=lambda: resolveConfig(self.runtime, self._config),
            )
            result = self._invoke_plugin()
            if inspect.isawaitable(result):
                result = await result
            if result is not None:
                await self.effects.install(lambda: result, f"plugin({self.name!r})")
            if self._dispose_requested or self._resolve_dependencies_epoch() != epoch:
                await self._unload()
                return
            self._set_state(FiberState.ACTIVE)
            for name in tuple(self.provided_names):
                label = self.parent.root.reflect.label(self.context, name)
                self.parent.root.reflect.notify_soon(name, label)
        except BaseException as error:
            self.error = error
            self._failed_epoch = epoch
            try:
                await self.effects.close()
            except BaseException as cleanup_error:
                self.error = BaseExceptionGroup(
                    f"plugin {self.name!r} activation and rollback failed",
                    [error, cleanup_error],
                )
            self.store = None
            self._epoch = None
            self._set_state(FiberState.FAILED)

    def _resolve_dependencies_epoch(self) -> tuple[int, ...] | None:
        resolution = self._resolve_dependencies()
        return None if resolution is None else resolution[1]

    def _invoke_plugin(self) -> object:
        plugin = self.runtime.spec.callback
        if inspect.isclass(plugin):
            constructor = cast(Callable[[object, object], object], plugin)
            instance = constructor(self.context, self.config)
            self._mount_injected_methods(instance)
            initialize = getattr(instance, "init", None)
            return initialize() if callable(initialize) else None
        apply = getattr(plugin, "apply", None)
        if not inspect.isfunction(plugin) and callable(apply):
            return apply(self.context, self.config)
        if not callable(plugin):
            raise CordisError(CordisErrorCode.INVALID_PLUGIN)
        callback = cast(Callable[[object, object], object], plugin)
        return callback(self.context, self.config)

    def _mount_injected_methods(self, instance: object) -> None:
        members: dict[str, object] = {}
        for base in reversed(type(instance).__mro__):
            members.update(vars(base))
        for name, member in members.items():
            dependencies = getattr(member, METHOD_INJECT, None)
            if dependencies is None:
                continue
            method = getattr(instance, name)

            @wraps(method)
            def invoke(
                _context: Context,
                _config: object,
                method: Callable[[], object] = method,
            ) -> object:
                return method()

            self.context.inject(dependencies, invoke)

    async def _unload(self, *, settle_pending: bool = True) -> None:
        self._set_state(FiberState.UNLOADING)
        try:
            await self.effects.close()
        finally:
            self.store = None
            self.provided_names.clear()
            self._epoch = None
            self.error = None
            if self._dispose_requested:
                self._set_state(FiberState.DISPOSED)
            elif settle_pending:
                self._set_state(FiberState.PENDING)

    async def restart(self) -> None:
        """Force a new activation using the current raw configuration."""

        self.assertActive()
        self._force_restart = True
        self.error = None
        self.request_refresh()
        await self.wait()

    async def update(self, config: object, no_save: bool = False) -> object:
        """Validate and apply configuration through the update waterfall."""

        self.assertActive()
        self._config = config
        if self.state is not FiberState.ACTIVE:
            self.error = None
            self._force_restart = True
            self.request_refresh()
            return None

        config = resolveConfig(self.runtime, config)

        async def apply_update() -> None:
            await self.restart()

        return await self.context.waterfall(
            "internal/update",
            config,
            no_save,
            next_=apply_update,
        )

    async def dispose(self) -> None:
        """Permanently unload and remove this Fiber."""

        if self.state is FiberState.DISPOSED:
            return
        self._dispose_requested = True
        failure: BaseException | None = None
        async with self._lifecycle_lock:
            try:
                if cast(FiberState, self.state) is not FiberState.DISPOSED:
                    await self._unload()
            except BaseException as error:
                failure = error
            finally:
                self.parent.root.registry.remove(self)
                self.uid = None
                self.parent.root.events.emit_safe("internal/plugin", self)
        if failure is not None:
            raise failure
