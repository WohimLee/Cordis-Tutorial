import json
from pathlib import Path
from typing import cast

import pytest

from cordis import Inject
from cordis.context import Context
from cordis.errors import CordisError, CordisErrorCode
from cordis.fiber import FiberState
from cordis.service import Service


class ValueService(Service):
    provide = "value"

    def __init__(self, context: Context, config: object = None) -> None:
        self.value = config
        super().__init__(context)


@pytest.mark.asyncio
async def test_consumer_waits_for_provider_and_reloads_after_replacement() -> None:
    context = Context()
    trace: list[dict[str, object]] = []
    consumer_fiber = None

    def consumer(plugin_context: Context, config: object) -> object:
        value = cast(ValueService, plugin_context.value)
        assert consumer_fiber is not None
        trace.append({"plugin": "consumer", "state": consumer_fiber.state.value})
        trace.append(
            {
                "plugin": "consumer",
                "event": "activate",
                "service_owner": value.value,
            }
        )

        def cleanup() -> None:
            fiber = consumer_fiber
            assert fiber is not None
            trace.append({"plugin": "consumer", "state": fiber.state.value})
            trace.append(
                {
                    "plugin": "consumer",
                    "event": "dispose-effect",
                    "effect": "consumer-resource",
                }
            )

        return cleanup

    consumer.inject = ["value"]  # type: ignore[attr-defined]
    consumer_fiber = context.plugin(consumer)
    await consumer_fiber.wait()
    assert consumer_fiber.state is FiberState.PENDING
    trace.append({"plugin": "consumer", "state": consumer_fiber.state.value})

    provider_a = context.plugin(ValueService, "provider-a")
    await provider_a.wait()
    await consumer_fiber.wait()
    assert consumer_fiber.state is FiberState.ACTIVE
    trace.append({"plugin": "consumer", "state": consumer_fiber.state.value})

    await provider_a.dispose()
    await consumer_fiber.wait()
    assert consumer_fiber.state is FiberState.PENDING
    trace.append({"plugin": "consumer", "state": consumer_fiber.state.value})

    provider_b = context.plugin(ValueService, "provider-b")
    await provider_b.wait()
    await consumer_fiber.wait()
    assert consumer_fiber.state is FiberState.ACTIVE
    trace.append({"plugin": "consumer", "state": consumer_fiber.state.value})

    await context.aclose()
    assert consumer_fiber.state is FiberState.DISPOSED
    trace.append({"plugin": "consumer", "state": consumer_fiber.state.value})

    scenario_path = Path(__file__).parent / "compat/scenarios/001-provider-replacement.json"
    scenario = cast(dict[str, object], json.loads(scenario_path.read_text()))
    assert trace == cast(list[dict[str, object]], scenario["expected"])


@pytest.mark.asyncio
async def test_isolated_services_do_not_conflict() -> None:
    context = Context()
    isolated = context.isolate("value")

    root_provider = context.plugin(ValueService, "root")
    isolated_provider = isolated.plugin(ValueService, "isolated")
    await root_provider.wait()
    await isolated_provider.wait()

    root_value = cast(ValueService, context.value)
    isolated_value = cast(ValueService, isolated.value)
    assert root_value.value == "root"
    assert isolated_value.value == "isolated"
    await context.aclose()


@pytest.mark.asyncio
async def test_duplicate_provider_fails_without_replacing_original() -> None:
    context = Context()
    original = context.plugin(ValueService, "original")
    duplicate = context.plugin(ValueService, "duplicate")
    await original.wait()

    with pytest.raises(CordisError) as caught:
        await duplicate.wait()

    assert caught.value.code is CordisErrorCode.DUPLICATE_SERVICE
    assert cast(ValueService, context.value).value == "original"
    await context.aclose()


@pytest.mark.asyncio
async def test_context_extend_get_and_registry_public_contracts() -> None:
    context = Context()
    child = context.extend({"marker": "child"})

    assert child.marker == "child"
    assert context.get("missing") is None
    assert context.get("missing", False) is None

    trace: list[str] = []

    def plugin(plugin_context: Context, _config: object) -> object:
        trace.append(cast(str, plugin_context.marker))
        return None

    fiber = child.plugin(plugin)
    await fiber.wait()

    runtime = context.registry.get(plugin)
    assert runtime is not None
    assert context.registry.has(plugin)
    assert tuple(context.registry.keys()) == (plugin,)
    assert tuple(context.registry.values()) == (runtime,)
    assert tuple(context.registry.entries()) == ((plugin, runtime),)

    visited: list[tuple[object, object]] = []
    context.registry.forEach(lambda value, key: visited.append((key, value)))
    assert visited == [(plugin, runtime)]
    assert trace == ["child"]

    await fiber.dispose()
    assert not context.registry.has(plugin)
    await context.aclose()


@pytest.mark.asyncio
async def test_context_inject_uses_normal_plugin_lifecycle() -> None:
    context = Context()
    trace: list[str] = []

    def consumer(plugin_context: Context, _config: object) -> object:
        service = cast(ValueService, plugin_context.value)
        trace.append(cast(str, service.value))
        return lambda: trace.append("cleanup")

    consumer_fiber = context.inject(["value"], consumer)
    await consumer_fiber.wait()
    assert consumer_fiber.state is FiberState.PENDING

    provider_fiber = context.plugin(ValueService, "ready")
    await provider_fiber.wait()
    await consumer_fiber.wait()
    assert trace == ["ready"]

    await provider_fiber.dispose()
    await consumer_fiber.wait()
    assert trace == ["ready", "cleanup"]
    assert consumer_fiber.state is FiberState.PENDING
    await context.aclose()


@pytest.mark.asyncio
async def test_object_plugin_registry_identity_is_its_apply_callback() -> None:
    context = Context()

    class ObjectPlugin:
        def apply(self, _context: Context, _config: object) -> None:
            return None

    plugin = ObjectPlugin()
    fiber = context.plugin(plugin)
    await fiber.wait()

    assert context.registry.resolve(plugin) == plugin.apply
    assert context.registry.get(plugin) is fiber.runtime
    await context.aclose()


@pytest.mark.asyncio
async def test_registry_delete_returns_runtime_and_disposes_fibers() -> None:
    context = Context()

    def plugin(_context: Context, _config: object) -> None:
        return None

    fiber = context.plugin(plugin)
    await fiber.wait()
    runtime = fiber.runtime

    assert context.registry.delete(plugin) is runtime
    assert not context.registry.has(plugin)
    assert context.registry.delete(plugin) is None
    await fiber.dispose()
    assert fiber.state is FiberState.DISPOSED
    await context.aclose()


@pytest.mark.asyncio
async def test_get_strictness_while_provider_is_loading() -> None:
    context = Context()
    observed: dict[str, object | None] = {}

    def provider(plugin_context: Context, _config: object) -> None:
        plugin_context.provide("value", "loading")
        observed["strict"] = plugin_context.get("value")
        observed["loose"] = plugin_context.get("value", False)

    fiber = context.plugin(provider)
    await fiber.wait()

    assert observed == {"strict": None, "loose": "loading"}
    assert context.get("value") == "loading"
    await context.aclose()


@pytest.mark.asyncio
async def test_inject_decorator_inherits_and_mounts_method_fiber() -> None:
    context = Context()
    trace: list[str] = []

    @Inject("parent")
    class Base(Service):
        def __init__(self, plugin_context: Context, _config: object) -> None:
            super().__init__(plugin_context, "plugin")

    @Inject("child")
    class Plugin(Base):
        def __init__(self, plugin_context: Context, _config: object) -> None:
            super().__init__(plugin_context, _config)
            trace.append("construct")

        @Inject("method")
        def start(self) -> object:
            trace.append(cast(str, self.context.method))
            return lambda: trace.append("cleanup")

    assert Inject.resolve(cast(object, vars(Plugin)["inject"])) == {
        "parent": None,
        "child": None,
    }
    fiber = context.plugin(Plugin)

    def provide(name: str) -> object:
        def provider(plugin_context: Context, _config: object) -> None:
            plugin_context.provide(name, name)

        return provider

    providers = [context.plugin(provide(name)) for name in ("parent", "child")]
    for provider in providers:
        await provider.wait()
    await fiber.wait()
    assert fiber.state is FiberState.ACTIVE

    def method_provider(plugin_context: Context, _config: object) -> None:
        plugin_context.provide("method", "method")

    method = context.plugin(method_provider)
    await method.wait()
    for runtime in context.registry.values():
        for mounted in tuple(runtime.fibers):
            await mounted.wait()
    assert trace == ["construct", "method"]
    await context.aclose()
    assert trace == ["construct", "method", "cleanup"]
