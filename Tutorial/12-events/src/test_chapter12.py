from __future__ import annotations

import asyncio

import pytest
from chapter12 import Context, FiberState


@pytest.mark.asyncio
async def test_listener_is_owned_by_its_fiber_and_released_on_dispose_or_failure() -> (
    None
):
    context = Context()
    seen: list[str] = []

    def plugin(plugin_context: Context, _config: object) -> None:
        plugin_context.on("owned", lambda: seen.append("normal"))
        plugin_context.on("owned", lambda: seen.append("prepend"), True)

    fiber = context.plugin(plugin)
    await fiber
    context.emit("owned")
    assert seen == ["prepend", "normal"]
    assert fiber.get_effects() == ("ctx.on('owned')", "ctx.on('owned')")

    await fiber.dispose()
    context.emit("owned")
    assert seen == ["prepend", "normal"]
    assert fiber.state is FiberState.DISPOSED

    def broken(plugin_context: Context, _config: object) -> None:
        plugin_context.on("owned", lambda: seen.append("leaked"))
        raise ValueError("activation failed")

    failed = context.plugin(broken)
    with pytest.raises(ValueError, match="activation failed"):
        await failed

    assert failed.state is FiberState.FAILED
    assert failed.get_effects() == ()
    context.emit("owned")
    assert seen == ["prepend", "normal"]
    await context.aclose()


@pytest.mark.asyncio
async def test_once_runs_once_and_disposers_are_idempotent() -> None:
    context = Context()
    seen: list[str] = []
    dispose = context.on("event", lambda: seen.append("on"))
    dispose_once = context.once("event", lambda: seen.append("once"))

    context.emit("event")
    context.emit("event")
    assert seen == ["on", "once", "on"]

    assert dispose() is None
    assert dispose() is None
    assert dispose_once() is None
    context.emit("event")
    assert seen == ["on", "once", "on"]
    await context.aclose()


@pytest.mark.asyncio
async def test_emit_schedules_awaitable_listener_without_waiting() -> None:
    context = Context()
    entered = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def listener() -> None:
        entered.set()
        await release.wait()
        completed.set()

    context.on("async", listener)
    context.emit("async")
    assert not entered.is_set()

    await entered.wait()
    assert not completed.is_set()
    release.set()
    await completed.wait()
    await context.aclose()


@pytest.mark.asyncio
async def test_bail_continues_only_on_none_and_false() -> None:
    context = Context()
    order: list[str] = []

    def returns(value: object):  # type: ignore[no-untyped-def]
        def listener() -> object:
            order.append(str(value))
            return value

        return listener

    for value in (False, None, 0, "late"):
        context.on("decision", returns(value))

    assert context.bail("decision") == 0
    assert order == ["False", "None", "0"]

    context.on("quiet", returns(False))
    assert context.bail("quiet") is None
    await context.aclose()


@pytest.mark.asyncio
async def test_serial_awaits_in_order_and_stops_at_first_bail() -> None:
    context = Context()
    order: list[int] = []
    release = asyncio.Event()

    async def first() -> None:
        await release.wait()
        order.append(1)

    def second() -> str:
        order.append(2)
        return "stop"

    def third() -> str:
        order.append(3)
        return "unreachable"

    context.on("chain", first)
    context.on("chain", second)
    context.on("chain", third)

    task = asyncio.create_task(context.serial("chain"))
    assert not task.done()
    release.set()
    assert await task == "stop"
    assert order == [1, 2]
    await context.aclose()


@pytest.mark.asyncio
async def test_parallel_waits_for_every_listener_and_aggregates_failures() -> None:
    context = Context()
    started = asyncio.Event()
    release = asyncio.Event()
    finished: list[str] = []

    async def fail() -> None:
        raise ValueError("boom")

    async def slow() -> None:
        started.set()
        await release.wait()
        finished.append("slow")

    context.on("parallel", fail)
    context.on("parallel", slow)

    task = asyncio.create_task(context.parallel("parallel"))
    await started.wait()
    assert not task.done()
    release.set()

    with pytest.raises(BaseExceptionGroup) as caught:
        await task
    assert finished == ["slow"]
    assert [str(error) for error in caught.value.exceptions] == ["boom"]
    await context.aclose()


@pytest.mark.asyncio
async def test_waterfall_composes_around_and_can_short_circuit() -> None:
    context = Context()

    async def outer(_value: str, next_: object) -> str:
        result = await next_()  # type: ignore[operator]
        return f"outer({result})"

    async def inner(_value: str, next_: object) -> str:
        result = await next_()  # type: ignore[operator]
        return f"inner({result})"

    context.on("flow", outer)
    context.on("flow", inner)
    result = await context.waterfall("flow", "value", next_=lambda: "default")
    assert result == "outer(inner(default))"

    context.once("cut", lambda _value, _next: "cached")
    reached: list[bool] = []

    def fallback() -> str:
        reached.append(True)
        return "default"

    assert await context.waterfall("cut", "value", next_=fallback) == "cached"
    assert reached == []
    await context.aclose()


@pytest.mark.asyncio
async def test_dispatch_context_filters_local_hooks_but_not_global_hooks() -> None:
    context = Context()
    allowed = context.extend()
    blocked = context.extend()
    dispatch_context = context.extend({Context.filter: lambda owner: owner is allowed})
    seen: list[str] = []

    allowed.on("filtered", lambda: seen.append("allowed"))
    blocked.on("filtered", lambda: seen.append("blocked"))
    blocked.on("filtered", lambda: seen.append("global"), {"global": True})
    context.emit(dispatch_context, "filtered")
    assert seen == ["allowed", "global"]

    with pytest.raises(TypeError, match="dispatch Context"):
        context.emit(dispatch_context)
    await context.aclose()


@pytest.mark.asyncio
async def test_internal_listener_can_replace_registration() -> None:
    context = Context()
    options_seen: list[dict[str, bool]] = []
    active = True

    def intercept(
        name: object,
        _listener: object,
        options: dict[str, bool],
    ) -> object:
        if name != "special":
            return None
        options_seen.append(options)

        def dispose() -> bool:
            nonlocal active
            previous, active = active, False
            return previous

        return dispose

    context.on("internal/listener", intercept)
    called: list[bool] = []
    dispose = context.on("special", lambda: called.append(True), True)
    context.emit("special")

    assert called == []
    assert options_seen == [{"prepend": True, "global": False}]
    assert dispose() is True
    assert dispose() is False

    context.once("internal/listener", lambda _name, *_rest: "not-callable")
    with pytest.raises(TypeError, match="replacement must be callable"):
        context.on("other", lambda: None)
    await context.aclose()


@pytest.mark.asyncio
async def test_internal_dispatch_reports_public_modes_and_isolates_observers() -> None:
    context = Context()
    trace: list[tuple[str, object, tuple[object, ...], object]] = []

    def observe(
        mode: str,
        name: object,
        args: tuple[object, ...],
        dispatch_context: object,
    ) -> None:
        trace.append((mode, name, args, dispatch_context))
        raise RuntimeError("observer failed")

    context.on("internal/dispatch", observe)
    context.on("public", lambda: None)

    context.emit("public")
    await context.serial("public")
    context.emit("internal/silent")

    assert trace == [
        ("emit", "public", (), None),
        ("serial", "public", (), None),
    ]
    await context.aclose()
