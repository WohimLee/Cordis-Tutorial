"""Behaviour tests for the chapter 19 timer runtime.

Every test drives virtual time through ``ManualClock``; no test ever calls
``asyncio.sleep``, not even ``asyncio.sleep(0)``.  Where a test needs a
suspension point it uses a bare ``Future`` that the coroutine under test
resolves before parking, which makes the ordering deterministic.
"""

from __future__ import annotations

import asyncio

import pytest
from chapter19 import (
    Fiber,
    FiberState,
    InactiveEffectError,
    ManualClock,
    ScheduledCall,
    TimerKind,
    TimerService,
)


def make_timers(
    interval_failure_limit: int = 3,
) -> tuple[ManualClock, Fiber, TimerService]:
    """Return a live owner wired to one shared manual clock."""

    clock = ManualClock()
    fiber = Fiber("owner")
    fiber.activate()
    timers = TimerService(clock, fiber, interval_failure_limit=interval_failure_limit)
    return clock, fiber, timers


def test_timeout_fires_once_and_the_clock_is_queryable() -> None:
    clock, fiber, timers = make_timers()
    calls: list[str] = []
    handle = timers.timeout(2.0, lambda: calls.append("beat"), label="heartbeat")

    assert clock.now == 0.0
    assert clock.pending == 1
    assert clock.scheduled == (ScheduledCall(2.0, "heartbeat"),)
    assert len(fiber.effects) == 1  # the handle lives on the owner's scope

    clock.advance(1.0)
    assert calls == []
    assert handle.completed is False

    clock.advance(1.0)
    assert calls == ["beat"]
    assert handle.completed is True
    assert handle.pending is False
    assert clock.pending == 0

    clock.advance(10.0)  # a one-shot timeout never repeats
    assert calls == ["beat"]
    assert clock.failures == []


def test_disposing_the_owner_kills_callbacks_and_blocks_new_timers() -> None:
    clock, fiber, timers = make_timers()
    calls: list[str] = []
    scope = fiber.effects
    first = timers.timeout(1.0, lambda: calls.append("first"), label="first")
    second = timers.interval(1.0, lambda: calls.append("second"), label="second")
    assert len(scope) == 2
    assert clock.pending == 2

    fiber.dispose()

    assert fiber.state is FiberState.DISPOSED
    assert len(scope) == 0  # the old scope is drained, not just bypassed
    assert clock.pending == 0
    assert first.disposed is True and second.disposed is True
    assert first.valid is False and second.valid is False

    clock.advance(100.0)
    assert calls == []

    with pytest.raises(InactiveEffectError, match="cannot create timeout"):
        timers.timeout(1.0, lambda: None)

    # an owner that was never activated, or was released, is inactive too
    idle = Fiber("idle")
    with pytest.raises(InactiveEffectError, match="cannot create interval"):
        TimerService(clock, idle).interval(1.0, lambda: None)

    released = Fiber("released")
    released.activate()
    released.release()
    with pytest.raises(InactiveEffectError):
        TimerService(clock, released).timeout(1.0, lambda: None)


def test_cancel_and_dispose_are_idempotent_and_stop_firing() -> None:
    clock, fiber, timers = make_timers()
    calls: list[str] = []
    handle = timers.timeout(1.0, lambda: calls.append("x"), label="x")

    handle.cancel()
    handle.cancel()
    handle.dispose()
    assert handle.disposed is True
    assert handle.valid is False
    assert clock.pending == 0
    assert len(fiber.effects) == 0

    clock.advance(5.0)
    assert calls == []

    # cancelling an already completed handle is a no-op as well
    done = timers.timeout(0.0, lambda: calls.append("y"), label="y")
    clock.advance(0.0)
    assert calls == ["y"]
    done.dispose()
    done.cancel()
    assert calls == ["y"]
    assert clock.pending == 0


def test_interval_ticks_once_per_advance_without_catch_up() -> None:
    clock, fiber, timers = make_timers()
    ticks: list[float] = []
    handle = timers.interval(1.0, lambda: ticks.append(clock.now), label="tick")
    assert clock.scheduled == (ScheduledCall(1.0, "tick"),)

    clock.advance(1.0)
    assert ticks == [1.0]

    clock.advance(4.0)  # four periods pass in one jump
    assert ticks == [1.0, 5.0]  # exactly one tick, stamped with the new now
    assert handle.calls == 2
    assert clock.pending == 1

    clock.advance(1.0)
    assert ticks == [1.0, 5.0, 6.0]

    handle.dispose()
    assert clock.pending == 0
    clock.advance(10.0)
    assert ticks == [1.0, 5.0, 6.0]
    assert len(fiber.effects) == 0


def test_interval_failure_contract_records_and_stops_at_limit() -> None:
    clock, _fiber, timers = make_timers(interval_failure_limit=2)
    attempts: list[float] = []

    def boom() -> None:
        attempts.append(clock.now)
        raise RuntimeError("periodic failure")

    handle = timers.interval(1.0, boom, label="boom")

    clock.advance(1.0)
    assert attempts == [1.0]
    assert handle.failures == 1
    assert handle.consecutive_failures == 1
    assert handle.disposed is False
    assert clock.pending == 1  # the failing tick keeps its cadence

    clock.advance(1.0)
    assert attempts == [1.0, 2.0]
    assert handle.failures == 2
    assert handle.stop_reason == "failure_limit"
    assert handle.disposed is True
    assert clock.pending == 0

    clock.advance(100.0)
    assert attempts == [1.0, 2.0]  # no catch-up burst, no silent repeats

    assert [failure.recurrence for failure in timers.failures] == [1, 2]
    assert all(failure.kind is TimerKind.INTERVAL for failure in timers.failures)
    assert all(isinstance(failure.error, RuntimeError) for failure in timers.failures)
    assert len(clock.failures) == 2  # the clock saw the same two failures

    # one failure followed by successes resets the consecutive counter
    flaky_calls: list[float] = []

    def flaky() -> None:
        flaky_calls.append(clock.now)
        if len(flaky_calls) == 1:
            raise RuntimeError("first tick fails")

    flaky_handle = timers.interval(1.0, flaky, label="flaky")
    clock.advance(1.0)
    clock.advance(1.0)
    clock.advance(1.0)
    assert flaky_handle.calls == 3
    assert flaky_handle.failures == 1
    assert flaky_handle.consecutive_failures == 0
    assert flaky_handle.disposed is False
    assert clock.pending == 1


def test_failing_callback_is_isolated_and_reported() -> None:
    clock, fiber, timers = make_timers()
    seen: list[str] = []
    ticks: list[float] = []

    def boom() -> None:
        seen.append("boom")
        raise ValueError("callback failed")

    failing = timers.timeout(1.0, boom, label="boom")
    healthy = timers.timeout(1.0, lambda: seen.append("healthy"), label="healthy")
    periodic = timers.interval(1.0, lambda: ticks.append(clock.now), label="periodic")

    clock.advance(1.0)

    assert seen == ["boom", "healthy"]  # the sibling timeout still ran
    assert ticks == [1.0]  # the interval still ran and re-armed
    assert failing.failures == 1
    assert failing.completed is True
    assert healthy.failures == 0
    assert healthy.completed is True
    assert periodic.pending is True
    assert clock.pending == 1

    assert len(timers.failures) == 1
    failure = timers.failures[0]
    assert failure.kind is TimerKind.TIMEOUT
    assert failure.label == "boom"
    assert str(failure.error) == "callback failed"

    assert len(clock.failures) == 1  # reported at the transport layer too
    assert clock.failures[0].label == "boom"
    assert isinstance(clock.failures[0].error, ValueError)
    assert fiber.state is FiberState.ACTIVE  # a failure never kills the owner

    clock.advance(1.0)
    assert ticks == [1.0, 2.0]  # the surviving interval keeps working
    assert len(timers.failures) == 1


def test_throttle_keeps_the_leading_edge_within_the_window() -> None:
    clock, fiber, timers = make_timers()
    calls: list[float] = []
    handle = timers.throttle(1.0, lambda: calls.append(clock.now), label="save")

    handle()
    handle()
    clock.advance(0.5)
    handle()
    assert calls == [0.0]
    assert handle.suppressed == 2
    assert clock.pending == 0  # throttle schedules nothing

    clock.advance(0.5)  # now == 1.0, the window has elapsed
    handle()
    assert calls == [0.0, 1.0]
    assert clock.pending == 0

    fiber.release()  # a released owner revokes the throttle handle
    handle()
    assert calls == [0.0, 1.0]
    assert handle.valid is False
    assert clock.pending == 0


def test_debounce_keeps_the_trailing_call_and_cancels_it() -> None:
    clock, fiber, timers = make_timers()
    calls: list[float] = []
    handle = timers.debounce(1.0, lambda: calls.append(clock.now), label="search")

    handle()
    clock.advance(0.5)
    handle()  # pushes the trailing call to 1.5
    clock.advance(0.5)  # now == 1.0, still pending
    assert calls == []
    assert clock.pending == 1
    assert clock.scheduled == (ScheduledCall(1.5, "search"),)

    clock.advance(0.5)  # now == 1.5
    assert calls == [1.5]
    assert handle.completed is True
    assert clock.pending == 0

    handle()  # the same handle can serve a new burst
    clock.advance(1.0)
    assert calls == [1.5, 2.5]

    handle()
    assert clock.pending == 1
    handle.dispose()  # cancels the pending trailing call
    clock.advance(5.0)
    assert calls == [1.5, 2.5]
    assert clock.pending == 0
    assert handle.valid is False
    assert len(fiber.effects) == 0


def test_restart_opens_a_new_epoch_and_stale_timers_die() -> None:
    clock, fiber, timers = make_timers()
    trace: list[str] = []
    epoch_one = fiber.epoch
    scope_one = fiber.effects

    stale = timers.timeout(5.0, lambda: trace.append("stale"), label="stale")
    raw = clock.schedule(
        5.0,
        lambda: trace.append("raw"),
        label="raw",
        valid=lambda: fiber.epoch == epoch_one,
    )
    assert clock.pending == 2

    def restart_owner() -> None:
        trace.append("tick")
        fiber.restart()

    periodic = timers.interval(1.0, restart_owner, label="periodic")
    clock.advance(1.0)

    assert trace == ["tick"]
    assert fiber.epoch == epoch_one + 2  # release + activate
    assert len(scope_one) == 0
    assert periodic.epoch == epoch_one
    assert periodic.valid is False
    assert stale.disposed is True and stale.valid is False
    assert raw.cancelled is False  # still queued, but no longer able to fire
    assert clock.pending == 0  # nothing was resurrected by the periodic tick

    clock.advance(100.0)
    assert trace == ["tick"]  # no old-epoch callback ever ran

    fresh = timers.timeout(1.0, lambda: trace.append("fresh"), label="fresh")
    assert fresh.epoch == fiber.epoch
    clock.advance(1.0)
    assert trace == ["tick", "fresh"]
    assert clock.pending == 0


@pytest.mark.asyncio
async def test_sleep_wakes_on_advance_and_owner_dispose_cancels_it() -> None:
    clock, fiber, timers = make_timers()
    woke: list[str] = []
    started: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def sleeper() -> None:
        started.set_result(None)
        await timers.sleep(2.0)
        woke.append("woke")

    task = asyncio.create_task(sleeper())
    await started  # the sleeper is parked and its entry is registered
    assert clock.pending == 1
    assert clock.scheduled == (ScheduledCall(2.0, "sleep"),)

    clock.advance(1.0)
    assert task.done() is False
    assert woke == []

    clock.advance(1.0)
    await task
    assert woke == ["woke"]
    assert clock.pending == 0

    scope = fiber.effects
    blocked_started: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def blocked_sleeper() -> None:
        blocked_started.set_result(None)
        await timers.sleep(5.0)
        woke.append("never")

    blocked = asyncio.create_task(blocked_sleeper())
    await blocked_started
    assert clock.pending == 1

    fiber.dispose()

    assert clock.pending == 0
    assert len(scope) == 0
    with pytest.raises(asyncio.CancelledError):
        await blocked
    assert woke == ["woke"]
