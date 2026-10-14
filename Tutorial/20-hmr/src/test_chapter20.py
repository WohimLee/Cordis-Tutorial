"""Behaviour tests for the chapter 20 teaching hot-reload contract.

Every test is synchronous and driven by ``ManualClock`` and by
``HotReloader.drain()``: there is no real time, no ``asyncio.sleep`` and no
free-threaded scheduling anywhere in this suite.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from chapter20 import (
    ChangeEvent,
    ChangeKind,
    EffectScope,
    ExecutionError,
    HotReloader,
    HotReloadError,
    InvalidChangeError,
    ManualClock,
    MappingModuleSource,
    ParsedEntry,
    StaleEpochError,
)

trace: list[str] = []
stale_calls: list[str] = []


# --------------------------------------------------------------------------- #
# Fake module versions: the "code" the host claims is inside module vN
# --------------------------------------------------------------------------- #


def provider_v1(context: object, config: object) -> object:
    context.provide("value", f"v1:{config}")  # type: ignore[attr-defined]
    trace.append(f"v1:start:{config}")
    return lambda: trace.append(f"v1:stop:{config}")


def provider_v2(context: object, config: object) -> object:
    context.provide("value", f"v2:{config}")  # type: ignore[attr-defined]
    trace.append(f"v2:start:{config}")
    return lambda: trace.append(f"v2:stop:{config}")


def listener_v1(context: object, config: object) -> object:
    def on_event(payload: object) -> None:
        trace.append(f"v1:listener:{payload}")
        stale_calls.append("v1")

    context.listener(on_event)  # type: ignore[attr-defined]
    trace.append("v1:start")
    return None


def listener_v2(context: object, config: object) -> object:
    context.listener(lambda payload: trace.append(f"v2:listener:{payload}"))  # type: ignore[attr-defined]
    trace.append("v2:start")
    return None


def broken_activation(context: object, config: object) -> object:
    raise RuntimeError("v2 activation exploded")


def cleanup_failure(context: object, config: object) -> object:
    broken = {"gamma"}

    def make(label: str) -> Callable[[], Callable[[], None]]:
        """The setup runs during activation; the cleanup runs at release."""

        def setup() -> Callable[[], None]:
            def cleanup() -> None:
                trace.append(f"cleanup:{label}")
                if label in broken:
                    raise RuntimeError(f"cleanup {label} exploded")

            return cleanup

        return setup

    for label in ("alpha", "beta", "gamma", "delta", "epsilon"):
        context.effect(make(label))  # type: ignore[attr-defined]
    return None


class PositiveConfig:
    """Config validator: only positive integers are legal."""

    @staticmethod
    def validate(value: object) -> int:
        if not isinstance(value, int) or value <= 0:
            raise ValueError("expected a positive config")
        return value


provider_v1.Config = PositiveConfig  # type: ignore[attr-defined]
provider_v2.Config = PositiveConfig  # type: ignore[attr-defined]


def make_source() -> MappingModuleSource:
    return MappingModuleSource(
        {
            "app:provider": {1: provider_v1, 2: provider_v2},
            "app:listener": {1: listener_v1, 2: listener_v2},
            "app:broken": {1: provider_v1, 2: broken_activation},
            "app:cleanup": {1: cleanup_failure, 2: cleanup_failure},
        }
    )


def parsed(
    entry_id: str, module: str, config: object = 1, **kwargs: object
) -> ParsedEntry:
    return ParsedEntry(id=entry_id, module=module, config=config, **kwargs)  # type: ignore[arg-type]


def replace_event(entry: str, module: str, version: int | None = None) -> ChangeEvent:
    return ChangeEvent(
        kind=ChangeKind.MODULE, entry=entry, module=module, version=version
    )


@pytest.fixture(autouse=True)
def clear_trace() -> None:
    trace.clear()
    stale_calls.clear()


# --------------------------------------------------------------------------- #
# 1. config update keeps the generation and the fiber
# --------------------------------------------------------------------------- #


def test_config_change_reuses_the_fiber_and_keeps_the_generation() -> None:
    clock = ManualClock()
    reloader = HotReloader(make_source(), clock, debounce_ms=50)
    (mounted,) = reloader.mount((parsed("provider", "app:provider", 1),))
    generation = mounted.target.generation
    fiber = mounted.fiber
    assert mounted.context is not None
    assert mounted.context.get("value") == "v1:1"
    assert trace == ["v1:start:1"]

    reloader.on_change(ChangeEvent(kind=ChangeKind.CONFIG, entry="provider", payload=2))
    assert reloader.drain() == 1

    assert reloader.pending_events == 0
    assert reloader.generation("provider") == generation  # no new generation
    assert reloader.batches[-1].kind is ChangeKind.CONFIG
    assert reloader.batches[-1].events[0].payload == 2
    assert reloader.live("provider") is provider_v1  # same code, new config
    assert reloader.context("provider") is mounted.context
    assert reloader.context("provider").get("value") == "v1:2"
    assert reloader.entry("provider").fiber is fiber
    assert fiber is not None and fiber.config == 2
    assert fiber.epoch == 2
    assert mounted.parsed.config == 2
    assert mounted.version == 1
    # The old epoch really was released: stop-then-start, never two live epochs.
    assert trace == ["v1:start:1", "v1:stop:1", "v1:start:2"]
    assert mounted.target.retired == {}  # the generation never ended
    assert mounted.target.cleanup_counts["plugin: provider"] == 1
    assert mounted.target.cleanup_counts["provide: value"] == 1
    reloader.close()


# --------------------------------------------------------------------------- #
# 2. module version change replaces the generation
# --------------------------------------------------------------------------- #


def test_module_change_replaces_the_generation_and_uses_the_new_version() -> None:
    clock = ManualClock()
    reloader = HotReloader(make_source(), clock, debounce_ms=50)
    (mounted,) = reloader.mount((parsed("provider", "app:provider", 1),))
    target = mounted.target
    first_generation = target.generation
    first_fiber = mounted.fiber
    first_context = mounted.context
    assert first_fiber is not None and first_context is not None

    reloader.on_change(replace_event("provider", "app:provider", 2))
    assert reloader.drain() == 1

    # A replacement is a new generation: new fiber, new context, preserved target.
    assert reloader.generation("provider") == first_generation + 1
    assert target.generation == first_generation + 1
    current = reloader.entry("provider")
    assert current.target is target
    assert reloader.live("provider") is provider_v2
    assert current.fiber is not first_fiber
    assert current.context is not first_context
    assert current.fiber is not None and current.fiber.epoch == 1
    assert current.parsed.version == 2
    assert current.context is not None
    assert current.context.get("value") == "v2:1"  # config survived the replacement
    assert trace == ["v1:start:1", "v1:stop:1", "v2:start:1"]
    # The replaced generation is archived and provably clean, exactly once.
    assert len(target.history) == 1
    archived = target.history[0]
    assert archived.generation == first_generation
    assert archived.clean is True
    assert archived.released("plugin: provider") == 1
    assert archived.released("provide: value") == 1
    assert target.retired[first_generation]["plugin: provider"] == 1
    assert target.cleanup_counts == {}  # the new generation has its own ledger
    assert first_context.get("value") is None  # the service was withdrawn
    reloader.close()


# --------------------------------------------------------------------------- #
# 3. debounce merges a burst into a single batch
# --------------------------------------------------------------------------- #


def test_debounce_merges_a_burst_into_one_batch() -> None:
    clock = ManualClock()
    reloader = HotReloader(make_source(), clock, debounce_ms=100)
    reloader.mount((parsed("provider", "app:provider", 1),))

    for payload in (2, 3, 4):
        reloader.on_change(
            ChangeEvent(kind=ChangeKind.CONFIG, entry="provider", payload=payload)
        )
        reloader.advance(40)

    # Every event re-armed the window, so nothing has been applied yet.
    assert reloader.batches == []
    assert reloader.pending_events == 3
    assert trace == ["v1:start:1"]

    # clock=120, last event at 120, so the window closes at 180.
    assert reloader.advance(59) == 0  # t=179: still inside the window
    assert reloader.advance(1) == 1  # t=180: the merged burst is applied
    assert reloader.pending_events == 0

    batch = reloader.batches[0]
    assert [event.payload for event in batch.events] == [2, 3, 4]
    assert batch.kind is ChangeKind.CONFIG
    assert len(reloader.batches) == 1  # three changes, one processing step
    assert reloader.entry("provider").version == 1  # one applied update
    assert reloader.entry("provider").parsed.config == 4
    assert trace == ["v1:start:1", "v1:stop:1", "v1:start:4"]
    reloader.close()


def test_burst_with_a_module_event_is_classified_as_structural() -> None:
    clock = ManualClock()
    reloader = HotReloader(make_source(), clock, debounce_ms=25)
    reloader.mount((parsed("provider", "app:provider", 1),))

    reloader.on_change(ChangeEvent(kind=ChangeKind.CONFIG, entry="provider", payload=5))
    reloader.on_change(replace_event("provider", "app:provider", 2))
    reloader.advance(25)

    assert len(reloader.batches) == 1
    batch = reloader.batches[0]
    assert batch.structural is True
    assert batch.kind is ChangeKind.MODULE
    assert batch.version == 2
    assert batch.module == "app:provider"
    assert batch.payload == 5  # the config part of the burst is not lost
    assert reloader.drain() == 0  # nothing left to apply
    assert reloader.live("provider") is provider_v2
    assert reloader.entry("provider").parsed.config == 5
    assert reloader.entry("provider").version == 0  # a replacement, not an update
    assert trace == ["v1:start:1", "v1:stop:1", "v2:start:5"]
    reloader.close()


# --------------------------------------------------------------------------- #
# 4. a failing candidate leaves the previous generation running
# --------------------------------------------------------------------------- #


def test_failed_candidate_keeps_the_previous_generation_running() -> None:
    clock = ManualClock()
    reloader = HotReloader(make_source(), clock, debounce_ms=10)
    (mounted,) = reloader.mount((parsed("broken", "app:broken", 1),))
    target = mounted.target
    generation = target.generation
    fiber = mounted.fiber
    assert mounted.context is not None
    assert mounted.context.get("value") == "v1:1"
    assert trace == ["v1:start:1"]

    reloader.on_change(replace_event("broken", "app:broken", 2))
    reloader.drain()

    assert len(reloader.errors) == 1
    failure = reloader.errors[0]
    assert isinstance(failure, ExecutionError)
    assert failure.phase == "activate"  # resolve succeeded, activation did not
    assert isinstance(failure.cause, RuntimeError)
    assert "exploded" in str(failure.cause)

    # The previous generation is back in charge, with its original number.
    assert reloader.generation("broken") == generation
    assert target.generation == generation  # the failed attempt bumped nothing
    assert reloader.live("broken") is provider_v1
    current = reloader.entry("broken")
    assert current.fiber is not fiber  # the restore mounts a fresh fiber
    assert current.fiber is not None and current.fiber.epoch == 1
    assert all(snapshot.clean for snapshot in target.history)
    assert current.context is not None and current.context.get("value") == "v1:1"
    assert reloader.context("broken").get("value") == "v1:1"
    assert fiber is not None and fiber.epoch == 1  # the released fiber is terminal
    assert trace == ["v1:start:1", "v1:stop:1", "v1:start:1"]

    # A later, valid change still works: the failure did not poison the entry.
    reloader.on_change(replace_event("broken", "app:broken", 1))
    reloader.drain()
    assert reloader.generation("broken") > generation
    assert trace == [
        "v1:start:1",
        "v1:stop:1",
        "v1:start:1",  # the restore
        "v1:stop:1",
        "v1:start:1",  # the later valid replacement
    ]
    reloader.close()


# --------------------------------------------------------------------------- #
# 5. cleanup failures are aggregated, never swallowed
# --------------------------------------------------------------------------- #


def test_cleanup_failure_is_aggregated_in_reverse_order_without_stopping() -> None:
    clock = ManualClock()
    reloader = HotReloader(make_source(), clock, debounce_ms=10)
    (mounted,) = reloader.mount((parsed("worker", "app:cleanup", 1),))
    target = mounted.target

    reloader.on_change(replace_event("worker", "app:cleanup", 2))
    reloader.drain()

    # "gamma" raised, but every sibling cleanup still ran exactly once.
    archived = target.history[0]
    for index in range(1, 6):
        assert archived.released(f"effect: {index}") == 1
    assert archived.clean is True  # the scope itself is empty and closed
    assert trace.count("cleanup:gamma") == 1  # exactly once, no retry storm

    # The failure is reported with its type instead of being swallowed.
    assert len(reloader.errors) == 1
    failure = reloader.errors[0]
    assert isinstance(failure, ExecutionError)
    assert failure.phase == "cleanup"
    assert isinstance(failure.cause, BaseExceptionGroup)
    assert failure.cause.message == "effect cleanup failed"
    assert len(failure.cause.exceptions) == 1
    assert isinstance(failure.cause.exceptions[0], RuntimeError)

    # The replacement itself still succeeded.
    assert reloader.generation("worker") == 2
    assert reloader.entry("worker").version == 0

    # The same contract at the smallest scale: one scope, four cleanups, two of
    # them raising.  Reverse order, uninterrupted, failures aggregated.
    runs: list[str] = []

    def broken_cleanup(label: str, broken: bool) -> Callable[[], None]:
        def cleanup() -> None:
            runs.append(label)
            if broken:
                raise RuntimeError(f"{label} exploded")

        return cleanup

    scope = EffectScope()
    for label, broken in (
        ("one", False),
        ("two", True),
        ("three", True),
        ("four", False),
    ):
        scope.install(broken_cleanup(label, broken), label)

    with pytest.raises(BaseExceptionGroup) as caught:
        scope.close()

    assert runs == ["four", "three", "two", "one"]
    assert len(scope) == 0
    assert scope.closed is True
    assert [type(error) for error in caught.value.exceptions] == [
        RuntimeError,
        RuntimeError,
    ]
    scope.close()  # idempotent: no second pass, no second exception
    assert runs == ["four", "three", "two", "one"]

    with pytest.raises(BaseExceptionGroup):
        reloader.close()  # "gamma" fails again on the way out, and is reported


# --------------------------------------------------------------------------- #
# 6. an invalid update keeps the current epoch installed
# --------------------------------------------------------------------------- #


def test_invalid_update_keeps_the_current_epoch_installed() -> None:
    clock = ManualClock()
    reloader = HotReloader(make_source(), clock, debounce_ms=10)
    (mounted,) = reloader.mount((parsed("provider", "app:provider", 1),))
    generation = mounted.target.generation
    fiber = mounted.fiber
    assert mounted.context is not None
    assert mounted.context.get("value") == "v1:1"

    reloader.on_change(ChangeEvent(kind=ChangeKind.CONFIG, entry="provider", payload=0))
    reloader.drain()

    assert len(reloader.errors) == 1
    failure = reloader.errors[0]
    assert isinstance(failure, ValueError)  # rejected before any side effect
    assert "positive" in str(failure)
    assert reloader.generation("provider") == generation
    assert reloader.entry("provider").fiber is fiber
    assert fiber is not None and fiber.config == 1 and fiber.epoch == 1
    assert reloader.context("provider").get("value") == "v1:1"
    assert reloader.entry("provider").version == 0  # nothing was applied
    assert trace == ["v1:start:1"]  # the live epoch was never released
    reloader.close()


# --------------------------------------------------------------------------- #
# 7. listeners of a replaced generation never fire again
# --------------------------------------------------------------------------- #


def test_old_epoch_listener_and_tasks_never_fire_after_replacement() -> None:
    clock = ManualClock()
    reloader = HotReloader(make_source(), clock, debounce_ms=10)
    (mounted,) = reloader.mount((parsed("listener", "app:listener", None),))
    target = mounted.target
    first_generation = target.generation
    first_context = mounted.context
    assert first_context is not None

    assert reloader.emit("listener", "one") == [None]
    assert trace == ["v1:start", "v1:listener:one"]
    assert stale_calls == ["v1"]
    assert first_context.listener_count() == 1

    reloader.on_change(replace_event("listener", "app:listener", 2))
    reloader.drain()

    assert reloader.generation("listener") == first_generation + 1
    assert target.history[0].listeners is False
    assert target.history[0].released("listener: 0") == 1  # released once
    assert first_context.listener_count() == 0
    assert reloader.batches[-1].kind is ChangeKind.MODULE

    reloader.emit("listener", "two")
    assert trace == [
        "v1:start",
        "v1:listener:one",
        "v2:start",
        "v2:listener:two",  # only the live subscription answered
    ]
    assert stale_calls == ["v1"]  # the replaced listener is gone, exactly once

    # The dead context refuses new work instead of silently joining the new epoch.
    with pytest.raises(StaleEpochError):
        first_context.listener(lambda payload: None)
    with pytest.raises(StaleEpochError):
        first_context.provide("late", 1)
    with pytest.raises(StaleEpochError):
        first_context.effect(lambda: lambda: None)
    with pytest.raises(StaleEpochError):
        first_context.set("late", 1)
    with pytest.raises(StaleEpochError):
        first_context.emit("late")

    # Scheduled work belongs to the generation that scheduled it, exactly like
    # the subscription: a replaced epoch's task reference is released and the
    # task itself can never run.
    ticks: list[str] = []
    long_task = reloader.schedule("listener", 1_000, lambda: ticks.append("old"))
    short_task = reloader.schedule("listener", 10, lambda: ticks.append("short"))
    reloader.advance(10)
    assert ticks == ["short"]
    assert short_task.fired == 1

    reloader.on_change(replace_event("listener", "app:listener", 2))
    reloader.drain()
    # first_generation + 1 was the listener swap above; this is the task epoch.
    assert reloader.generation("listener") == first_generation + 2
    assert long_task.cancelled is True and long_task.fired == 0
    assert target.history[1].tasks is False
    assert target.tasks == []
    assert reloader.advance(10_000) == 0
    assert ticks == ["short"]  # the replaced generation's task never ran
    reloader.close()


# --------------------------------------------------------------------------- #
# 8. tasks are owned by the generation that scheduled them
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 9. every resource belongs to the reloader and dies with it
# --------------------------------------------------------------------------- #


def test_close_releases_every_resource_exactly_once() -> None:
    clock = ManualClock()
    reloader = HotReloader(make_source(), clock, debounce_ms=50)
    installed = reloader.mount(
        (
            parsed("provider", "app:provider", 1),
            parsed("worker", "app:cleanup", 1),
        )
    )
    provider = installed[0]
    worker = installed[1]
    assert provider.context is not None
    assert worker.context is not None

    provider.context.listener(lambda payload: None)
    provider.context.effect(lambda: lambda: None)
    task = reloader.schedule("provider", 5_000, lambda: None)
    reloader.on_change(ChangeEvent(kind=ChangeKind.CONFIG, entry="provider", payload=9))
    assert reloader.pending_events == 1

    # worker's "gamma" cleanup raises; close() still releases everything and
    # reports the failure instead of stopping halfway.
    with pytest.raises(BaseExceptionGroup) as closing:
        reloader.close()
    assert len(closing.value.exceptions) == 1
    assert isinstance(closing.value.exceptions[0], BaseExceptionGroup)
    assert isinstance(closing.value.exceptions[0].exceptions[0], RuntimeError)

    assert reloader.closed is True
    assert reloader.generations == {}
    assert reloader.generation("provider") is None
    assert reloader.generation("worker") is None
    assert reloader.pending_events == 0
    assert clock.pending() == 0
    assert task.cancelled is True and task.fired == 0
    assert provider.context.has("value") is False
    assert provider.context.listener_count() == 0

    for generation in (provider.target, worker.target):
        generation.state.check()  # raises AssertionError if anything leaked
        assert generation._scope is None or len(generation._scope) == 0

    for index in range(1, 6):
        assert worker.target.cleanup_counts[f"effect: {index}"] == 1

    snapshots = (
        dict(provider.target.cleanup_counts),
        dict(worker.target.cleanup_counts),
    )
    reloader.close()  # idempotent: no second cleanup pass, no second error
    reloader.close()
    assert reloader.generations == {}
    assert (
        dict(provider.target.cleanup_counts),
        dict(worker.target.cleanup_counts),
    ) == snapshots


# --------------------------------------------------------------------------- #
# 10. host input is validated at the boundary
# --------------------------------------------------------------------------- #


def test_host_input_is_validated_at_the_boundary() -> None:
    clock = ManualClock()
    reloader = HotReloader(make_source(), clock, debounce_ms=10)
    (mounted,) = reloader.mount((parsed("provider", "app:provider", 1),))
    del mounted  # this test only checks the host-facing boundary

    with pytest.raises(InvalidChangeError, match="non-empty entry id"):
        ChangeEvent(kind=ChangeKind.CONFIG, entry="", payload=1)
    with pytest.raises(InvalidChangeError, match="requires a payload"):
        ChangeEvent(kind=ChangeKind.CONFIG, entry="provider")
    with pytest.raises(InvalidChangeError, match="requires a module name"):
        ChangeEvent(kind=ChangeKind.MODULE, entry="provider")
    with pytest.raises(ValueError, match="debounce_ms"):
        HotReloader(make_source(), clock, debounce_ms=-1)
    with pytest.raises(ValueError, match="delay_ms"):
        clock.call_later(-1, lambda: None)

    reloader.on_change(ChangeEvent(kind=ChangeKind.CONFIG, entry="missing", payload=1))
    reloader.drain()
    assert isinstance(reloader.errors[-1], HotReloadError)
    assert "not mounted" in str(reloader.errors[-1])

    reloader.on_change(replace_event("provider", "app:absent"))
    reloader.drain()
    assert isinstance(reloader.errors[-1], ExecutionError)
    assert (
        reloader.generation("provider") == reloader.entry("provider").target.generation
    )

    with pytest.raises(KeyError, match="no version 3"):
        reloader.source.pin("app:provider", 3)
    assert reloader.source.versions("app:provider") == (1, 2)
    reloader.source.add("app:provider", 3, provider_v2)
    reloader.source.pin("app:provider", 3)
    assert reloader.source.deployed("app:provider") == 3

    reloader.close()
    with pytest.raises(HotReloadError, match="closed"):
        reloader.on_change(
            ChangeEvent(kind=ChangeKind.CONFIG, entry="provider", payload=1)
        )
    with pytest.raises(HotReloadError, match="closed"):
        reloader.mount((parsed("provider", "app:provider", 1),))
