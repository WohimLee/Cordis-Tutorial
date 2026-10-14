import asyncio
from typing import cast

import pytest

from cordis import Context, Service
from cordis.fiber import FiberState


class ValueService(Service):
    provide = "value"

    def __init__(self, context: Context, config: object = None) -> None:
        self.value = config
        super().__init__(context)


@pytest.mark.asyncio
async def test_dependency_loss_while_loading_rolls_back_once() -> None:
    context = Context()
    provider = context.plugin(ValueService, "first")
    await provider.wait()
    started = asyncio.Event()
    release = asyncio.Event()
    cleanups = 0

    async def consumer(plugin_context: Context, config: object) -> object:
        nonlocal cleanups
        assert cast(ValueService, plugin_context.value).value == "first"
        started.set()
        await release.wait()

        def cleanup() -> None:
            nonlocal cleanups
            cleanups += 1

        return cleanup

    consumer.inject = ["value"]  # type: ignore[attr-defined]
    fiber = context.plugin(consumer)
    await started.wait()
    dispose_task = asyncio.create_task(provider.dispose())
    await asyncio.sleep(0)
    assert not dispose_task.done()
    release.set()
    await dispose_task
    await fiber.wait()

    assert fiber.state is FiberState.PENDING
    assert cleanups == 1
    await context.aclose()


@pytest.mark.asyncio
async def test_dependency_restoration_while_unloading_reactivates() -> None:
    context = Context()
    provider = context.plugin(ValueService, "first")
    await provider.wait()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    activations: list[object] = []

    def consumer(plugin_context: Context, config: object) -> object:
        activations.append(cast(ValueService, plugin_context.value).value)

        async def cleanup() -> None:
            cleanup_started.set()
            await release_cleanup.wait()

        return cleanup

    consumer.inject = ["value"]  # type: ignore[attr-defined]
    fiber = context.plugin(consumer)
    await fiber.wait()
    dispose_task = asyncio.create_task(provider.dispose())
    await cleanup_started.wait()

    replacement = context.plugin(ValueService, "second")
    await replacement.wait()
    release_cleanup.set()
    await dispose_task
    await fiber.wait()

    assert fiber.state is FiberState.ACTIVE
    assert activations == ["first", "second"]
    await context.aclose()


@pytest.mark.asyncio
async def test_root_close_waits_for_loading_plugin_and_cleans_it() -> None:
    context = Context()
    started = asyncio.Event()
    release = asyncio.Event()
    cleaned = asyncio.Event()

    async def plugin(plugin_context: Context, config: object) -> object:
        started.set()
        await release.wait()
        return cleaned.set

    fiber = context.plugin(plugin)
    await started.wait()
    close_task = asyncio.create_task(context.aclose())
    await asyncio.sleep(0)
    assert not close_task.done()
    release.set()
    await close_task

    assert cleaned.is_set()
    assert fiber.state is FiberState.DISPOSED


@pytest.mark.asyncio
async def test_cleanup_failure_still_removes_disposed_fiber() -> None:
    context = Context()
    cleaned = False

    def plugin(plugin_context: Context, config: object) -> object:
        def cleanup() -> None:
            nonlocal cleaned
            cleaned = True
            raise ValueError("cleanup failed")

        return cleanup

    fiber = context.plugin(plugin)
    await fiber.wait()

    with pytest.raises(BaseExceptionGroup):
        await fiber.dispose()

    assert cleaned is True
    assert fiber.uid is None
    assert fiber.state is FiberState.DISPOSED
    assert not context.registry.has(plugin)
    await context.aclose()
