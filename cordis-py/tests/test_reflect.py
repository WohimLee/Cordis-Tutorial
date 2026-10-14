from dataclasses import dataclass
from typing import cast

import pytest

from cordis import Context, Service
from cordis.fiber import FiberState


@pytest.mark.asyncio
async def test_accessor_and_mixin_are_owned_and_reversible() -> None:
    context = Context()
    state = {"value": 1}

    def set_value(receiver: Context, value: object) -> None:
        assert isinstance(value, int)
        state["value"] = value

    def plugin(plugin_context: Context, config: object) -> None:
        plugin_context.accessor(
            "computed",
            {"get": lambda receiver: state["value"], "set": set_value},
        )

        @dataclass
        class Source:
            count: int = 2

        plugin_context.provide("source", Source())
        plugin_context.mixin("source", ["count"])

    fiber = context.plugin(plugin)
    await fiber.wait()
    assert context.computed == 1
    context.set("computed", 3)
    assert context.computed == 3
    assert context.count == 2
    context.set("count", 4)
    assert context.count == 4

    await fiber.dispose()
    assert context.get("computed") is None
    assert context.get("count") is None
    await context.aclose()


@pytest.mark.asyncio
async def test_service_write_requires_owner_fiber() -> None:
    context = Context()

    def provider(plugin_context: Context, config: object) -> None:
        plugin_context.provide("value", 1)
        plugin_context.set("value", 2)

    fiber = context.plugin(provider)
    await fiber.wait()
    assert context.value == 2
    with pytest.raises(PermissionError):
        context.set("value", 3)
    await context.aclose()


@pytest.mark.asyncio
async def test_inject_intercept_is_visible_to_service() -> None:
    context = Context()

    class Configurable(Service):
        provide = "configurable"

        def __init__(self, plugin_context: Context, config: object = None) -> None:
            super().__init__(plugin_context)

    provider = context.plugin(Configurable)
    await provider.wait()
    seen: list[dict[str, object]] = []

    def consumer(plugin_context: Context, config: object) -> None:
        service = cast(Configurable, plugin_context.configurable)
        seen.append(service.resolve_config({"timeout": 10}, {"retries": 2}))

    consumer.inject = {"configurable": {"timeout": 3}}  # type: ignore[attr-defined]
    fiber = context.plugin(consumer)
    await fiber.wait()
    assert seen == [{"timeout": 3, "retries": 2}]
    await context.aclose()


@pytest.mark.asyncio
async def test_service_availability_refreshes_dependents() -> None:
    context = Context()

    class Switchable(Service):
        provide = "switchable"

        def __init__(self, plugin_context: Context, config: object = None) -> None:
            self.enabled = True
            super().__init__(plugin_context)

        def available(self) -> bool:
            return self.enabled

    provider = context.plugin(Switchable)
    await provider.wait()
    service = cast(Switchable, provider.context.reflect.get("switchable"))
    activations = 0

    def consumer(plugin_context: Context, config: object) -> None:
        nonlocal activations
        activations += 1

    consumer.inject = ["switchable"]  # type: ignore[attr-defined]
    fiber = context.plugin(consumer)
    await fiber.wait()
    assert fiber.state is FiberState.ACTIVE

    label = service.context.reflect.label(service.context, service.name)
    service.enabled = False
    await service.context.reflect.notify_label(service.name, label)
    assert fiber.state is FiberState.PENDING

    service.enabled = True
    await service.context.reflect.notify_label(service.name, label)
    assert fiber.state is FiberState.ACTIVE
    assert activations == 2
    await context.aclose()


@pytest.mark.asyncio
async def test_service_init_call_extend_and_custom_config_merge() -> None:
    context = Context()
    trace: list[str] = []

    class MergeConfig:
        @staticmethod
        def validate(value: object) -> object:
            return value

        @staticmethod
        def merge(*configs: object) -> dict[str, object]:
            return {"count": len(configs)}

    class CallableService(Service):
        provide = "callable"
        Config = MergeConfig

        def __call__(self) -> Context:
            return self.caller_context

        def init(self) -> object:
            trace.append("init")
            return lambda: trace.append("cleanup")

    provider = context.plugin(CallableService)
    await provider.wait()
    consumer = context.extend({"marker": "consumer"})
    service = cast(CallableService, consumer.callable)

    assert service() is consumer
    assert service.resolve_config({}, {"head": True}) == {"count": 2}
    derived = service.extend(extra=True)
    assert derived is not service
    assert derived.extra is True  # type: ignore[attr-defined]

    await provider.dispose()
    assert trace == ["init", "cleanup"]
    await context.aclose()


@pytest.mark.asyncio
async def test_accessor_after_missing_lookup_and_scope_filtered_service_event() -> None:
    context = Context()
    assert context.get("computed") is None
    context.accessor("computed", {"get": lambda receiver: 1})
    assert context.computed == 1

    root_events: list[object] = []
    isolated_events: list[object] = []
    isolated = context.isolate("value")

    def observe_root(_name: object, value: object) -> None:
        root_events.append(value)

    def observe_isolated(_name: object, value: object) -> None:
        isolated_events.append(value)

    context.on("internal/service", observe_root)
    isolated.on("internal/service", observe_isolated)
    effect = context.provide("value", "root")
    label = context.reflect.label(context, "value")
    await context.reflect.notify_label("value", label)

    assert root_events[-1] == "root"
    assert isolated_events == []
    await effect.dispose()
    await context.aclose()


@pytest.mark.asyncio
async def test_set_returns_accessor_decision() -> None:
    context = Context()
    context.accessor("accepted", {"get": lambda receiver: 1, "set": lambda receiver, value: True})
    context.accessor("rejected", {"get": lambda receiver: 1, "set": lambda receiver, value: False})

    assert context.set("accepted", 2) is True
    assert context.set("rejected", 2) is False
    await context.aclose()
