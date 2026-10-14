import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import cast

import pytest

from cordis.effect import Cleanup, EffectScope, EffectSetup
from cordis.errors import CordisError, CordisErrorCode


@pytest.mark.asyncio
async def test_scope_cleans_effects_and_cleanups_in_reverse_order() -> None:
    order: list[str] = []
    scope = EffectScope()

    def first() -> Iterator[Cleanup]:
        yield lambda: order.append("first-a")
        yield lambda: order.append("first-b")

    async def second() -> AsyncIterator[Cleanup]:
        yield lambda: order.append("second-a")

        async def cleanup() -> None:
            await asyncio.sleep(0)
            order.append("second-b")

        yield cleanup

    await scope.install(first, "first")
    await scope.install(second, "second")
    await scope.close()
    await scope.close()

    assert order == ["second-b", "second-a", "first-b", "first-a"]


@pytest.mark.asyncio
async def test_invalid_generator_result_rolls_back_collected_cleanup() -> None:
    cleaned: list[bool] = []
    scope = EffectScope()

    def setup() -> Iterator[object]:
        yield lambda: cleaned.append(True)
        yield "invalid"

    with pytest.raises(TypeError, match="Invalid effect"):
        await scope.install(cast(EffectSetup, setup))

    assert cleaned == [True]
    assert scope.effects == ()


@pytest.mark.asyncio
async def test_cleanup_failures_are_aggregated_without_stopping_cleanup() -> None:
    cleaned: list[bool] = []
    scope = EffectScope()

    def fail() -> None:
        raise ValueError("failed")

    await scope.install(lambda: [lambda: cleaned.append(True), fail])

    with pytest.raises(BaseExceptionGroup) as caught:
        await scope.close()

    assert cleaned == [True]
    assert len(caught.value.exceptions) == 1


@pytest.mark.asyncio
async def test_closed_scope_rejects_new_effects() -> None:
    scope = EffectScope()
    await scope.close()

    with pytest.raises(CordisError) as caught:
        await scope.install(lambda: None)

    assert caught.value.code == CordisErrorCode.INACTIVE_EFFECT


@pytest.mark.asyncio
async def test_effect_is_an_awaitable_callable_single_shot_disposer() -> None:
    order: list[str] = []
    scope = EffectScope()
    effect = scope.install_sync(
        lambda: [lambda: order.append("first"), lambda: order.append("second")]
    )

    disposer = await effect
    assert disposer is effect
    first = effect()
    second = effect()
    assert first is second
    await first

    assert order == ["second", "first"]
    await scope.close()


@pytest.mark.asyncio
async def test_context_effect_accepts_async_setup_and_disposal_waits_for_it() -> None:
    from cordis import Context

    context = Context()
    setup_started = asyncio.Event()
    allow_setup = asyncio.Event()
    cleaned = asyncio.Event()

    async def setup() -> Cleanup:
        setup_started.set()
        await allow_setup.wait()
        return cleaned.set

    effect = context.effect(setup)
    await setup_started.wait()
    disposal = effect()
    assert not disposal.done()
    allow_setup.set()
    await disposal

    assert cleaned.is_set()
    await context.aclose()


@pytest.mark.asyncio
async def test_scope_close_waits_for_in_flight_async_setup_and_cleans_result() -> None:
    setup_started = asyncio.Event()
    allow_setup = asyncio.Event()
    cleaned = asyncio.Event()
    scope = EffectScope()

    async def setup() -> Cleanup:
        setup_started.set()
        await allow_setup.wait()
        return cleaned.set

    install_task = asyncio.create_task(scope.install(setup))
    await setup_started.wait()
    close_task = asyncio.create_task(scope.close())
    await asyncio.sleep(0)
    assert not close_task.done()

    allow_setup.set()
    with pytest.raises(CordisError) as caught:
        await install_task
    await close_task

    assert caught.value.code is CordisErrorCode.INACTIVE_EFFECT
    assert cleaned.is_set()


@pytest.mark.asyncio
async def test_nested_effects_form_a_diagnostic_tree() -> None:
    scope = EffectScope()
    cleaned: list[bool] = []

    async def outer() -> None:
        await scope.install(lambda: lambda: cleaned.append(True), "child")

    parent = await scope.install(outer, "parent")

    assert len(scope.effects) == 1
    assert scope.effects[0].label == "parent"
    assert [child.label for child in scope.effects[0].children] == ["child"]
    await parent.dispose()
    assert cleaned == [True]
    await scope.close()
