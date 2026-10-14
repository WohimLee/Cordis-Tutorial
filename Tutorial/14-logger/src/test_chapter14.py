from __future__ import annotations

import gc

import pytest
from chapter14 import (
    Context,
    Logger,
    LoggerLevel,
    LoggerService,
    Message,
)


class MemoryExporter:
    """Tutorial-local test helper, mirroring ``cordis_observer.MemoryExporter``.

    It is deliberately defined here instead of in ``chapter14`` because the real
    in-memory collector is *not* part of Cordis Core.
    """

    colors = False
    maxLength = 10240

    def __init__(self) -> None:
        self.levels: dict[str, int] = {}
        self.formatters: dict[str, object] = {}
        self.messages: list[Message] = []

    def export(self, message: Message) -> None:
        self.messages.append(message)

    @property
    def texts(self) -> list[str]:
        return [Logger.format(self, message) for message in self.messages]


@pytest.mark.asyncio
async def test_level_thresholds_skip_messages_before_formatting() -> None:
    context = Context()
    exporter = MemoryExporter()
    formatted: list[str] = []

    def record(value: object, _exporter: object, _message: object) -> str:
        formatted.append(str(value))
        return str(value)

    exporter.formatters["s"] = record
    exporter.levels["agent.loop"] = LoggerLevel.ERROR
    exporter.levels["default"] = LoggerLevel.WARN
    context.exporter(exporter)

    named = context.logger("agent.loop")
    named.info("ignored %s", "early")
    named.error("kept %s", "late")

    default = context.logger("other")
    default.debug("dropped")
    default.warn("kept %s", "value")

    # Filtered messages are never exported, so formatting them is never attempted.
    assert [(m.name, m.type, m.level) for m in exporter.messages] == [
        ("agent.loop", "error", LoggerLevel.ERROR),
        ("other", "warn", LoggerLevel.WARN),
    ]
    assert exporter.texts == ["kept late", "kept value"]
    assert formatted == ["late", "value"]
    assert (
        LoggerLevel.ERROR,
        LoggerLevel.INFO,
        LoggerLevel.WARN,
        LoggerLevel.DEBUG,
    ) == (0, 1, 2, 3)
    await context.aclose()


@pytest.mark.asyncio
async def test_message_fields_serials_and_fiber_association() -> None:
    context = Context()
    exporter = MemoryExporter()
    context.exporter(exporter)

    seen: list[Message] = []

    def plugin(plugin_context: Context, _config: object) -> None:
        plugin_context.logger().info("hello %s", "world")
        seen.append(exporter.messages[-1])

    fiber = context.plugin(plugin)
    await fiber
    message = seen[0]

    assert message.sn == 1
    assert isinstance(message.ts, int)
    assert (message.name, message.type, message.level) == (
        "plugin",
        "info",
        LoggerLevel.INFO,
    )
    assert message.args == ("hello %s", "world")
    assert message.fiber is not None
    assert message.fiber() is fiber
    assert Logger.format(exporter, message) == "hello world"
    await context.aclose()


@pytest.mark.asyncio
async def test_formatters_placeholders_leftovers_and_truncation() -> None:
    context = Context()
    exporter = MemoryExporter()
    context.exporter(exporter)
    log = context.logger("format")

    log.info("%s-%d-%i-%f-%o-%O-%%", "a", "2.9", 3.9, 4.5, {"k": 1}, [1, 2])
    log.info({"structured": True})
    log.info("left", 1, {"a": 1}, [2])  # noqa: PLE1205 - tutorial Logger, not stdlib logging

    assert exporter.texts == [
        'a-2-3-4.5-{"k":1}-[1,2]-%',
        '{"structured":true}',
        'left 1 {"a":1} [2]',
    ]

    exporter.maxLength = 3
    log.info("abcdef\nok")
    assert exporter.texts[-1] == "abc...\nok"
    await context.aclose()


@pytest.mark.asyncio
async def test_exception_argument_expansion_uses_cause_and_groups() -> None:
    context = Context()
    exporter = MemoryExporter()
    context.exporter(exporter)
    log = context.logger("errors")

    log.error(ValueError("simple"))
    cause = ValueError("cause")
    wrapper = RuntimeError("wrapper")
    wrapper.__cause__ = cause
    log.error(wrapper)
    log.error(BaseExceptionGroup("group", [ValueError("one"), ValueError("two")]))

    # A cause is logged first and the original error still follows it, which is
    # the observable Cordis contract.
    assert exporter.texts == ["simple", "cause", "wrapper", "one", "two"]
    assert [message.type for message in exporter.messages] == ["error"] * 5
    await context.aclose()


@pytest.mark.asyncio
async def test_buffer_is_bounded_and_configured_by_the_service() -> None:
    context = Context()
    assert isinstance(context.logger, LoggerService)
    log = context.logger("buffer")
    log.info("one")
    log.info("two")
    log.info("three")
    assert [message.args[0] for message in context.logger.buffer] == [
        "one",
        "two",
        "three",
    ]

    context.logger.bufferSize = 2
    log.info("four")
    assert [message.args[0] for message in context.logger.buffer] == ["three", "four"]

    context.logger.bufferSize = 0
    log.info("five")
    assert context.logger.buffer == []
    await context.aclose()


@pytest.mark.asyncio
async def test_exporter_lifecycle_is_owned_by_the_registering_fiber() -> None:
    context = Context()
    exporter = MemoryExporter()

    def plugin(plugin_context: Context, _config: object) -> None:
        plugin_context.exporter(exporter)

    fiber = context.plugin(plugin)
    await fiber
    context.logger("app").info("before")
    assert len(exporter.messages) == 1

    await fiber.dispose()
    context.logger("app").info("after")
    assert len(exporter.messages) == 1
    assert len(context.logger.exporters) == 1  # only the internal buffer remains
    await context.aclose()


@pytest.mark.asyncio
async def test_default_name_hyphenation_and_logger_intercept() -> None:
    context = Context()
    exporter = MemoryExporter()
    exporter.levels["camel-case"] = LoggerLevel.DEBUG
    context.exporter(exporter)

    def CamelCase(plugin_context: Context, _config: object) -> None:
        plugin_context.logger().debug("default-name")

    fiber = context.plugin(CamelCase)
    await fiber
    assert exporter.messages[-1].name == "camel-case"

    intercepted = context.intercept(
        "logger", {"name": "renamed", "level": LoggerLevel.ERROR}
    )
    intercepted.logger().debug("dropped")
    intercepted.logger().error("kept")
    assert exporter.messages[-1].name == "renamed"
    assert exporter.messages[-1].type == "error"
    assert len(exporter.messages) == 2
    await context.aclose()


@pytest.mark.asyncio
async def test_logger_options_meta_override_order_is_preserved() -> None:
    context = Context()
    exporter = MemoryExporter()
    exporter.levels["default"] = LoggerLevel.DEBUG
    context.exporter(exporter)

    logger = Logger(
        {
            "name": "original",
            "level": LoggerLevel.DEBUG,
            "meta": {"name": "override", "type": "custom", "level": 0, "tag": "value"},
        },
        context.logger,
    )
    logger.debug("message")

    message = exporter.messages[0]
    assert (message.name, message.type, message.level) == ("override", "custom", 0)
    assert message.__dict__["tag"] == "value"
    await context.aclose()


@pytest.mark.asyncio
async def test_scheduler_keeps_fiber_reference_without_holding_it_alive() -> None:
    context = Context()
    exporter = MemoryExporter()
    context.exporter(exporter)
    seen: list[Message] = []

    def plugin(plugin_context: Context, _config: object) -> None:
        plugin_context.logger().info("scoped")
        seen.append(exporter.messages[-1])

    fiber = context.plugin(plugin)
    await fiber
    reference = seen[0].fiber
    assert reference is not None
    assert reference() is fiber

    await fiber.dispose()
    del fiber
    gc.collect()
    assert reference() is not None  # the parent EffectScope still owns the child

    await context.aclose()
    gc.collect()
    assert reference() is None


@pytest.mark.asyncio
async def test_failed_activation_releases_its_exporter() -> None:
    context = Context()
    exporter = MemoryExporter()

    def plugin(plugin_context: Context, _config: object) -> None:
        plugin_context.exporter(exporter)
        raise ValueError("activation failed")

    fiber = context.plugin(plugin)
    with pytest.raises(ValueError, match="activation failed"):
        await fiber

    assert len(context.logger.exporters) == 1  # only the internal buffer survived
    context.logger("app").info("after failure")
    assert exporter.messages == []
    await context.aclose()
