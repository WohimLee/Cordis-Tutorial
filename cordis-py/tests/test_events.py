import asyncio
from collections.abc import Awaitable, Callable

import pytest

from cordis.context import Context


@pytest.mark.asyncio
async def test_event_dispatch_modes() -> None:
    context = Context()
    emitted: list[str] = []

    def receive(value: str) -> None:
        emitted.append(value)

    context.on("emit", receive)
    context.emit("emit", "seen")
    assert emitted == ["seen"]

    context.on("decision", lambda: False)
    context.on("decision", lambda: 0)
    context.on("decision", lambda: "late")
    assert context.bail("decision") == 0

    serial_order: list[int] = []

    async def first() -> None:
        await asyncio.sleep(0)
        serial_order.append(1)

    async def second() -> str:
        serial_order.append(2)
        return "done"

    context.on("serial", first)
    context.on("serial", second)
    assert await context.serial("serial") == "done"
    assert serial_order == [1, 2]


@pytest.mark.asyncio
async def test_parallel_aggregates_errors_after_all_listeners_finish() -> None:
    context = Context()
    completed: list[bool] = []

    async def fail() -> None:
        raise ValueError("failed")

    async def finish() -> None:
        await asyncio.sleep(0)
        completed.append(True)

    context.on("parallel", fail)
    context.on("parallel", finish)

    with pytest.raises(BaseExceptionGroup) as caught:
        await context.parallel("parallel")

    assert completed == [True]
    assert len(caught.value.exceptions) == 1


@pytest.mark.asyncio
async def test_waterfall_wraps_and_can_short_circuit() -> None:
    context = Context()

    async def outer(value: str, next_: Callable[[], Awaitable[object]]) -> str:
        result = await next_()
        return f"outer({result})"

    async def inner(value: str, next_: Callable[[], Awaitable[object]]) -> str:
        result = await next_()
        return f"inner({result})"

    context.on("flow", outer)
    context.on("flow", inner)
    result = await context.waterfall("flow", "value", next_=lambda: "default")
    assert result == "outer(inner(default))"

    def short(value: str, next_: Callable[[], Awaitable[object]]) -> str:
        return "cached"

    context.on("short", short)
    reached_default = False

    def default() -> str:
        nonlocal reached_default
        reached_default = True
        return "default"

    assert await context.waterfall("short", "value", next_=default) == "cached"
    assert reached_default is False


@pytest.mark.asyncio
async def test_listener_is_removed_when_plugin_unloads() -> None:
    context = Context()
    seen: list[str] = []

    def plugin(plugin_context: Context, config: object) -> None:
        plugin_context.on("owned", lambda: seen.append("called"))

    fiber = context.plugin(plugin)
    await fiber.wait()
    context.emit("owned")
    await fiber.dispose()
    context.emit("owned")

    assert seen == ["called"]
    await context.aclose()


@pytest.mark.asyncio
async def test_once_listener_runs_only_once_for_immediate_emits() -> None:
    context = Context()
    seen: list[bool] = []
    context.once("once", lambda: seen.append(True))

    context.emit("once")
    context.emit("once")
    await asyncio.sleep(0)

    assert seen == [True]
    await context.aclose()


@pytest.mark.asyncio
async def test_listener_disposers_are_idempotent_and_remove_the_hook() -> None:
    context = Context()
    called: list[bool] = []
    dispose = context.on("event", lambda: called.append(True))
    dispose_once = context.once("once", lambda: None)

    assert dispose() is None
    assert dispose() is None
    context.emit("event")
    context.emit("once")
    assert dispose_once() is None
    assert called == []
    await context.aclose()


@pytest.mark.asyncio
async def test_internal_listener_can_replace_normal_registration() -> None:
    context = Context()
    intercepted: list[object] = []
    replacement_active = True

    def intercept(
        name: object,
        _listener: Callable[..., object],
        options: dict[str, bool],
    ) -> object:
        if name != "special":
            return None
        intercepted.append(options)

        def dispose() -> bool:
            nonlocal replacement_active
            previous, replacement_active = replacement_active, False
            return previous

        return dispose

    context.on("internal/listener", intercept)
    called: list[bool] = []
    dispose = context.on("special", lambda: called.append(True), True)
    context.emit("special")

    assert called == []
    assert intercepted == [{"prepend": True, "global": False}]
    assert dispose() is True
    assert dispose() is False
    await context.aclose()


@pytest.mark.asyncio
async def test_emit_schedules_awaitable_listener_and_supports_object_event_keys() -> None:
    context = Context()
    event = object()
    completed = asyncio.Event()

    async def listener() -> None:
        await asyncio.sleep(0)
        completed.set()

    context.on(event, listener)
    context.emit(event)
    assert not completed.is_set()
    await completed.wait()
    await context.aclose()


@pytest.mark.asyncio
async def test_dispatch_context_filters_local_hooks_but_not_global_hooks() -> None:
    context = Context()
    allowed = context.extend()
    blocked = context.extend()

    def filter_owner(owner: Context) -> bool:
        return owner is allowed

    dispatch_context = context.extend({Context.filter: filter_owner})
    seen: list[str] = []

    allowed.on("filtered", lambda: seen.append("allowed"))
    blocked.on("filtered", lambda: seen.append("blocked"))
    blocked.on("filtered", lambda: seen.append("global"), {"global": True})
    context.emit(dispatch_context, "filtered")

    assert seen == ["allowed", "global"]
    await context.aclose()
