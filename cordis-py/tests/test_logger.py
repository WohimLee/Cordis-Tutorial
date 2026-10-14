import pytest

from cordis import Context, Logger, LoggerLevel, LoggerService
from cordis_observer import MemoryExporter


@pytest.mark.asyncio
async def test_logger_levels_and_exporter_ownership() -> None:
    context = Context()
    exporter = MemoryExporter()

    def plugin(plugin_context: Context, config: object) -> None:
        plugin_context.exporter(exporter)

    fiber = context.plugin(plugin)
    await fiber.wait()
    exporter.levels["agent.loop"] = LoggerLevel.ERROR
    log = context.logger("agent.loop")
    log.info("ignored %s", "message")
    log.error("failed %s", "request")

    assert exporter.texts == ["failed request"]
    await fiber.dispose()
    log.error("after disposal")
    assert len(exporter.messages) == 1
    await context.aclose()


@pytest.mark.asyncio
async def test_logger_options_meta_and_default_name() -> None:
    context = Context()
    exporter = MemoryExporter()
    exporter.levels["default"] = LoggerLevel.DEBUG
    effect = context.exporter(exporter)
    assert isinstance(context.logger, LoggerService)
    logger = Logger(
        {
            "name": "original",
            "level": LoggerLevel.DEBUG,
            "meta": {"name": "override", "type": "custom", "level": 0, "tag": "value"},
        },
        context.logger,
    )
    logger.debug("message")

    def CamelCase(plugin_context: Context, _config: object) -> None:
        plugin_context.logger().info("plugin")

    fiber = context.plugin(CamelCase)
    await fiber.wait()
    first, second = exporter.messages
    assert (first.name, first.type, first.level, first.__dict__["tag"]) == (
        "override",
        "custom",
        0,
        "value",
    )
    assert second.name == "camel-case"
    await effect.dispose()
    await context.aclose()
