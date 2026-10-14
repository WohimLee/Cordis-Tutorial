from collections.abc import Callable

import pytest

from cordis import Context, Fiber, FiberState


@pytest.mark.asyncio
async def test_plugin_status_and_dispatch_events_are_observable() -> None:
    context = Context()
    plugins: list[int | None] = []
    statuses: list[tuple[FiberState, FiberState]] = []
    dispatches: list[tuple[object, ...]] = []

    def observe_plugin(fiber: Fiber) -> None:
        plugins.append(fiber.uid)

    def observe_status(fiber: Fiber, old: FiberState) -> None:
        statuses.append((old, fiber.state))

    def observe_dispatch(
        mode: object,
        name: object,
        args: tuple[object, ...],
        dispatch_context: Context | None,
    ) -> None:
        dispatches.append((mode, name, args, dispatch_context))

    context.on("internal/plugin", observe_plugin)
    context.on("internal/status", observe_status)
    context.on("internal/dispatch", observe_dispatch)

    def plugin(plugin_context: Context, config: object) -> None:
        plugin_context.on("public", lambda: None)

    fiber = context.plugin(plugin)
    uid = fiber.uid
    await fiber.wait()
    context.emit("public")
    await fiber.dispose()

    assert plugins == [uid, None]
    assert fiber.uid is None
    assert (FiberState.PENDING, FiberState.LOADING) in statuses
    assert (FiberState.LOADING, FiberState.ACTIVE) in statuses
    assert dispatches == [("emit", "public", (), None)]
    await context.aclose()


@pytest.mark.asyncio
async def test_internal_observer_error_does_not_break_disposal() -> None:
    context = Context()

    def broken(fiber: object) -> None:
        raise RuntimeError("observer failed")

    context.on("internal/plugin", broken)

    def plugin(plugin_context: Context, config: object) -> None:
        return None

    fiber = context.plugin(plugin)
    await fiber.wait()
    await fiber.dispose()
    assert fiber.state is FiberState.DISPOSED
    await context.aclose()


@pytest.mark.asyncio
async def test_internal_get_and_set_are_synchronous_waterfalls() -> None:
    context = Context()

    def provider(plugin_context: Context, config: object) -> None:
        plugin_context.provide("value", 1)

    fiber = context.plugin(provider)
    await fiber.wait()

    def override_get(
        receiver: object,
        name: object,
        error: BaseException,
        next_: Callable[[], object],
    ) -> object:
        assert str(error) == 'cannot get property "value" without inject'
        if name == "value":
            return 10
        return next_()

    context.on("internal/get", override_get)
    assert context.value == 10
    await context.aclose()
