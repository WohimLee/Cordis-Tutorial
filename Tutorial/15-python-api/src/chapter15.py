"""Public surface inventory and Python language differences for chapter 15.

Self-contained teaching module: it imports only the standard library and never
imports ``cordis``.  Unlike chapters 12-14 this module is deliberately small: it
exists to *show* each portable contract next to the TypeScript mechanism it
replaces, and to record which package-root exports the tutorial has introduced.

It does not invent compatibility aliases.  Where Cordis names cannot be spelled
in Python (``Context.is``, ``Fiber.await``, a ``global`` field), the module uses
the single documented Python spelling and says so in :data:`API_MATRIX`.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, TypeAlias, TypedDict, cast

Cleanup = Callable[[], object]
Listener = Callable[..., object]
Status: TypeAlias = Literal["exact", "equivalent", "language-specific", "extension"]


class InactiveEffectError(RuntimeError):
    """Raised when a disposed owner tries to create a new long-lived resource."""


@dataclass(frozen=True, slots=True)
class ApiEntry:
    """One Cordis mechanism and the Python contract that carries it."""

    concept: str
    status: Status
    python: str
    chapter: str
    note: str


API_MATRIX: tuple[ApiEntry, ...] = (
    ApiEntry("Context()", "exact", "Context()", "01", "根作用域与派生作用域"),
    ApiEntry(
        "Context.is(value)",
        "language-specific",
        "Context.is_context(value)",
        "15",
        "`is` 是 Python 语法，不能作为方法名",
    ),
    ApiEntry(
        "ctx.extend(meta?)", "exact", "Context.extend(meta)", "01", "子作用域元数据继承"
    ),
    ApiEntry(
        "Context Symbol keys",
        "language-specific",
        "Context.filter sentinel + Context.metadata()",
        "12",
        "Python 没有 Symbol identity，用不透明 sentinel 代替",
    ),
    ApiEntry(
        "Fiber.await()",
        "language-specific",
        "await fiber / fiber.wait()",
        "03",
        "`await` 不能作为方法名，Fiber 自身可等待",
    ),
    ApiEntry(
        "EventOptions { global }",
        "equivalent",
        "EventOptions 键 'global' + Hook.global_ 字段",
        "12",
        "`global` 是关键字，只有存储字段改名",
    ),
    ApiEntry(
        "thenable disposer",
        "equivalent",
        "可 await 且可调用的 Effect",
        "02",
        "Python 原生 await 协议保留同一能力",
    ),
    ApiEntry(
        "Proxy",
        "equivalent",
        "ServiceProxy.__getattr__ + ContextVar 调用方绑定",
        "13",
        "Python 没有 prototype/Proxy，用显式视图表达",
    ),
    ApiEntry(
        "Symbol tracking",
        "language-specific",
        "不透明 sentinel identity",
        "15",
        "不复制 Symbol identity，只保留可观察语义",
    ),
    ApiEntry(
        "prototype inheritance",
        "equivalent",
        "Context.derive 复制父元数据",
        "01",
        "显式父 Context，不修改父作用域",
    ),
    ApiEntry(
        "AggregateError",
        "equivalent",
        "BaseExceptionGroup",
        "11",
        "原生聚合异常，cleanup/parallel 语义相同",
    ),
    ApiEntry(
        "Plugin.Runtime namespace",
        "language-specific",
        "导出的 PluginRuntime",
        "04",
        "Python 类型联合无法携带 TypeScript 命名空间",
    ),
    ApiEntry(
        "decorator initializers",
        "language-specific",
        "类/方法装饰器 + 构造后扫描",
        "06",
        "Python 装饰器没有 JavaScript initializer 钩子",
    ),
    ApiEntry(
        "Standard Schema symbol",
        "equivalent",
        "ConfigValidator 适配器",
        "09",
        "Python 生态没有共享的 symbol 属性",
    ),
    ApiEntry(
        "V8 stack rewriting",
        "language-specific",
        "原生 traceback + Effect label",
        "10",
        "不制造 JavaScript 栈字符串",
    ),
)


# ``cordis`` package-root ``__all__`` snapshot (41 names).  The value is the
# tutorial chapter that introduces the contract, or ``later``/``extension``.
# Copied from ``cordis-py/src/cordis/__init__.py``; see the compatibility matrix.
CORDIS_ROOT_EXPORTS: dict[str, str] = {
    "ConfigValidator": "09",
    "Context": "01",
    "CordisError": "13",
    "CordisErrorCode": "13",
    "DispatchMode": "12",
    "Disposable": "02",
    "DisposableList": "later",
    "Effect": "02",
    "EffectMeta": "10",
    "EventOptions": "12",
    "EventsService": "12",
    "Exporter": "14",
    "Fiber": "03",
    "FiberState": "09",
    "Formatter": "14",
    "Hook": "12",
    "Impl": "05",
    "Inject": "06",
    "Logger": "14",
    "LoggerLevel": "14",
    "LoggerMethod": "14",
    "LoggerOptions": "14",
    "LoggerService": "14",
    "LoggerType": "14",
    "Message": "14",
    "Plugin": "04",
    "PluginContext": "04",
    "PluginFunction": "04",
    "PluginObject": "04",
    "PluginRuntime": "04",
    "Property": "13",
    "ReflectService": "05",
    "RegistryService": "04",
    "Service": "13",
    "ValidationError": "09",
    "__version__": "extension",
    "c16": "14",
    "c256": "14",
    "defaultFormatters": "14",
    "isBailed": "12",
    "resolveConfig": "09",
}

CHAPTER_TAGS = frozenset(
    {
        "01",
        "02",
        "03",
        "04",
        "05",
        "06",
        "07",
        "08",
        "09",
        "10",
        "11",
        "12",
        "13",
        "14",
        "15",
    }
)


class FiberState(StrEnum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"
    DISPOSED = "DISPOSED"


class Effect:
    """The ``thenable disposer`` contract: awaitable *and* callable."""

    def __init__(self, label: str = "anonymous") -> None:
        self.label = label
        self._cleanup: Cleanup | None = None
        self._setup_done = asyncio.Event()
        self._dispose_task: asyncio.Task[None] | None = None

    def __await__(self):  # type: ignore[no-untyped-def]
        return self._wait().__await__()

    async def _wait(self) -> Effect:
        await self._setup_done.wait()
        return self

    async def start(self, setup: Callable[[], object]) -> Effect:
        try:
            result = setup()
            if inspect.isawaitable(result):
                result = await cast("asyncio.Future[object]", result)
            if result is not None and not callable(result):
                raise TypeError("invalid Effect cleanup")
            self._cleanup = cast(Cleanup | None, result)
        finally:
            self._setup_done.set()
        return self

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


class RootFiber:
    uid = 0
    name = "root"

    def __init__(self, context: Context) -> None:
        self.ctx = self.context = context
        self.state = FiberState.ACTIVE

    def assert_active(self) -> None:
        if self.state is FiberState.DISPOSED:
            raise InactiveEffectError("inactive effect")

    async def dispose(self) -> None:
        self.state = FiberState.DISPOSED


class Fiber:
    """Awaitable plugin mount, in the two documented Python spellings."""

    def __init__(
        self,
        parent: Context,
        name: str,
        plugin: object,
        config: object = None,
    ) -> None:
        self.parent = parent
        self.context = Context.derive(parent, self)
        self.name = name
        self.plugin = plugin
        self.config = config
        self.state = FiberState.PENDING
        self.error: BaseException | None = None
        self.effects: list[Effect] = []
        self._task: asyncio.Task[None] | None = None
        self._dispose_requested = False

    def __await__(self):  # type: ignore[no-untyped-def]
        return self.wait().__await__()

    def start(self) -> Fiber:
        self._task = asyncio.create_task(self._activate())
        return self

    def assert_active(self) -> None:
        if self._dispose_requested or self.state is FiberState.DISPOSED:
            raise InactiveEffectError("inactive effect")

    async def wait(self) -> Fiber:
        task = self._task
        if task is not None:
            await asyncio.shield(task)
        if self.error is not None:
            raise self.error
        return self

    async def _activate(self) -> None:
        self.error = None
        try:
            result = self.plugin(self.context, self.config)
            if inspect.isawaitable(result):
                result = await cast("asyncio.Future[object]", result)
            if result is not None:
                if not callable(result):
                    raise TypeError("plugin result must be a cleanup callable or None")
                effect = await Effect(f"plugin({self.name!r})").start(lambda: result)
                self.effects.append(effect)
            if self._dispose_requested:
                await self._unload()
                return
            self.state = FiberState.ACTIVE
        except BaseException as error:  # noqa: BLE001 - cancellation boundary: roll back partial activation
            self.error = error
            await self._unload()
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
        errors: list[BaseException] = []
        while self.effects:
            try:
                await self.effects.pop().dispose()
            except BaseException as error:  # noqa: BLE001 - cleanup aggregation: keep cleaning after any failure
                errors.append(error)
        if self.state is not FiberState.FAILED:
            self.state = FiberState.DISPOSED
        if errors:
            raise BaseExceptionGroup(f"fiber {self.name!r} cleanup failed", errors)


EventOptions = TypedDict("EventOptions", {"prepend": bool, "global": bool}, total=False)


@dataclass(frozen=True, slots=True)
class Hook:
    """``EventOptions`` keeps ``global``; the stored field must be ``global_``."""

    ctx: Context
    callback: Listener
    global_: bool = False


class Events:
    """One shared bus; only the dispatch and the two spelled differences matter."""

    def __init__(self) -> None:
        self._hooks: dict[object, list[Hook]] = {}

    def on(
        self,
        context: Context,
        name: object,
        listener: Listener,
        options: EventOptions | None = None,
    ) -> Callable[[], None]:
        config = options or {}
        hook = Hook(context, listener, config.get("global", False))
        hooks = self._hooks.setdefault(name, [])
        hooks.append(hook)

        def dispose() -> None:
            if hook in hooks:
                hooks.remove(hook)

        return dispose

    def hooks(self, name: object) -> tuple[Hook, ...]:
        return tuple(self._hooks.get(name, ()))

    def emit(self, name: object, *args: object) -> None:
        dispatch_context: Context | None = None
        if isinstance(name, Context):
            dispatch_context, name, args = name, args[0], args[1:]
        for hook in self.hooks(name):
            if dispatch_context is not None and not hook.global_:
                filter_ = dispatch_context.metadata(dispatch_context.filter)
                if callable(filter_) and not filter_(hook.ctx):
                    continue
            hook.callback(*args)

    async def parallel(self, name: object, *args: object) -> None:
        """``AggregateError`` becomes ``BaseExceptionGroup``."""

        async def invoke(callback: Listener) -> None:
            result = callback(*args)
            if inspect.isawaitable(result):
                await cast("asyncio.Future[object]", result)

        results = await asyncio.gather(
            *(invoke(hook.callback) for hook in self.hooks(name)),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            raise BaseExceptionGroup(f"parallel event {name!r} failed", errors)


_current_context: contextvars.ContextVar[Context | None] = contextvars.ContextVar(
    "tutorial_caller_context",
    default=None,
)


def current_context() -> Context | None:
    """Return the Context a proxied method call is currently bound to."""

    return _current_context.get()


class ServiceProxy:
    """The ``Proxy`` contract without prototypes: forward and bind on call."""

    __slots__ = ("_context", "_target")

    def __init__(self, target: object, context: Context) -> None:
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


class Context:
    """Root or derived scope that carries metadata and the event bus."""

    filter = object()

    def __init__(self) -> None:
        self.root = self
        self._metadata: dict[object, object] = {}
        self.fiber: RootFiber | Fiber = RootFiber(self)
        self.events = Events()

    @classmethod
    def derive(cls, parent: Context, fiber: Fiber | None = None) -> Context:
        context = object.__new__(cls)
        context.__dict__ = parent.__dict__.copy()
        context.root = parent.root
        context._metadata = dict(parent._metadata)
        context.fiber = fiber or parent.fiber
        return context

    @staticmethod
    def is_context(value: object) -> bool:
        """The documented Python spelling of ``Context.is(value)``."""

        return isinstance(value, Context)

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
        options: EventOptions | None = None,
    ) -> Callable[[], None]:
        return self.events.on(self, name, listener, options)

    def emit(self, name: object, *args: object) -> None:
        self.events.emit(name, *args)

    async def parallel(self, name: object, *args: object) -> None:
        await self.events.parallel(name, *args)

    async def aclose(self) -> None:
        await self.root.fiber.dispose()


__all__ = [
    "API_MATRIX",
    "CHAPTER_TAGS",
    "CORDIS_ROOT_EXPORTS",
    "ApiEntry",
    "Context",
    "Effect",
    "EventOptions",
    "Events",
    "Fiber",
    "FiberState",
    "Hook",
    "ServiceProxy",
    "current_context",
]
