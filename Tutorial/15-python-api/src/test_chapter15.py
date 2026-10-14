from __future__ import annotations

import asyncio
import inspect

import pytest
from chapter15 import (
    API_MATRIX,
    CHAPTER_TAGS,
    CORDIS_ROOT_EXPORTS,
    Context,
    Effect,
    EventOptions,
    FiberState,
    ServiceProxy,
    current_context,
)


@pytest.mark.asyncio
async def test_await_fiber_and_wait_settle_on_the_same_fiber() -> None:
    context = Context()
    started = asyncio.Event()
    release = asyncio.Event()

    async def plugin(plugin_context: Context, _config: object) -> None:
        started.set()
        await release.wait()

    fiber = context.plugin(plugin)
    waiter = asyncio.create_task(fiber.wait())
    await started.wait()
    assert not waiter.done()

    release.set()
    assert await waiter is fiber
    assert await fiber is fiber  # the `await fiber` spelling
    assert fiber.state is FiberState.ACTIVE

    documented = next(entry for entry in API_MATRIX if entry.concept == "Fiber.await()")
    assert documented.status == "language-specific"
    assert documented.python == "await fiber / fiber.wait()"
    await context.aclose()


@pytest.mark.asyncio
async def test_is_context_matches_the_context_is_contract() -> None:
    context = Context()
    child = context.extend()

    assert Context.is_context(context)
    assert Context.is_context(child)
    assert not Context.is_context(object())
    assert not hasattr(context, "is")  # `is` cannot be a Python method name

    documented = next(
        entry for entry in API_MATRIX if entry.concept == "Context.is(value)"
    )
    assert documented.status == "language-specific"
    assert documented.python == "Context.is_context(value)"
    await context.aclose()


@pytest.mark.asyncio
async def test_event_options_keep_global_key_while_hook_field_is_global_() -> None:
    context = Context()
    allowed = context.extend()
    blocked = context.extend()
    dispatch = context.extend({Context.filter: lambda owner: owner is allowed})
    seen: list[str] = []

    allowed.on("filtered", lambda: seen.append("allowed"))
    blocked.on("filtered", lambda: seen.append("blocked"))
    blocked.on("filtered", lambda: seen.append("global"), {"global": True})
    context.emit(dispatch, "filtered")

    assert seen == ["allowed", "global"]
    assert [hook.global_ for hook in context.events.hooks("filtered")] == [
        False,
        False,
        True,
    ]
    assert set(EventOptions.__annotations__) == {"prepend", "global"}
    await context.aclose()


@pytest.mark.asyncio
async def test_effect_is_awaitable_and_callable_and_disposes_once() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    cleaned: list[str] = []

    async def setup() -> object:
        started.set()
        await release.wait()
        return lambda: cleaned.append("cleanup")

    effect = Effect("thenable")
    start_task = asyncio.create_task(effect.start(setup))
    await started.wait()
    assert inspect.isawaitable(effect)

    release.set()
    await start_task
    assert await effect is effect

    first = effect()
    assert isinstance(first, asyncio.Task)
    await first
    assert cleaned == ["cleanup"]
    assert effect() is first
    assert cleaned == ["cleanup"]

    documented = next(
        entry for entry in API_MATRIX if entry.concept == "thenable disposer"
    )
    assert documented.status == "equivalent"


@pytest.mark.asyncio
async def test_parallel_aggregates_failures_like_aggregate_error() -> None:
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

    context.on("job", fail)
    context.on("job", slow)

    task = asyncio.create_task(context.parallel("job"))
    await started.wait()
    assert not task.done()
    release.set()

    with pytest.raises(BaseExceptionGroup) as caught:
        await task
    assert finished == ["slow"]
    assert [str(error) for error in caught.value.exceptions] == ["boom"]

    documented = next(
        entry for entry in API_MATRIX if entry.concept == "AggregateError"
    )
    assert documented.python == "BaseExceptionGroup"
    await context.aclose()


@pytest.mark.asyncio
async def test_service_proxy_forwards_members_and_binds_the_caller_context() -> None:
    context = Context()
    consumer = context.extend({"marker": "consumer"})

    class Counter:
        def __init__(self) -> None:
            self.value = 1

        def caller(self) -> Context | None:
            return current_context()

    target = Counter()
    proxy = ServiceProxy(target, consumer)

    assert proxy is not target
    assert not isinstance(target, ServiceProxy)  # no prototype was modified
    assert proxy.value == 1
    proxy.value = 2
    assert target.value == 2
    assert proxy.caller() is consumer
    assert current_context() is None

    documented = next(entry for entry in API_MATRIX if entry.concept == "Proxy")
    assert documented.status == "equivalent"
    await context.aclose()


@pytest.mark.asyncio
async def test_context_filter_sentinel_is_identity_based() -> None:
    context = Context()
    first = context.extend({Context.filter: lambda owner: True})
    second = context.extend({Context.filter: lambda owner: False})

    assert context.metadata(Context.filter) is None
    assert first.metadata(Context.filter) is not second.metadata(Context.filter)
    assert Context.filter is Context.filter
    assert type(Context.filter) is object  # opaque sentinel, not a Symbol

    documented = next(
        entry for entry in API_MATRIX if entry.concept == "Symbol tracking"
    )
    assert documented.status == "language-specific"
    await context.aclose()


@pytest.mark.asyncio
async def test_extend_inherits_metadata_without_mutating_the_parent() -> None:
    key = object()
    root = Context()
    parent = root.extend({key: "parent"})
    child = parent.extend({key: "child", "marker": "child"})

    assert root.metadata(key) is None
    assert parent.metadata(key) == "parent"
    assert child.metadata(key) == "child"
    assert not hasattr(parent, "marker")
    assert child.marker == "child"  # type: ignore[attr-defined]
    assert child.root is root
    assert Context.is_context(child)

    documented = next(
        entry for entry in API_MATRIX if entry.concept == "prototype inheritance"
    )
    assert documented.status == "equivalent"
    await root.aclose()


def test_public_surface_and_matrix_are_alias_free() -> None:
    import chapter15

    assert set(chapter15.__all__) == set(chapter15.__all__)  # names are unique
    for name in chapter15.__all__:
        assert hasattr(chapter15, name), name

    # Teaching-only aliases must not survive into the API chapter.
    for alias in ("await_fiber", "is_context", "AggregateError", "global_", "is_"):
        assert not hasattr(chapter15, alias), alias

    concepts = [entry.concept for entry in API_MATRIX]
    assert len(concepts) == len(set(concepts))
    assert {entry.status for entry in API_MATRIX} <= {
        "exact",
        "equivalent",
        "language-specific",
        "extension",
    }
    assert {
        "Context.is(value)",
        "Fiber.await()",
        "EventOptions { global }",
        "Proxy",
        "Symbol tracking",
        "prototype inheritance",
        "thenable disposer",
        "AggregateError",
    } <= set(concepts)
    assert all(
        entry.chapter in CHAPTER_TAGS or entry.chapter in {"later", "extension"}
        for entry in API_MATRIX
    )

    assert len(CORDIS_ROOT_EXPORTS) == 41
    assert all(
        tag in CHAPTER_TAGS or tag in {"later", "extension"}
        for tag in CORDIS_ROOT_EXPORTS.values()
    )
    assert {"Context", "Fiber", "Effect", "Service", "Message"} <= set(
        CORDIS_ROOT_EXPORTS
    )


@pytest.mark.asyncio
async def test_failed_activation_surfaces_through_wait_and_await() -> None:
    context = Context()

    def plugin(_plugin_context: Context, _config: object) -> None:
        raise ValueError("activation failed")

    fiber = context.plugin(plugin)
    with pytest.raises(ValueError, match="activation failed"):
        await fiber
    assert fiber.state is FiberState.FAILED

    with pytest.raises(ValueError, match="activation failed"):
        await fiber.wait()
    await context.aclose()
