from collections.abc import Callable

import pytest

from cordis import Context, FiberState, ValidationError


class PositiveConfig:
    @staticmethod
    def validate(value: object) -> int:
        if not isinstance(value, int) or value <= 0:
            raise ValueError("expected a positive integer")
        return value


@pytest.mark.asyncio
async def test_config_is_validated_and_restart_replaces_activation() -> None:
    context = Context()
    activations: list[int] = []
    cleanups: list[int] = []

    def plugin(plugin_context: Context, config: object) -> object:
        assert isinstance(config, int)
        activations.append(config)
        return lambda: cleanups.append(config)

    plugin.Config = PositiveConfig  # type: ignore[attr-defined]
    fiber = context.plugin(plugin, 1)
    await fiber.wait()
    assert fiber.config == 1

    await fiber.restart()
    assert activations == [1, 1]
    assert cleanups == [1]

    await fiber.update(2)
    assert fiber.config == 2
    assert activations == [1, 1, 2]
    assert cleanups == [1, 1]
    await context.aclose()


@pytest.mark.asyncio
async def test_invalid_update_preserves_active_configuration() -> None:
    context = Context()

    def plugin(plugin_context: Context, config: object) -> None:
        return None

    plugin.Config = PositiveConfig  # type: ignore[attr-defined]
    fiber = context.plugin(plugin, 1)
    await fiber.wait()

    with pytest.raises(ValidationError):
        await fiber.update(0)

    assert fiber.config == 1
    assert vars(fiber)["_config"] == 0
    assert fiber.state is FiberState.ACTIVE
    await context.aclose()


@pytest.mark.asyncio
async def test_pending_update_defers_validation_until_dependencies_exist() -> None:
    context = Context()

    def plugin(plugin_context: Context, config: object) -> None:
        return None

    plugin.Config = PositiveConfig  # type: ignore[attr-defined]
    plugin.inject = ["ready"]  # type: ignore[attr-defined]
    fiber = context.plugin(plugin, 1)
    await fiber.wait()

    assert await fiber.update(0) is None
    assert vars(fiber)["_config"] == 0

    def provider(plugin_context: Context, config: object) -> None:
        plugin_context.provide("ready", True)

    dependency = context.plugin(provider)
    await dependency.wait()
    with pytest.raises(ValidationError):
        await fiber.wait()
    assert fiber.state.value == "FAILED"
    await context.aclose()


@pytest.mark.asyncio
async def test_update_waterfall_can_veto_restart() -> None:
    context = Context()
    activations: list[int] = []

    def plugin(plugin_context: Context, config: object) -> None:
        assert isinstance(config, int)
        activations.append(config)

    async def veto(
        config: object,
        no_save: object,
        next_: Callable[[], object],
    ) -> str:
        return "vetoed"

    context.on("internal/update", veto)
    fiber = context.plugin(plugin, 1)
    await fiber.wait()
    assert await fiber.update(2) == "vetoed"
    assert activations == [1]
    assert fiber.config == 1
    await context.aclose()
