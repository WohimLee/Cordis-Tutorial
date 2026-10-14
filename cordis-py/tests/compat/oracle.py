"""Execute shared compatibility scenarios with Cordis-py."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypedDict, cast

from cordis import (
    Context,
    DisposableList,
    Fiber,
    FiberState,
    Inject,
    Logger,
    LoggerLevel,
    LoggerService,
    Message,
    Service,
)


class Scenario(TypedDict):
    id: str
    expected: object


async def run_core_smoke() -> dict[str, object]:
    trace: list[str] = []
    context = Context()

    def record_event(value: object) -> None:
        trace.append(f"event:{value}")

    def probe(plugin_context: Context, _config: object) -> object:
        trace.append("activate")
        plugin_context.on("probe", record_event)
        return lambda: trace.append("cleanup")

    fiber = context.plugin(probe)
    await fiber.wait()
    context.emit("probe", "value")
    await fiber.dispose()
    context.emit("probe", "after-dispose")
    await context.aclose()

    return {
        "trace": list(trace),
        "fiber_state": fiber.state.value,
    }


async def run_plugin_shapes() -> dict[str, object]:
    activated: list[str] = []
    cleaned: list[str] = []
    context = Context()

    def function_probe(_context: Context, config: object) -> object:
        activated.append(f"function:{config}")
        return lambda: cleaned.append("function")

    class ClassProbe:
        def __init__(self, _context: Context, config: object) -> None:
            activated.append(f"class:{config}")

    class ObjectProbe:
        name = "objectProbe"

        def apply(self, _context: Context, config: object) -> object:
            activated.append(f"object:{config}")
            return lambda: cleaned.append("object")

    fibers = [
        context.plugin(function_probe, "function"),
        context.plugin(ClassProbe, "class"),
        context.plugin(ObjectProbe(), "object"),
    ]
    await asyncio.gather(*(fiber.wait() for fiber in fibers))
    registry_size_before = context.registry.size
    await context.aclose()
    return {
        "activated": sorted(activated),
        "cleaned": sorted(cleaned),
        "registry_size_before": registry_size_before,
        "registry_size_after": context.registry.size,
    }


async def run_context_registry() -> dict[str, object]:
    trace: list[object] = []
    context = Context()
    child = context.extend({"marker": "child"})

    def probe(plugin_context: Context, _config: object) -> None:
        trace.append(plugin_context.marker)

    fiber = child.plugin(probe)
    await fiber.wait()
    runtime = context.registry.get(probe)
    visited = 0

    def visit(_runtime: object, _key: object) -> None:
        nonlocal visited
        visited += 1

    context.registry.forEach(visit)
    result: dict[str, object] = {
        "child_marker": child.marker,
        "parent_marker": context.marker,
        "missing_strict": context.get("missing"),
        "missing_loose": context.get("missing", False),
        "trace": trace,
        "registry": {
            "size": context.registry.size,
            "has": context.registry.has(probe),
            "keys": len(tuple(context.registry.keys())),
            "values": len(tuple(context.registry.values())),
            "entries": len(tuple(context.registry.entries())),
            "visited": visited,
            "runtime": [runtime.name, runtime.callback is probe, runtime.Config is None]
            if runtime
            else None,
        },
        "service_contexts": [
            context.events.ctx is context,
            context.registry.ctx is context,
            context.reflect.ctx is context,
            context.logger.ctx is context,
        ],
    }
    if runtime is None:
        raise RuntimeError("missing probe runtime")
    await fiber.dispose()
    result["registry_size_after"] = context.registry.size
    await context.aclose()
    return result


async def run_inject_delete() -> dict[str, object]:
    trace: list[str] = []
    context = Context()

    def consumer(plugin_context: Context, _config: object) -> object:
        trace.append(f"activate:{plugin_context.value}")
        return lambda: trace.append("cleanup")

    def provider(plugin_context: Context, _config: object) -> None:
        plugin_context.provide("value", "ready")

    consumer_fiber = context.inject(["value"], consumer)
    await consumer_fiber.wait()
    initial_state = consumer_fiber.state.value
    provider_fiber = context.plugin(provider)
    await provider_fiber.wait()
    await consumer_fiber.wait()
    active_state = consumer_fiber.state.value
    consumer_runtime = context.registry.get(consumer)
    if consumer_runtime is None:
        raise RuntimeError("missing consumer runtime")
    mounted_fiber = next(iter(consumer_runtime.fibers))
    removed = context.registry.delete(consumer)
    await mounted_fiber.dispose()
    result: dict[str, object] = {
        "initial_state": initial_state,
        "active_state": active_state,
        "trace": trace,
        "delete_returned_runtime": removed is not None,
        "has_after_delete": context.registry.has(consumer),
        "registry_size_after": context.registry.size,
    }
    await context.aclose()
    return result


async def run_strict_get() -> dict[str, object]:
    result: dict[str, object] = {}
    context = Context()

    def provider(plugin_context: Context, _config: object) -> None:
        plugin_context.provide("value", "loading")
        result["during_loading_strict"] = plugin_context.get("value")
        result["during_loading_loose"] = plugin_context.get("value", False)

    fiber = context.plugin(provider)
    await fiber.wait()
    result["after_loading_strict"] = context.get("value")
    await context.aclose()
    return result


async def settle_registry(context: Context) -> None:
    fibers = [fiber for runtime in context.registry.values() for fiber in runtime.fibers]
    await asyncio.gather(*(fiber.wait() for fiber in fibers))


async def run_inject_metadata() -> dict[str, object]:
    trace: list[str] = []
    context = Context()

    @Inject("a")
    class Base(Service):
        def __init__(self, plugin_context: Context, _config: object) -> None:
            super().__init__(plugin_context, "worker")

    @Inject("c")
    class Worker(Base):
        def __init__(self, plugin_context: Context, _config: object) -> None:
            super().__init__(plugin_context, _config)
            trace.append(f"construct:{plugin_context.a}:{plugin_context.c}")

        @Inject("b")
        def run(self) -> object:
            value = self.context.b
            trace.append(f"method:{value}")
            return lambda: trace.append(f"method-cleanup:{value}")

    worker = context.plugin(Worker)
    await worker.wait()
    before_services = worker.state.value

    def provide(name: str, value: str) -> object:
        def provider(plugin_context: Context, _config: object) -> None:
            plugin_context.provide(name, value)

        return provider

    provider_a = context.plugin(provide("a", "A"))
    await provider_a.wait()
    await worker.wait()
    after_inherited_only = worker.state.value

    provider_c = context.plugin(provide("c", "C"))
    await provider_c.wait()
    await worker.wait()
    after_class_dependencies = worker.state.value

    provider_b1 = context.plugin(provide("b", "B1"))
    await provider_b1.wait()
    await settle_registry(context)
    await provider_b1.dispose()
    await settle_registry(context)

    provider_b2 = context.plugin(provide("b", "B2"))
    await provider_b2.wait()
    await settle_registry(context)
    await context.aclose()
    return {
        "before_services": before_services,
        "after_inherited_only": after_inherited_only,
        "after_class_dependencies": after_class_dependencies,
        "trace": trace,
    }


async def run_context_filter() -> dict[str, object]:
    context = Context()
    context.baseUrl = "file:///root/"
    allowed = context.extend()
    blocked = context.extend()
    seen: list[str] = []
    dispatch_context_seen = False

    def observe_dispatch(
        _mode: object,
        _name: object,
        _args: object,
        current: Context | None,
    ) -> None:
        nonlocal dispatch_context_seen
        dispatch_context_seen = current is dispatch_context

    def filter_owner(owner: Context) -> bool:
        return owner is allowed

    dispatch_context = context.extend({Context.filter: filter_owner})
    context.on("internal/dispatch", observe_dispatch)
    allowed.on("probe", lambda: seen.append("allowed"))
    blocked.on("probe", lambda: seen.append("blocked"))
    blocked.on("probe", lambda: seen.append("global"), {"global": True})
    context.emit(dispatch_context, "probe")
    result: dict[str, object] = {
        "root_is_context": Context.is_context(context),
        "child_is_context": Context.is_context(allowed),
        "plain_is_context": Context.is_context({}),
        "root_base_url": context.baseUrl,
        "child_base_url": allowed.baseUrl,
        "seen": seen,
        "dispatch_context_seen": dispatch_context_seen,
    }
    await context.aclose()
    return result


async def run_inject_config() -> dict[str, object]:
    context = Context()
    seen: list[dict[str, object]] = []

    class Configurable(Service):
        provide = "configurable"

        def read(self) -> dict[str, object]:
            return self.resolve_config({"base": 1, "shared": "base"}, {"head": 3})

    provider = context.plugin(Configurable)
    await provider.wait()

    def consumer(plugin_context: Context, _config: object) -> None:
        service = cast(Configurable, plugin_context.configurable)
        seen.append(service.read())

    fiber = context.inject({"configurable": {"middle": 2, "shared": "inject"}}, consumer)
    await fiber.wait()
    result: dict[str, object] = {"seen": seen, "state": fiber.state.value}
    await context.aclose()
    return result


async def run_event_contracts() -> dict[str, object]:
    context = Context()
    order: list[str] = []
    modes: list[str] = []
    replacement_options: list[dict[str, bool]] = []
    replacement_active = True

    def observe_dispatch(mode: str, *_args: object) -> None:
        modes.append(mode)

    def replace_listener(
        name: object,
        _listener: object,
        options: dict[str, bool],
    ) -> object:
        if name != "replaced":
            return None
        replacement_options.append(options)

        def dispose() -> bool:
            nonlocal replacement_active
            previous, replacement_active = replacement_active, False
            return previous

        return dispose

    context.on("internal/dispatch", observe_dispatch)
    context.on("internal/listener", replace_listener)
    first = context.on("order", lambda: order.append("normal"))
    context.on("order", lambda: order.append("prepend"), True)
    once = context.once("once", lambda: order.append("once"))
    replaced = context.on("replaced", lambda: order.append("unexpected"), True)
    context.emit("order")
    context.emit("once")
    context.emit("once")
    first_dispose = [first(), first()]
    once_after_emit = once()
    replacement_dispose = [replaced(), replaced()]

    emit_done = asyncio.Event()

    async def async_emit() -> None:
        await asyncio.sleep(0)
        order.append("emit-async")
        emit_done.set()

    context.on("async-emit", async_emit)
    context.emit("async-emit")
    await emit_done.wait()

    async def async_bail() -> str:
        await asyncio.sleep(0)
        return "async-bail"

    context.on("bail", async_bail)
    bail_result = await cast(Awaitable[object], context.bail("bail"))
    context.on("serial", lambda: False)
    context.on("serial", lambda: "serial")
    serial_result = await context.serial("serial")

    async def fail() -> None:
        raise ValueError("parallel")

    context.on("parallel", fail)
    parallel_errors = 0
    try:
        await context.parallel("parallel")
    except BaseExceptionGroup as error:
        parallel_errors = len(error.exceptions)

    async def outer(_value: object, next_: Callable[[], Awaitable[object]]) -> str:
        return f"outer({await next_()})"

    context.on("waterfall", outer)
    waterfall_result = await context.waterfall("waterfall", "value", next_=lambda: "inner")
    result: dict[str, object] = {
        "order": order,
        "modes": modes,
        "first_dispose": first_dispose,
        "once_after_emit": once_after_emit,
        "replacement_options": replacement_options,
        "replacement_dispose": replacement_dispose,
        "bail_result": bail_result,
        "serial_result": serial_result,
        "parallel_errors": parallel_errors,
        "waterfall_result": waterfall_result,
    }
    await context.aclose()
    return result


async def run_effect_contracts() -> dict[str, object]:
    context = Context()
    trace: list[str] = []
    effect = context.effect(
        lambda: [lambda: trace.append("first"), lambda: trace.append("second")],
        "pair",
    )
    awaited = await effect
    first_disposal = effect()
    second_disposal = effect()
    await first_disposal
    invalid_error: str | None = None
    try:
        context.effect(lambda: 1)
    except TypeError as error:
        invalid_error = str(error)
    async_trace: list[str] = []
    setup_started = asyncio.Event()
    allow_setup = asyncio.Event()

    async def async_setup() -> Callable[[], None]:
        async_trace.append("setup-start")
        setup_started.set()
        await allow_setup.wait()
        async_trace.append("setup-end")
        return lambda: async_trace.append("cleanup")

    async_effect = context.effect(async_setup)
    await setup_started.wait()
    async_disposal = async_effect()
    pending_before_release = not async_disposal.done()
    allow_setup.set()
    await async_disposal
    parent = context.effect(lambda: context.effect(lambda: None, "child"), "parent")
    await parent
    effect_meta = context.fiber.getEffects()[0]
    metadata = [effect_meta.label, [child.label for child in effect_meta.children]]
    await parent.dispose()
    result: dict[str, object] = {
        "awaited_callable": callable(awaited),
        "shared_disposal": first_disposal is second_disposal,
        "trace": trace,
        "invalid_error": invalid_error,
        "live_effects": len(context.fiber.getEffects()),
        "async_trace": async_trace,
        "pending_before_release": pending_before_release,
        "metadata": metadata,
    }
    await context.aclose()
    return result


async def run_fiber_contracts() -> dict[str, object]:
    context = Context()
    trace: list[str] = []
    states: list[list[str]] = []
    plugin_context: Context | None = None

    def observe_state(fiber: Fiber, old: FiberState) -> None:
        states.append([old.value, fiber.state.value])

    context.on("internal/status", observe_state)

    def plugin(current: Context, config: object) -> Callable[[], None]:
        nonlocal plugin_context
        plugin_context = current
        trace.append(f"activate:{config}")
        return lambda: trace.append(f"cleanup:{config}")

    mounted = context.plugin(plugin, "one")
    fiber = await mounted.wait()
    initial_uid = fiber.uid
    initial = {
        "name": fiber.name,
        "ctx_same": cast(Context, plugin_context) is fiber.ctx,
        "raw_config": vars(fiber)["_config"],
        "config": fiber.config,
        "store_size": len(fiber.store or {}),
        "inertia": fiber.inertia is not None,
    }
    restart_result = await fiber.restart()
    update_result = await fiber.update("two")
    await fiber.dispose()
    inactive_error: str | None = None
    try:
        fiber.assertActive()
    except Exception as error:
        inactive_error = getattr(error, "code", None)
    result: dict[str, object] = {
        "initial_uid_positive": isinstance(initial_uid, int) and initial_uid > 0,
        "initial": initial,
        "restart_result": restart_result,
        "update_result": update_result,
        "trace": trace,
        "states": list(states),
        "disposed_uid": fiber.uid,
        "disposed_state": fiber.state.value,
        "disposed_store": fiber.store,
        "inactive_error": inactive_error,
    }
    await context.aclose()
    return result


async def run_fiber_invalid_update() -> dict[str, object]:
    context = Context()

    class Positive:
        @staticmethod
        def validate(value: object) -> int:
            if not isinstance(value, int) or value <= 0:
                raise ValueError("positive")
            return value

    def plugin(_context: Context, _config: object) -> None:
        return None

    plugin.Config = Positive  # type: ignore[attr-defined]
    plugin.inject = ["ready"]  # type: ignore[attr-defined]

    def provider(plugin_context: Context, _config: object) -> None:
        plugin_context.provide("ready", True)

    active_provider = context.plugin(provider)
    await active_provider.wait()
    active = context.plugin(plugin, 1)
    await active.wait()
    active_failed_immediately = False
    active_error: str | None = None
    try:
        await active.update(0)
    except Exception as error:
        active_failed_immediately = True
        active_error = str(error)

    isolated = context.isolate("ready")
    pending = isolated.plugin(plugin, 1)
    await pending.wait()
    pending_failed_immediately = False
    try:
        await pending.update(0)
    except Exception:
        pending_failed_immediately = True
    isolated_provider = isolated.plugin(provider)
    await isolated_provider.wait()
    pending_failed_on_activation = False
    try:
        await pending.wait()
    except Exception:
        pending_failed_on_activation = True
    result: dict[str, object] = {
        "active_failed_immediately": active_failed_immediately,
        "active_error": active_error,
        "active_raw": vars(active)["_config"],
        "active_config": active.config,
        "active_state": active.state.value,
        "pending_failed_immediately": pending_failed_immediately,
        "pending_raw": vars(pending)["_config"],
        "pending_failed_on_activation": pending_failed_on_activation,
        "pending_state": pending.state.value,
    }
    await context.aclose()
    return result


async def run_fiber_failures() -> dict[str, object]:
    context = Context()
    trace: list[str] = []

    def unstable(plugin_context: Context, config: object) -> None:
        trace.append(f"activate:{config}")
        plugin_context.effect(lambda: lambda: trace.append(f"cleanup:{config}"))
        if config == "fail":
            raise ValueError("startup failed")

    fiber = context.plugin(unstable, "fail")
    startup_failed = False
    try:
        await fiber.wait()
    except ValueError:
        startup_failed = True
    failed_state = fiber.state.value
    await fiber.update("ok")
    await fiber.wait()

    cleanup_called = False

    def broken_cleanup(_context: Context, _config: object) -> Callable[[], None]:
        def cleanup() -> None:
            nonlocal cleanup_called
            cleanup_called = True
            raise RuntimeError("cleanup failed")

        return cleanup

    broken = context.plugin(broken_cleanup)
    await broken.wait()
    try:
        await broken.dispose()
    except BaseExceptionGroup:
        pass
    result: dict[str, object] = {
        "startup_failed": startup_failed,
        "failed_state": failed_state,
        "recovered_state": fiber.state.value,
        "trace": list(trace),
        "cleanup_called": cleanup_called,
        "cleanup_final_state": broken.state.value,
        "cleanup_final_uid": broken.uid,
        "cleanup_removed": not context.registry.has(broken_cleanup),
    }
    await context.aclose()
    return result


async def run_fiber_dependency_races() -> dict[str, object]:
    class Value(Service):
        provide = "value"

        def __init__(self, plugin_context: Context, config: object) -> None:
            self.value = config
            super().__init__(plugin_context)

    loss_context = Context()
    provider = loss_context.plugin(Value, "first")
    await provider.wait()
    loading_started = asyncio.Event()
    allow_loading = asyncio.Event()
    loss_cleanups = 0

    async def loading_consumer(plugin_context: Context, _config: object) -> object:
        nonlocal loss_cleanups
        loading_started.set()
        await allow_loading.wait()

        def cleanup() -> None:
            nonlocal loss_cleanups
            loss_cleanups += 1

        return cleanup

    loading_consumer.inject = ["value"]  # type: ignore[attr-defined]
    loss_fiber = loss_context.plugin(loading_consumer)
    await loading_started.wait()
    provider_disposal = asyncio.create_task(provider.dispose())
    await asyncio.sleep(0)
    allow_loading.set()
    await provider_disposal
    await loss_fiber.wait()
    loss_result = {
        "state": loss_fiber.state.value,
        "cleanups": loss_cleanups,
    }
    await loss_context.aclose()

    restore_context = Context()
    provider_a = restore_context.plugin(Value, "A")
    await provider_a.wait()
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    activations: list[object] = []

    def restore_consumer(plugin_context: Context, _config: object) -> object:
        activations.append(cast(Value, plugin_context.value).value)

        async def cleanup() -> None:
            cleanup_started.set()
            await allow_cleanup.wait()

        return cleanup

    restore_consumer.inject = ["value"]  # type: ignore[attr-defined]
    restore_fiber = restore_context.plugin(restore_consumer)
    await restore_fiber.wait()
    disposal_a = asyncio.create_task(provider_a.dispose())
    await cleanup_started.wait()
    provider_b = restore_context.plugin(Value, "B")
    allow_cleanup.set()
    await provider_b.wait()
    await disposal_a
    await restore_fiber.wait()
    restore_result = {
        "state": restore_fiber.state.value,
        "activations": list(activations),
    }
    await restore_context.aclose()
    return {"loss": loss_result, "restore": restore_result}


async def run_reflect_service() -> dict[str, object]:
    context = Context()
    trace: list[str] = []

    class MergeConfig:
        @staticmethod
        def validate(value: object) -> object:
            return value

        @staticmethod
        def merge(*configs: object) -> dict[str, object]:
            return {"count": len(configs)}

    class Fancy(Service):
        provide = "fancy"
        Config = MergeConfig

        def __call__(self) -> object:
            return getattr(self.caller_context, "marker", None)

        def init(self) -> Callable[[], None]:
            trace.append("init")
            return lambda: trace.append("cleanup")

        def read(self) -> dict[str, object]:
            return self.resolve_config({"base": True}, {"head": True})

    provider = context.plugin(Fancy)
    await provider.wait()
    consumer = context.extend({"marker": "consumer"}).intercept("fancy", {"middle": True})
    service = cast(Fancy, consumer.fancy)
    derived = service.extend(extra=True)

    shared_label = object()
    isolated_a = context.isolate("shared", shared_label)
    isolated_b = context.isolate("shared", shared_label)
    isolated_a.provide("shared", "scoped")
    scope_result = [isolated_b.get("shared"), context.get("shared")]

    root_events: list[object] = []
    isolated_events: list[object] = []
    isolated = context.isolate("notice")

    def observe_root(_name: object, value: object) -> None:
        root_events.append(value)

    def observe_isolated(_name: object, value: object) -> None:
        isolated_events.append(value)

    context.on("internal/service", observe_root)
    isolated.on("internal/service", observe_isolated)
    notice = context.provide("notice", "root")
    await context.reflect.notify_label("notice", context.reflect.label(context, "notice"))
    result: dict[str, object] = {
        "call": service(),
        "config": service.read(),
        "derived_extra": getattr(derived, "extra", False),
        "scope": scope_result,
        "service_events": [root_events[-1], len(isolated_events)],
        "set_result": context.set("notice", "updated"),
    }
    await notice.dispose()
    await provider.dispose()
    result["trace"] = trace
    await context.aclose()
    return result


async def run_accessor_mixin() -> dict[str, object]:
    context = Context()
    state = {"value": 1}

    def set_computed(_receiver: Context, value: object) -> bool:
        if not isinstance(value, int) or value < 0:
            return False
        state["value"] = value
        return True

    accessor = context.accessor(
        "computed",
        {"get": lambda _receiver: state["value"], "set": set_computed},
    )

    class Source:
        def __init__(self) -> None:
            self.count = 2

        def inc(self) -> int:
            self.count += 1
            return self.count

    source = context.provide("source", Source())
    mixin = context.mixin("source", ["count", "inc"])
    values = [context.computed, context.set("computed", 3), context.computed]
    rejected = context.set("computed", -1)
    increment = cast(Callable[[], object], context.inc)
    mixed: list[object] = [context.count, increment(), context.count]
    await mixin.dispose()
    await accessor.dispose()
    result: dict[str, object] = {
        "values": values,
        "rejected": rejected,
        "mixed": mixed,
        "after_dispose": [context.get("computed"), context.get("count")],
    }
    await source.dispose()
    await context.aclose()
    return result


async def run_logger_contracts() -> dict[str, object]:
    context = Context()

    def format_x(value: object, _exporter: object, _message: Message) -> str:
        return f"<{value}>"

    class Capture:
        colors = False
        maxLength = 5

        def __init__(self) -> None:
            self.levels = {"scope": LoggerLevel.DEBUG}
            self.formatters = {"x": format_x}
            self.messages: list[Message] = []

        def export(self, message: Message) -> None:
            self.messages.append(message)

    capture = Capture()
    effect = context.exporter(capture)
    child = context.intercept("logger", {"name": "scope", "level": LoggerLevel.WARN})
    logger = child.logger()
    logger.debug("debug")
    logger.info("hello %s", "world")
    logger.warn("warn")
    logger.error("error")
    formatted = Logger.format(
        capture,
        Message(0, 0, "scope", "info", 1, ("abcdef\n%s %x", "ok", "z")),
    )
    messages = [
        {
            "sn": message.sn,
            "name": message.name,
            "type": message.type,
            "level": message.level,
            "args": list(message.args),
            "fiber": message.fiber() is child.fiber if message.fiber else False,
        }
        for message in capture.messages
    ]
    logger_service = cast(LoggerService, context.root.logger)
    logger_service.bufferSize = 2
    root_logger = context.logger("buffer")
    root_logger.info("one")
    root_logger.info("two")
    root_logger.info("three")
    result: dict[str, object] = {
        "levels": [level.value for level in LoggerLevel],
        "messages": messages,
        "formatted": formatted,
        "buffer": [list(message.args) for message in logger_service.buffer],
    }
    await effect.dispose()
    await context.aclose()
    return result


async def run_logger_options() -> dict[str, object]:
    context = Context()

    class Capture:
        colors = False

        def __init__(self) -> None:
            self.levels = {"default": LoggerLevel.DEBUG}
            self.messages: list[Message] = []

        def export(self, message: Message) -> None:
            self.messages.append(message)

    capture = Capture()
    effect = context.exporter(capture)
    service = cast(LoggerService, context.root.logger)
    logger = Logger(
        {
            "name": "original",
            "level": LoggerLevel.DEBUG,
            "meta": {"name": "override", "type": "custom", "level": 0, "tag": "value"},
        },
        service,
    )
    logger.debug("message")

    def CamelCase(plugin_context: Context, _config: object) -> None:
        plugin_context.logger().info("plugin")

    fiber = context.plugin(CamelCase)
    await fiber.wait()
    first, second = capture.messages
    result: dict[str, object] = {
        "meta": [first.name, first.type, first.level, getattr(first, "tag", None)],
        "default_name": second.name,
        "codes": [Logger.code("short", 1), Logger.code("veryLongLoggerName", 2)],
    }
    await effect.dispose()
    await context.aclose()
    return result


async def run_disposable_list() -> dict[str, object]:
    class Value:
        def __init__(self, name: str) -> None:
            self.name = name

    values = DisposableList[Value]()
    first, second = Value("a"), Value("b")
    dispose_first = values.push(first)
    dispose_second = values.push(second)
    values.push(first)
    before = [value.name for value in values]
    deleted = values.delete(first)
    after_delete = [value.name for value in values]
    disposed = dispose_first()
    cleared = [value.name for value in values.clear()]
    return {
        "before": before,
        "deleted": deleted,
        "after_delete": after_delete,
        "disposed": disposed,
        "cleared": cleared,
        "length": values.length,
        "stale_disposer": dispose_second(),
    }


async def run_service_facades() -> dict[str, object]:
    context = Context()
    child = context.extend()
    trace: list[str] = []

    class Capture:
        colors = False

        def __init__(self) -> None:
            self.messages: list[Message] = []

        def export(self, message: Message) -> None:
            self.messages.append(message)

    capture = Capture()
    exporter = child.logger.exporter(capture)

    def record(value: object) -> None:
        trace.append(str(value))

    listener = child.events.on("facade", record)
    provided = child.reflect.provide("facade_value", 7)

    def probe(_plugin_context: Context, _config: object) -> None:
        trace.append("plugin")

    fiber = child.registry.plugin(probe)
    await fiber.wait()
    child.events.emit("facade", "event")
    child.logger("facade").info("logged")
    result: dict[str, object] = {
        "value": child.reflect.get("facade_value"),
        "registered": child.registry.has(probe),
        "trace": trace,
        "message": [capture.messages[0].name, list(capture.messages[0].args)],
        "contexts": [
            child.events.ctx is child,
            child.reflect.ctx is child,
            child.registry.ctx is child,
            child.logger.ctx is child,
        ],
    }
    listener()
    await provided.dispose()
    await exporter.dispose()
    await context.aclose()
    return result


async def run_remaining_contracts() -> dict[str, object]:
    context = Context()
    activations = 0

    class Switchable(Service):
        provide = "switchable"

        def __init__(self, plugin_context: Context, _config: object) -> None:
            self.enabled = True
            super().__init__(plugin_context)

        def available(self) -> bool:
            return self.enabled

    provider = await context.plugin(Switchable)
    service = cast(Switchable, provider.ctx.reflect.get("switchable"))

    def consumer(_plugin_context: Context, _config: object) -> None:
        nonlocal activations
        activations += 1

    consumer.inject = ["switchable"]  # type: ignore[attr-defined]
    dependent = await context.plugin(consumer)
    service.enabled = False
    await asyncio.gather(
        *(fiber.wait() for fiber in service.context.reflect.notify([service.name]))
    )
    unavailable = dependent.state.value
    service.enabled = True
    await asyncio.gather(
        *(fiber.wait() for fiber in service.context.reflect.notify([service.name]))
    )
    restored = dependent.state.value

    def probe(_plugin_context: Context, _config: object) -> None:
        return None

    first = await context.plugin(probe)
    second = await context.plugin(probe)
    runtime = context.registry.get(probe)
    count = len(runtime.fibers) if runtime else 0
    removed = context.registry.delete(probe)
    await first.dispose()
    await second.dispose()
    result: dict[str, object] = {
        "availability": [unavailable, restored, activations],
        "registry": [count, removed is runtime, context.registry.has(probe)],
        "await_returns_fiber": provider.ctx.fiber is provider,
    }
    await context.aclose()
    return result


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    scenario = cast(Scenario, json.loads(args.scenario.read_text()))
    cases = {
        "core-smoke": run_core_smoke,
        "plugin-shapes": run_plugin_shapes,
        "context-registry": run_context_registry,
        "inject-delete": run_inject_delete,
        "strict-get": run_strict_get,
        "inject-metadata": run_inject_metadata,
        "context-filter": run_context_filter,
        "inject-config": run_inject_config,
        "event-contracts": run_event_contracts,
        "effect-contracts": run_effect_contracts,
        "fiber-contracts": run_fiber_contracts,
        "fiber-invalid-update": run_fiber_invalid_update,
        "fiber-failures": run_fiber_failures,
        "fiber-dependency-races": run_fiber_dependency_races,
        "reflect-service": run_reflect_service,
        "accessor-mixin": run_accessor_mixin,
        "logger-contracts": run_logger_contracts,
        "logger-options": run_logger_options,
        "disposable-list": run_disposable_list,
        "service-facades": run_service_facades,
        "remaining-contracts": run_remaining_contracts,
    }
    try:
        run = cases[scenario["id"]]
    except KeyError as error:
        raise SystemExit(f"unsupported scenario: {scenario['id']}") from error
    result = await run()
    if args.check and result != scenario["expected"]:
        raise SystemExit(json.dumps({"expected": scenario["expected"], "actual": result}, indent=2))
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    asyncio.run(main())
