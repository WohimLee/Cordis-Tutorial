"""Structured logging with lifecycle-owned exporters for tutorial chapter 14.

Self-contained teaching module: it imports only the standard library and never
imports ``cordis``.  It keeps the Core Logger contracts (levels, ``Message``,
formatters, level inheritance, bounded buffer and Fiber association) and
deliberately omits the terminal color palettes.

A teaching in-memory exporter is *not* part of this module: the real project
keeps it outside Core in ``cordis_observer``, and the chapter tests define their
own local helper with the same shape.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
import weakref
from collections.abc import Awaitable, Callable, Mapping
from enum import IntEnum
from typing import ClassVar, Literal, NotRequired, Protocol, TypeAlias, TypedDict, cast

Cleanup = Callable[[], object]


class InactiveEffectError(RuntimeError):
    """Raised when a disposed owner tries to create a new long-lived resource."""


LoggerType: TypeAlias = Literal["error", "info", "warn", "debug"]
LoggerMethod: TypeAlias = Callable[..., None]


class LoggerLevel(IntEnum):
    """Numeric severity, identical to Cordis ``LoggerLevel``."""

    ERROR = 0
    INFO = 1
    WARN = 2
    DEBUG = 3


class LoggerOptions(TypedDict):
    name: str
    meta: NotRequired[Mapping[str, object]]
    level: NotRequired[int]


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

    def install_sync(
        self, setup: Callable[[], object], label: str = "anonymous"
    ) -> Effect:
        if self._closed:
            raise InactiveEffectError("closed scope cannot own an Effect")
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


class Message:
    """One structured log record, identical in fields to Cordis ``Message``."""

    def __init__(
        self,
        sn: int,
        ts: int,
        name: str,
        type: str,
        level: int,
        args: tuple[object, ...],
        fiber: weakref.ReferenceType[Fiber | RootFiber] | None = None,
        **meta: object,
    ) -> None:
        self.sn, self.ts, self.name = sn, ts, name
        self.type, self.level, self.args, self.fiber = type, level, args, fiber
        self.__dict__.update(meta)


class Exporter(Protocol):
    def export(self, message: Message) -> None: ...


class _Defaults:
    colors = False
    maxLength = 10240
    levels: ClassVar[dict[str, int]] = {}
    formatters: ClassVar[dict[str, object]] = {}

    def export(self, message: Message) -> None:
        return None


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def _hyphenate(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "-", value.replace("_", "-")).lower()


Formatter: TypeAlias = Callable[[object, Exporter, Message], object]


def _string(value: object, _exporter: Exporter, _message: Message) -> str:
    return str(value)


def _integer(value: object, _exporter: Exporter, _message: Message) -> int:
    return int(float(cast(int | float | str, value)))


def _float(value: object, _exporter: Exporter, _message: Message) -> int | float:
    number = float(cast(int | float | str, value))
    return int(number) if number.is_integer() else number


def _object(value: object, _exporter: Exporter, _message: Message) -> str:
    return _json(value)


def _empty(_value: object, _exporter: Exporter, _message: Message) -> str:
    return ""


def _plain(value: object, _exporter: Exporter, _message: Message) -> str:
    """The color placeholder without the terminal palette."""

    return str(value)


defaultFormatters: dict[str, Formatter] = {
    "s": _string,
    "d": _integer,
    "i": _integer,
    "f": _float,
    "o": _object,
    "O": _object,
    "c": _empty,
    "C": _plain,
}


class Logger:
    def __init__(self, options: Mapping[str, object], service: LoggerService) -> None:
        self.service = service
        self.name = str(options["name"])
        level = options.get("level")
        self.level = level if isinstance(level, int) else None
        raw_meta = options.get("meta", {})
        self.meta: dict[str, object] = (
            {
                str(key): value
                for key, value in cast(Mapping[object, object], raw_meta).items()
            }
            if isinstance(raw_meta, Mapping)
            else {}
        )

    @staticmethod
    def format(exporter: Exporter, message: Message) -> str:
        """Render ``message`` with the exporter's formatters and limits."""

        args = list(message.args)
        if args and isinstance(args[0], BaseException):
            args[0] = str(args[0])
            args.insert(0, "%s")
        elif not args or not isinstance(args[0], str):
            args.insert(0, "%o")
        template = str(args.pop(0))
        output, consumed, index = "", 0, 0
        while index < len(template):
            if template[index] != "%" or index + 1 == len(template):
                output += template[index]
                index += 1
                continue
            char = template[index + 1]
            if char == "%":
                output += "%"
                index += 2
                continue
            custom = getattr(exporter, "formatters", {}).get(
                char
            ) or defaultFormatters.get(char)
            value = args[consumed] if consumed < len(args) else None
            if callable(custom):
                formatter = cast(Formatter, custom)
                output += str(formatter(value, exporter, message))
                consumed += 1
            else:
                output += f"%{char}"
            index += 2
        for value in args[consumed:]:
            structured = isinstance(value, (dict, list, tuple))
            output += " " + (_json(value) if structured else str(value))
        maximum = getattr(exporter, "maxLength", 10240)
        return "\n".join(
            line[:maximum] + ("..." if len(line) > maximum else "")
            for line in output.splitlines()
        )

    def _write(self, type_: str, level: LoggerLevel, args: tuple[object, ...]) -> None:
        self.service.write(self, type_, level, args)

    def debug(self, value: object, *args: object) -> None:
        self._write("debug", LoggerLevel.DEBUG, (value, *args))

    def info(self, value: object, *args: object) -> None:
        self._write("info", LoggerLevel.INFO, (value, *args))

    def warn(self, value: object, *args: object) -> None:
        self._write("warn", LoggerLevel.WARN, (value, *args))

    def error(self, value: object, *args: object) -> None:
        self._write("error", LoggerLevel.ERROR, (value, *args))


class _BufferExporter(_Defaults):
    """The always-present bounded history buffer owned by LoggerService."""

    def __init__(self, service: LoggerService) -> None:
        self.service = service

    def export(self, message: Message) -> None:
        self.service.buffer.append(message)
        if len(self.service.buffer) > self.service.bufferSize:
            if self.service.bufferSize:
                del self.service.buffer[: -self.service.bufferSize]
            else:
                self.service.buffer.clear()


class LoggerService:
    """One shared logger registry, buffer and exporter table per root Context."""

    def __init__(self, root: Context) -> None:
        self.ctx = self.root = root
        self.bufferSize = 1000
        self.buffer: list[Message] = []
        self.exporters: dict[int, Exporter] = {}
        self._snMessage = self._snExporter = 0
        self._add_exporter(_BufferExporter(self))

    def for_context(self, context: Context) -> LoggerServiceView:
        return LoggerServiceView(self, context)

    def __call__(self, name: str | None = None) -> Logger:
        return self.create(self.root, name)

    def create(self, context: Context, name: str | None = None) -> Logger:
        """Resolve name, level and Fiber metadata from the creating Context."""

        config: dict[str, object] = {}
        for value in context.intercepts.get("logger", ()):
            if isinstance(value, Mapping):
                config.update(cast(Mapping[str, object], value))
        resolved = name or config.get("name") or _hyphenate(context.fiber.name)
        level = config.get("level")
        return Logger(
            {
                "name": str(resolved),
                "level": level if isinstance(level, int) else None,
                "meta": {"fiber": weakref.ref(context.fiber)},
            },
            self,
        )

    def _add_exporter(self, exporter: Exporter) -> int:
        self._snExporter += 1
        self.exporters[self._snExporter] = exporter
        return self._snExporter

    def exporter_for(self, context: Context, exporter: Exporter) -> Effect:
        serial = 0

        def setup() -> object:
            nonlocal serial
            serial = self._add_exporter(exporter)
            return lambda: self.exporters.pop(serial, None)

        return context.fiber.effects.install_sync(setup, "ctx.exporter()")

    def exporter(self, exporter: Exporter) -> Effect:
        return self.exporter_for(self.ctx, exporter)

    def write(
        self,
        logger: Logger,
        type_: str,
        level: LoggerLevel,
        args: tuple[object, ...],
    ) -> None:
        """Filter by level first, then build one Message per surviving exporter."""

        if len(args) == 1 and isinstance(args[0], BaseException):
            if args[0].__cause__:
                logger.error(args[0].__cause__)
            elif isinstance(args[0], BaseExceptionGroup):
                for error in cast(
                    BaseExceptionGroup[BaseException], args[0]
                ).exceptions:
                    logger.error(error)
                return
        self._snMessage += 1
        for exporter in tuple(self.exporters.values()):
            levels = getattr(exporter, "levels", {})
            threshold = levels.get(logger.name, levels.get("default", logger.level))
            threshold = LoggerLevel.INFO if threshold is None else threshold
            if threshold < level:
                continue
            fields: dict[str, object] = {
                "sn": self._snMessage,
                "ts": int(time.time() * 1000),
                "name": logger.name,
                "type": type_,
                "level": level,
                **logger.meta,
            }
            known = {
                key: fields.pop(key) for key in ("sn", "ts", "name", "type", "level")
            }
            fiber = fields.pop("fiber", None)
            exporter.export(
                Message(
                    cast(int, known["sn"]),
                    cast(int, known["ts"]),
                    cast(str, known["name"]),
                    cast(str, known["type"]),
                    cast(int, known["level"]),
                    args,
                    cast("weakref.ReferenceType[Fiber | RootFiber] | None", fiber),
                    **fields,
                )
            )

    def debug(self, value: object, *args: object) -> None:
        self().debug(value, *args)

    def info(self, value: object, *args: object) -> None:
        self().info(value, *args)

    def warn(self, value: object, *args: object) -> None:
        self().warn(value, *args)

    def error(self, value: object, *args: object) -> None:
        self().error(value, *args)


class LoggerServiceView:
    """Context-bound facade that creates loggers named after that Context."""

    def __init__(self, service: LoggerService, context: Context) -> None:
        self.service, self.ctx = service, context

    def __call__(self, name: str | None = None) -> Logger:
        return self.service.create(self.ctx, name)

    def for_context(self, context: Context) -> LoggerServiceView:
        return self.service.for_context(context)

    def exporter(self, exporter: Exporter) -> Effect:
        return self.service.exporter_for(self.ctx, exporter)

    def __getattr__(self, name: str) -> object:
        return getattr(self(), name)


class RootFiber:
    uid = 0
    name = "root"

    def __init__(self, context: Context) -> None:
        self.ctx = self.context = context
        self.state = "ACTIVE"
        self.effects = EffectScope()

    def assert_active(self) -> None:
        if self.state == "DISPOSED":
            raise InactiveEffectError("inactive effect")

    async def dispose(self) -> None:
        if self.state == "DISPOSED":
            return
        self.state = "UNLOADING"
        try:
            await self.effects.close()
        finally:
            self.state = "DISPOSED"


class Fiber:
    """One plugin mount; its name becomes the default logger name."""

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
        self.state = "PENDING"
        self.error: BaseException | None = None
        self.effects = EffectScope()
        self._task: asyncio.Task[None] | None = None
        self._dispose_requested = False
        self.parent.fiber.effects.install_sync(
            lambda: self.dispose, f"ctx.plugin({name!r})"
        )

    def __await__(self):  # type: ignore[no-untyped-def]
        return self.wait().__await__()

    def start(self) -> Fiber:
        if not self._dispose_requested:
            self._task = asyncio.create_task(self._activate())
        return self

    def assert_active(self) -> None:
        if self._dispose_requested or self.state == "DISPOSED":
            raise InactiveEffectError("inactive effect")

    async def wait(self) -> Fiber:
        task = self._task
        if task is not None:
            await asyncio.shield(task)
        if self.error is not None:
            raise self.error
        return self

    async def _activate(self) -> None:
        self.state = "LOADING"
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
            self.state = "ACTIVE"
        except BaseException as error:  # noqa: BLE001 - cancellation boundary: roll back partial activation
            self.error = error
            try:
                await self.effects.close()
            except BaseException as cleanup_error:  # noqa: BLE001 - aggregate activation and rollback errors
                self.error = BaseExceptionGroup(
                    f"plugin {self.name!r} activation and rollback failed",
                    [error, cleanup_error],
                )
            self.state = "FAILED"

    async def dispose(self) -> None:
        if self.state == "DISPOSED":
            return
        self._dispose_requested = True
        task = self._task
        if task is not None and not task.done():
            await asyncio.shield(task)
        await self._unload()

    async def _unload(self) -> None:
        if self.state == "DISPOSED":
            return
        self.state = "UNLOADING"
        try:
            await self.effects.close()
        finally:
            self.state = "DISPOSED"


class Context:
    """Root or derived view over the tutorial logger."""

    filter = object()

    def __init__(self) -> None:
        self.root = self
        self._metadata: dict[object, object] = {}
        self.intercepts: dict[str, list[object]] = {}
        self.fiber: RootFiber | Fiber = RootFiber(self)
        self.logger: LoggerService | LoggerServiceView = LoggerService(self)

    @classmethod
    def derive(cls, parent: Context, fiber: Fiber | None = None) -> Context:
        context = object.__new__(cls)
        context.__dict__ = parent.__dict__.copy()
        context.root = parent.root
        context._metadata = dict(parent._metadata)
        context.intercepts = {
            name: list(values) for name, values in parent.intercepts.items()
        }
        context.fiber = fiber or parent.fiber
        context.logger = parent.root.logger.for_context(context)
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
        return Fiber(self, str(name), plugin, config).start()

    def exporter(self, exporter: Exporter) -> Effect:
        return self.logger.exporter(exporter)

    async def aclose(self) -> None:
        await self.root.fiber.dispose()
