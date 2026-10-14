"""Cordis-compatible structured logging with lifecycle-owned exporters."""

from __future__ import annotations

import json
import re
import time
import weakref
from collections.abc import Callable, Mapping
from enum import IntEnum
from typing import (
    TYPE_CHECKING,
    ClassVar,
    Literal,
    NotRequired,
    Protocol,
    TypeAlias,
    TypedDict,
    cast,
)

from .effect import Effect

if TYPE_CHECKING:
    from .context import Context
    from .fiber import Fiber, RootFiber


class LoggerLevel(IntEnum):
    ERROR = 0
    INFO = 1
    WARN = 2
    DEBUG = 3


LoggerType: TypeAlias = Literal["error", "info", "warn", "debug"]
LoggerMethod: TypeAlias = Callable[..., None]


class LoggerOptions(TypedDict):
    name: str
    meta: NotRequired[Mapping[str, object]]
    level: NotRequired[int]


class Message:
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


def _color(value: object, exporter: Exporter, message: Message) -> str:
    return Logger.color(
        exporter, Logger.code(message.name, getattr(exporter, "colors", False)), value
    )


defaultFormatters: dict[str, Formatter] = {
    "s": _string,
    "d": _integer,
    "i": _integer,
    "f": _float,
    "o": _object,
    "O": _object,
    "c": _empty,
    "C": _color,
}


class Logger:
    def __init__(
        self,
        options: Mapping[str, object],
        service: LoggerService,
    ) -> None:
        self.service = service
        self.name = str(options["name"])
        level = options.get("level")
        self.level = level if isinstance(level, int) else None
        raw_meta = options.get("meta", {})
        self.meta: dict[str, object] = (
            {str(key): value for key, value in cast(Mapping[object, object], raw_meta).items()}
            if isinstance(raw_meta, Mapping)
            else {}
        )

    @staticmethod
    def code(name: str, level: int | bool = False) -> int | None:
        if not level:
            return None
        palette = c256 if level >= 2 else c16
        hash_ = 0
        for char in name:
            hash_ = ((hash_ << 3) - hash_) + ord(char) + 13
            hash_ = ((hash_ + 2**31) % 2**32) - 2**31
        return palette[abs(hash_) % len(palette)]

    @staticmethod
    def color(exporter: Exporter, code: int | None, value: object, decoration: str = "") -> str:
        colors = getattr(exporter, "colors", False)
        if not colors or code is None:
            return str(value)
        prefix = str(code) if code < 8 else f"8;5;{code}"
        suffix = decoration if int(colors) >= 2 else ""
        return f"\x1b[3{prefix}{suffix}m{value}\x1b[0m"

    @staticmethod
    def format(exporter: Exporter, message: Message) -> str:
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
            custom = getattr(exporter, "formatters", {}).get(char) or defaultFormatters.get(char)
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
            line[:maximum] + ("..." if len(line) > maximum else "") for line in output.splitlines()
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

        return context.effect(setup, "ctx.logger.exporter()")

    def exporter(self, exporter: Exporter) -> Effect:
        return self.exporter_for(self.ctx, exporter)

    def write(
        self,
        logger: Logger,
        type_: str,
        level: LoggerLevel,
        args: tuple[object, ...],
    ) -> None:
        if len(args) == 1 and isinstance(args[0], BaseException):
            if args[0].__cause__:
                logger.error(args[0].__cause__)
            elif isinstance(args[0], BaseExceptionGroup):
                for error in cast(BaseExceptionGroup[BaseException], args[0]).exceptions:
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
            known = {key: fields.pop(key) for key in ("sn", "ts", "name", "type", "level")}
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


c16 = [6, 2, 3, 4, 5, 1]
c256 = [
    20,
    21,
    26,
    27,
    32,
    33,
    38,
    39,
    40,
    41,
    42,
    43,
    44,
    45,
    56,
    57,
    62,
    63,
    68,
    69,
    74,
    75,
    76,
    77,
    78,
    79,
    80,
    81,
    92,
    93,
    98,
    99,
    112,
    113,
    129,
    134,
    135,
    148,
    149,
    160,
    161,
    162,
    163,
    164,
    165,
    166,
    167,
    168,
    169,
    170,
    171,
    172,
    173,
    178,
    179,
    184,
    185,
    196,
    197,
    198,
    199,
    200,
    201,
    202,
    203,
    204,
    205,
    206,
    207,
    208,
    209,
    214,
    215,
    220,
    221,
]
