"""Lifecycle-safe time capabilities for the tutorial Cordis runtime (chapter 19).

Timers look like the easiest plugin in Cordis: wrap ``loop.call_later`` and be
done.  They are not, because a timer is a *long-lived resource owned by someone
else*.  This chapter therefore builds the smallest runtime that can answer four
questions:

* who owns the scheduled callback, and what happens when that owner is
  disposed, unloaded or restarted into a new epoch?
* what does a callback exception do to the other timers of the same owner?
* what is the exact contract of ``cancel()``/``dispose()``, and is it
  idempotent?
* how can all of this be tested without ever waiting on real time?

``ManualClock`` is the anchor: virtual monotonic seconds, synchronous firing, a
validity predicate that is re-checked immediately before every fire, and a
failure ledger so a broken callback is never silently swallowed.  Every
``TimerHandle`` is installed on the *calling* owner's ``EffectScope``, so
``Fiber.dispose()``, ``release()`` or ``restart()`` makes the old callback
unreachable without any cooperation from the code that created it.

The module imports only the standard library and never imports ``cordis`` or
another tutorial chapter.

``cordis-py`` currently ships **no** Timer module, test or dependency, so
nothing here is evidence of an official Timer implementation or of DSH
``@deepseek-ai/cordis-plugin-timer`` 1.1.2 equivalence.  This is a *candidate
contract* for a future official Timer package.
"""

from __future__ import annotations

import asyncio
import heapq
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

Callback = Callable[[], object]

__all__ = [
    "Callback",
    "ClockEntry",
    "ClockFailure",
    "Disposer",
    "EffectScope",
    "Fiber",
    "FiberState",
    "InactiveEffectError",
    "ManualClock",
    "ScheduledCall",
    "TimerFailure",
    "TimerHandle",
    "TimerKind",
    "TimerService",
]


class InactiveEffectError(RuntimeError):
    """Raised when a non-active owner tries to create a new timer."""


# --------------------------------------------------------------------------- #
# Minimal ownership: disposer, scope, epoch-changing owner
# --------------------------------------------------------------------------- #


class Disposer:
    """Idempotent cleanup handle owned by exactly one ``EffectScope``.

    Chapter 17's disposer is awaitable.  Timer cancellation needs no ``await``
    at all, so this chapter uses the synchronous form and keeps the same
    guarantee: the effect runs at most once, no matter who calls it.  Disposing
    also removes the handle from its scope, so ``len(scope)`` stays an accurate
    count of resources that are still installed.
    """

    def __init__(self, scope: EffectScope, execute: Callable[[], None], label: str):
        self.label = label
        self._scope = scope
        self._execute = execute
        self._disposed = False

    @property
    def disposed(self) -> bool:
        return self._disposed

    def __call__(self) -> None:
        self.dispose()

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._scope._forget(self)
        self._execute()


class EffectScope:
    """Ordered cleanup stack: reverse order, idempotent, fully drained."""

    def __init__(self) -> None:
        self._items: list[Disposer] = []

    def install(self, execute: Callable[[], None], label: str) -> Disposer:
        disposer = Disposer(self, execute, label)
        self._items.append(disposer)
        return disposer

    def __len__(self) -> int:
        return len(self._items)

    def _forget(self, disposer: Disposer) -> None:
        try:
            self._items.remove(disposer)
        except ValueError:  # already drained by EffectScope.dispose()
            pass

    def dispose(self) -> None:
        errors: list[BaseException] = []
        for disposer in reversed(list(self._items)):
            try:
                disposer.dispose()
            except BaseException as error:  # noqa: BLE001 - keep cleaning up
                errors.append(error)
        self._items.clear()
        if errors:
            raise BaseExceptionGroup("timer effect cleanup failed", errors)


class FiberState(StrEnum):
    """Portable owner states; only ``ACTIVE`` may create timers."""

    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    DISPOSED = "DISPOSED"


class Fiber:
    """Minimal plugin-style owner: one effect scope per lifecycle epoch.

    Every state change opens a new epoch *and* a new scope.  Handles remember
    the scope and epoch they were created in, so a disposed owner, a released
    owner and a restarted owner all invalidate the previous generation.  The
    epoch check is what stops a periodic timer from resurrecting itself after
    its own callback restarted the owner.
    """

    def __init__(self, name: str = "fiber") -> None:
        self.name = name
        self.state = FiberState.PENDING
        self.epoch = 0
        self.effects = EffectScope()

    @property
    def active(self) -> bool:
        return self.state is FiberState.ACTIVE

    def activate(self) -> None:
        """Enter (or re-enter) the active state in a fresh epoch."""

        if self.state is FiberState.DISPOSED:
            raise InactiveEffectError(f"cannot activate disposed fiber {self.name!r}")
        if self.state is FiberState.ACTIVE:
            return
        self._open_epoch(FiberState.ACTIVE)

    def release(self) -> None:
        """Drop every owned resource because a dependency went away."""

        if self.state in (FiberState.INACTIVE, FiberState.DISPOSED):
            return
        self._open_epoch(FiberState.INACTIVE)

    def restart(self) -> None:
        """Reload one owner without disposing it: release then activate."""

        self.release()
        self.activate()

    def dispose(self) -> None:
        """Terminal state; never activates again."""

        if self.state is FiberState.DISPOSED:
            return
        self._open_epoch(FiberState.DISPOSED)

    def _open_epoch(self, state: FiberState) -> None:
        previous = self.effects
        self.state = state
        self.epoch += 1
        self.effects = EffectScope()
        previous.dispose()


# --------------------------------------------------------------------------- #
# ManualClock: virtual time, synchronous firing, explicit validity
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ScheduledCall:
    """Read-only snapshot of one entry that is still able to fire."""

    at: float
    label: str


@dataclass(frozen=True, slots=True)
class ClockFailure:
    """One callback failure caught by the clock's isolation boundary."""

    label: str
    error: BaseException
    at: float


@dataclass(slots=True)
class ClockEntry:
    """One scheduled call, validated again right before each fire."""

    at: float
    callback: Callback
    label: str
    valid: Callable[[], bool] | None = None
    cancelled: bool = False
    completed: bool = False

    def cancel(self) -> None:
        self.cancelled = True


class ManualClock:
    """A deterministic monotonic clock; real time is never involved.

    ``advance`` jumps the clock to the target time *first* and then fires every
    due entry in scheduled-time order.  Because callbacks observe the target as
    "now", a periodic timer that re-arms at ``now + period`` silently skips the
    ticks it missed instead of firing a catch-up burst.
    """

    def __init__(self) -> None:
        self._now = 0.0
        self._queue: list[tuple[float, int, ClockEntry]] = []
        self._sequence = 0
        self.failures: list[ClockFailure] = []

    @property
    def now(self) -> float:
        """Current virtual time in seconds, monotonic and never negative."""

        return self._now

    @property
    def pending(self) -> int:
        """Number of entries that would still fire right now."""

        return sum(1 for entry in self._live())

    @property
    def scheduled(self) -> tuple[ScheduledCall, ...]:
        """Live entries as an ordered snapshot, earliest first."""

        live = sorted(self._live(), key=lambda entry: entry.at)
        return tuple(ScheduledCall(entry.at, entry.label) for entry in live)

    def schedule(
        self,
        delay: float,
        callback: Callback,
        *,
        label: str = "call",
        valid: Callable[[], bool] | None = None,
    ) -> ClockEntry:
        """Queue ``callback`` for ``now + delay`` seconds of virtual time."""

        if delay < 0:
            raise ValueError("cannot schedule a call in the past")
        entry = ClockEntry(self._now + delay, callback, label, valid)
        heapq.heappush(self._queue, (entry.at, self._sequence, entry))
        self._sequence += 1
        return entry

    def advance(self, seconds: float) -> float:
        """Move virtual time forward and synchronously fire due entries."""

        if seconds < 0:
            raise ValueError("cannot advance a monotonic clock backwards")
        target = self._now + seconds
        self._now = target
        while self._queue and self._queue[0][0] <= target:
            _, _, entry = heapq.heappop(self._queue)
            if not self._is_live(entry):
                continue
            self._fire(entry)
        return self._now

    def _live(self) -> list[ClockEntry]:
        return [entry for _, _, entry in self._queue if self._is_live(entry)]

    @staticmethod
    def _is_live(entry: ClockEntry) -> bool:
        if entry.cancelled or entry.completed:
            return False
        return entry.valid is None or entry.valid()

    def _fire(self, entry: ClockEntry) -> None:
        """Isolation boundary: one bad callback never stops the next entry."""

        try:
            entry.callback()
        except BaseException as error:  # noqa: BLE001 - reported, never silent
            self.failures.append(ClockFailure(entry.label, error, self._now))
        finally:
            entry.completed = True


# --------------------------------------------------------------------------- #
# Timer service: owner-bound handles over one shared clock
# --------------------------------------------------------------------------- #


class TimerKind(StrEnum):
    """Which timer shape produced a handle or a failure record."""

    TIMEOUT = "timeout"
    INTERVAL = "interval"
    SLEEP = "sleep"
    THROTTLE = "throttle"
    DEBOUNCE = "debounce"


@dataclass(frozen=True, slots=True)
class TimerFailure:
    """Owner-attributed record of one failing timer callback."""

    kind: TimerKind
    label: str
    error: BaseException
    at: float
    recurrence: int = 1


class TimerHandle:
    """One timer created by one owner; ``cancel``/``dispose`` are idempotent.

    The handle itself is registered on the creating scope, so the owner can
    revoke it without knowing which timer it is.  ``valid`` is the conjunction
    of three independent guards: the handle was not cancelled, the creating
    scope is still the owner's current scope, and the creating epoch is still
    the owner's current epoch.
    """

    def __init__(
        self,
        service: TimerService,
        scope: EffectScope,
        epoch: int,
        kind: TimerKind,
        label: str,
    ) -> None:
        self._service = service
        self._scope = scope
        self.epoch = epoch
        self.kind = kind
        self.label = label
        self.calls = 0
        self.failures = 0
        self.consecutive_failures = 0
        self.suppressed = 0
        self.completed = False
        self.stop_reason: str | None = None
        self.period: float | None = None
        self.last_call: float | None = None
        self._entry: ClockEntry | None = None
        self._waiter: asyncio.Future[None] | None = None
        self._invoke: Callable[[], None] | None = None
        self._disposed = False
        self._disposer = scope.install(self._cancel, f"timer({label})")

    def __call__(self) -> None:
        """Invoke a throttle/debounce handle; a stale handle is a no-op."""

        if self._invoke is None:
            raise TypeError(f"{self.kind} handle is not directly callable")
        if not self.valid:
            return
        self._invoke()

    @property
    def valid(self) -> bool:
        """Whether a fire started now would still reach the callback."""

        return (
            not self._disposed
            and self._scope is self._service.owner.effects
            and self.epoch == self._service.owner.epoch
        )

    @property
    def disposed(self) -> bool:
        return self._disposed

    @property
    def pending(self) -> bool:
        """Whether a clock entry is currently armed for this handle."""

        return self._entry is not None and not self._entry.cancelled

    def cancel(self) -> None:
        """Revoke the timer; repeated calls are no-ops."""

        self._disposer.dispose()

    def dispose(self) -> None:
        """Alias of ``cancel`` so ownership code can treat all effects alike."""

        self._disposer.dispose()

    def _cancel(self) -> None:
        self._disposed = True
        entry = self._entry
        self._entry = None
        if entry is not None:
            entry.cancel()
        waiter = self._waiter
        if waiter is not None and not waiter.done():
            waiter.cancel()

    def _stop(self, reason: str) -> None:
        if self.stop_reason is None:
            self.stop_reason = reason
        self.dispose()


class TimerService:
    """Owner-bound facade over one shared ``ManualClock``.

    A real Cordis plugin receives an owner through its calling ``Context``; the
    teaching version makes that caller explicit as a constructor argument so
    the five public methods keep the canonical signatures.
    """

    def __init__(
        self,
        clock: ManualClock,
        owner: Fiber,
        *,
        interval_failure_limit: int = 3,
    ) -> None:
        if interval_failure_limit < 1:
            raise ValueError("interval_failure_limit must be at least 1")
        self.clock = clock
        self.owner = owner
        self.interval_failure_limit = interval_failure_limit
        self.failures: list[TimerFailure] = []

    # -- public API -------------------------------------------------------- #

    def timeout(
        self,
        delay: float,
        callback: Callback,
        *,
        label: str | None = None,
    ) -> TimerHandle:
        """Fire ``callback`` once, ``delay`` virtual seconds from now."""

        handle = self._begin(TimerKind.TIMEOUT, callback, label)
        handle._entry = self._arm(
            handle, delay, lambda: self._fire_once(handle, callback)
        )
        return handle

    def interval(
        self,
        period: float,
        callback: Callback,
        *,
        label: str | None = None,
    ) -> TimerHandle:
        """Fire ``callback`` once per ``period`` until cancelled or stopped.

        Contract on failure: the failing tick is recorded and re-raised to the
        clock's isolation boundary, the periodic cadence continues *without*
        catch-up, and ``interval_failure_limit`` consecutive failures stop the
        handle with ``stop_reason == "failure_limit"``.
        """

        if period <= 0:
            raise ValueError("interval period must be positive")
        handle = self._begin(TimerKind.INTERVAL, callback, label)
        handle.period = period
        handle._entry = self._arm(
            handle, period, lambda: self._fire_interval(handle, callback)
        )
        return handle

    async def sleep(self, delay: float) -> None:
        """Await ``delay`` virtual seconds; cancelled when the owner is revoked.

        No real time is used: the coroutine parks on a bare ``Future`` that the
        clock resolves during ``advance``.  If the owning scope is disposed
        first, the future is cancelled and this coroutine raises
        ``asyncio.CancelledError``.
        """

        handle = self._begin(TimerKind.SLEEP, None, "sleep")
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        handle._waiter = waiter
        handle._entry = self._arm(handle, delay, lambda: self._wake(handle))
        try:
            await waiter
        finally:
            handle.dispose()

    def throttle(
        self,
        window: float,
        callback: Callback,
        *,
        label: str | None = None,
    ) -> TimerHandle:
        """Leading-edge throttle: at most one call per ``window`` seconds."""

        if window < 0:
            raise ValueError("throttle window must not be negative")
        handle = self._begin(TimerKind.THROTTLE, callback, label)

        def invoke() -> None:
            now = self.clock.now
            last = handle.last_call
            if last is not None and now - last < window:
                handle.suppressed += 1
                return
            handle.last_call = now
            self._call(handle, callback)

        handle._invoke = invoke
        return handle

    def debounce(
        self,
        delay: float,
        callback: Callback,
        *,
        label: str | None = None,
    ) -> TimerHandle:
        """Trailing-edge debounce: run once ``delay`` seconds after the last call."""

        handle = self._begin(TimerKind.DEBOUNCE, callback, label)

        def invoke() -> None:
            handle.completed = False
            entry = handle._entry
            if entry is not None:
                entry.cancel()
            handle._entry = self._arm(
                handle, delay, lambda: self._fire_once(handle, callback)
            )

        handle._invoke = invoke
        return handle

    # -- internals --------------------------------------------------------- #

    def _begin(
        self,
        kind: TimerKind,
        callback: Callback | None,
        label: str | None,
    ) -> TimerHandle:
        if not self.owner.active:
            raise InactiveEffectError(
                f"cannot create {kind} on inactive owner "
                f"{self.owner.name!r} ({self.owner.state})"
            )
        name = label or getattr(callback, "__name__", "callback")
        return TimerHandle(self, self.owner.effects, self.owner.epoch, kind, name)

    def _arm(
        self,
        handle: TimerHandle,
        delay: float,
        callback: Callback,
    ) -> ClockEntry:
        return self.clock.schedule(
            delay, callback, label=handle.label, valid=lambda: handle.valid
        )

    def _rearm(self, handle: TimerHandle, callback: Callback) -> None:
        assert handle.period is not None
        handle._entry = self._arm(
            handle, handle.period, lambda: self._fire_interval(handle, callback)
        )

    def _record(self, handle: TimerHandle, error: BaseException) -> None:
        self.failures.append(
            TimerFailure(
                handle.kind,
                handle.label,
                error,
                self.clock.now,
                handle.consecutive_failures or 1,
            )
        )

    def _call(self, handle: TimerHandle, callback: Callback) -> None:
        """Run a non-periodic callback; record and re-raise on failure."""

        handle.calls += 1
        try:
            callback()
        except BaseException as error:
            handle.failures += 1
            handle.consecutive_failures += 1
            self._record(handle, error)
            raise
        handle.consecutive_failures = 0

    def _fire_once(self, handle: TimerHandle, callback: Callback) -> None:
        handle._entry = None
        try:
            self._call(handle, callback)
        finally:
            handle.completed = True

    def _fire_interval(self, handle: TimerHandle, callback: Callback) -> None:
        handle._entry = None
        handle.calls += 1
        try:
            callback()
        except BaseException as error:
            handle.failures += 1
            handle.consecutive_failures += 1
            self._record(handle, error)
            if handle.consecutive_failures >= self.interval_failure_limit:
                handle._stop("failure_limit")
            if handle.valid:
                self._rearm(handle, callback)
            raise
        handle.consecutive_failures = 0
        if handle.valid:
            self._rearm(handle, callback)

    def _wake(self, handle: TimerHandle) -> None:
        handle._entry = None
        handle.completed = True
        waiter = handle._waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(None)
