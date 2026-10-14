from __future__ import annotations

import pytest
from chapter13 import (
    Context,
    CordisError,
    CordisErrorCode,
    FiberState,
    Service,
    ServiceView,
)


class Counter(Service):
    provide = "counter"

    def __init__(self, context: Context, config: object = None) -> None:
        self.value = 0
        super().__init__(context)

    def next(self) -> int:
        self.value += 1
        return self.value


@pytest.mark.asyncio
async def test_provide_registers_service_and_disposal_removes_it() -> None:
    context = Context()
    fiber = context.plugin(Counter)
    await fiber

    service = context.counter
    assert isinstance(service, ServiceView)
    assert service.next() == 1
    assert service.next() == 2
    assert [impl.name for impl in context.reflect.implementations()] == ["counter"]

    await fiber.dispose()
    assert context.get("counter") is None
    assert context.reflect.implementations() == ()
    assert context.reflect.dependents("counter") == ()
    await context.aclose()


@pytest.mark.asyncio
async def test_duplicate_provider_and_accessor_conflicts_raise() -> None:
    context = Context()

    def first(plugin_context: Context, _config: object) -> None:
        plugin_context.provide("value", 1)

    def second(plugin_context: Context, _config: object) -> None:
        plugin_context.provide("value", 2)

    await context.plugin(first)
    duplicate = context.plugin(second)
    with pytest.raises(CordisError) as caught:
        await duplicate

    assert caught.value.code is CordisErrorCode.DUPLICATE_SERVICE
    assert duplicate.state is FiberState.FAILED
    assert context.get("value") == 1

    consumer = context.extend()
    consumer.accessor("computed", {"get": lambda receiver: 1})
    with pytest.raises(TypeError, match="already declared"):
        consumer.accessor("computed", {"get": lambda receiver: 1})

    other = Context()
    other.accessor("mixed", {"get": lambda receiver: 1})
    with pytest.raises(TypeError, match="already an accessor"):
        other.provide("mixed", 2)
    await context.aclose()
    await other.aclose()


@pytest.mark.asyncio
async def test_service_init_cleanup_is_owned_by_the_provider_fiber() -> None:
    trace: list[str] = []

    class WithInit(Service):
        provide = "resource"

        def __init__(self, context: Context, config: object = None) -> None:
            super().__init__(context)

        def init(self) -> object:
            trace.append("init")
            return lambda: trace.append("cleanup")

    context = Context()
    fiber = context.plugin(WithInit)
    await fiber
    assert trace == ["init"]

    await fiber.dispose()
    await fiber.dispose()
    assert trace == ["init", "cleanup"]
    await context.aclose()


@pytest.mark.asyncio
async def test_caller_context_tracks_the_resolving_context() -> None:
    context = Context()

    class Recorder(Service):
        provide = "recorder"

        def __init__(self, plugin_context: Context, config: object = None) -> None:
            super().__init__(plugin_context)

        def where(self) -> Context:
            return self.caller_context

    provider = context.plugin(Recorder)
    await provider

    consumer = context.extend({"marker": "consumer"})
    service = consumer.recorder
    assert service.where() is consumer
    assert service.caller_context is provider.context
    await context.aclose()


@pytest.mark.asyncio
async def test_callable_service_returns_its_caller_context() -> None:
    context = Context()

    class Callable(Service):
        provide = "callable"

        def __init__(self, plugin_context: Context, config: object = None) -> None:
            super().__init__(plugin_context)

        def __call__(self) -> Context:
            return self.caller_context

    provider = context.plugin(Callable)
    await provider

    consumer = context.extend({"marker": "consumer"})
    assert consumer.callable() is consumer
    await context.aclose()


@pytest.mark.asyncio
async def test_extend_creates_an_unregistered_derived_service() -> None:
    context = Context()

    class Base(Service):
        provide = "base"

        def __init__(self, plugin_context: Context, config: object = None) -> None:
            self.state = {"count": 1}
            super().__init__(plugin_context)

    provider = context.plugin(Base)
    await provider
    service = context.reflect.get("base")
    derived = service.extend(extra=True)  # type: ignore[union-attr]

    assert derived is not service
    assert derived.extra is True  # type: ignore[attr-defined]
    assert derived.state is service.state  # type: ignore[union-attr]
    assert len(context.reflect.implementations()) == 1
    assert context.reflect.get("base") is service
    await context.aclose()


@pytest.mark.asyncio
async def test_availability_gates_strict_lookup_and_reloads_the_consumer() -> None:
    context = Context()

    class Switchable(Service):
        provide = "switchable"

        def __init__(self, plugin_context: Context, config: object = None) -> None:
            self.enabled = True
            super().__init__(plugin_context)

        def available(self) -> bool:
            return self.enabled

    provider = context.plugin(Switchable)
    await provider
    service = context.reflect.get("switchable")
    activations: list[bool] = []

    def consumer(plugin_context: Context, _config: object) -> None:
        activations.append(plugin_context.get("switchable") is not None)

    consumer.inject = ["switchable"]  # type: ignore[attr-defined]
    fiber = context.plugin(consumer)
    await fiber
    assert fiber.state is FiberState.ACTIVE
    assert activations == [True]

    isolated = context.isolate("switchable")

    def isolated_consumer(_plugin_context: Context, _config: object) -> None:
        activations.append(False)

    isolated_consumer.inject = ["switchable"]  # type: ignore[attr-defined]
    isolated_fiber = isolated.plugin(isolated_consumer)
    await isolated_fiber
    assert isolated_fiber.state is FiberState.PENDING
    assert activations == [True]

    service.enabled = False  # type: ignore[union-attr]
    await context.reflect.notify("switchable")
    assert context.reflect.get("switchable") is None
    assert fiber.state is FiberState.PENDING
    assert isolated_fiber.state is FiberState.PENDING

    service.enabled = True  # type: ignore[union-attr]
    await context.reflect.notify("switchable")
    assert fiber.state is FiberState.ACTIVE
    assert activations == [True, True]
    await context.aclose()


@pytest.mark.asyncio
async def test_accessor_read_write_read_only_and_disposal() -> None:
    context = Context()
    state = {"value": 1}

    def plugin(plugin_context: Context, _config: object) -> None:
        def set_value(_receiver: Context, value: object) -> None:
            assert isinstance(value, int)
            state["value"] = value

        plugin_context.accessor(
            "computed",
            {"get": lambda receiver: state["value"], "set": set_value},
        )
        plugin_context.accessor("readonly", {"get": lambda receiver: "fixed"})

    fiber = context.plugin(plugin)
    await fiber

    assert context.computed == 1
    assert context.set("computed", 3) is True
    assert context.computed == 3
    assert context.readonly == "fixed"
    with pytest.raises(AttributeError, match="read-only"):
        context.set("readonly", "changed")

    await fiber.dispose()
    assert context.get("computed") is None
    assert context.get("readonly") is None
    assert context.reflect.accessor_record("computed") is None
    await context.aclose()


@pytest.mark.asyncio
async def test_mixin_forwards_members_and_rejects_conflicts() -> None:
    context = Context()

    class Source:
        def __init__(self) -> None:
            self.count = 2

    def plugin(plugin_context: Context, _config: object) -> None:
        plugin_context.provide("source", Source())
        plugin_context.mixin("source", ["count"])

    fiber = context.plugin(plugin)
    await fiber

    assert context.count == 2
    assert context.set("count", 4) is True
    assert context.get("source").count == 4  # type: ignore[union-attr]

    other = context.extend()
    with pytest.raises(TypeError, match="already declared"):
        other.mixin("source", ["count"])

    await fiber.dispose()
    assert context.get("count") is None
    await context.aclose()


@pytest.mark.asyncio
async def test_custom_config_merge_and_intercept_precedence() -> None:
    context = Context()

    class MergeConfig:
        @staticmethod
        def merge(*configs: object) -> dict[str, object]:
            return {"count": len(configs)}

    class Configurable(Service):
        provide = "configurable"
        Config = MergeConfig

        def __init__(self, plugin_context: Context, config: object = None) -> None:
            super().__init__(plugin_context)

    provider = context.plugin(Configurable)
    await provider
    seen: list[dict[str, object]] = []

    def consumer(plugin_context: Context, _config: object) -> None:
        service = plugin_context.configurable
        seen.append(service.resolve_config({"timeout": 10}, {"retries": 2}))

    consumer.inject = {"configurable": {"timeout": 3}}  # type: ignore[attr-defined]
    fiber = context.plugin(consumer)
    await fiber

    assert seen == [{"count": 3}]

    bad = context.extend()
    bad.intercepts.setdefault("configurable", []).append("not-a-mapping")
    with pytest.raises(TypeError, match="must be a mapping"):
        bad.configurable.resolve_config()
    await context.aclose()
