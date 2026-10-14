# 19 — Timer：timeout、interval 与可取消 sleep（Step 4–6）

第 01 篇建立了虚拟时钟和所有权，但还没有任何时间能力。这一篇把三个基础能力接到所有权模型上：`timeout`、`interval`、`sleep`。每接一个，就立刻固定它的失败契约。

- 上一部分：[虚拟时钟与所有权](01-clock-and-ownership.md)（Step 0–3）
- 下一部分：[throttle、debounce 与 epoch](03-throttle-debounce-and-epochs.md)（Step 7–10）

三个能力共享同一个句柄类型，因为它们的生命周期问题完全相同：

```text
谁创建 → 谁拥有 → 谁能取消 → 取消之后还能不能触发
```

## Step 4：`TimerHandle`——句柄本身就是 owner 的 effect

句柄在构造时就把自己安装到创建者的 scope 上，并记录创建时的 scope 与 epoch：

```python
        self._disposer = scope.install(self._cancel, f"timer({label})")
```

于是"取消"不需要任何额外簿记，它只是让 owner 的 disposer 提前执行：

```python
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
```

`cancel()` 和 `dispose()` 是同一个操作的两个名字。因为 `Disposer.dispose()` 幂等，重复取消、取消已完成的句柄、owner 清理与显式取消"撞车"都是安全的：

| 调用序列 | 结果 |
| --- | --- |
| `cancel(); cancel()` | 只执行一次 `_cancel` |
| `dispose(); cancel()` | 同上 |
| owner `dispose()` 触发清理 | 与显式 `cancel()` 走同一条路径 |
| 已触发完成的 timeout 再 `cancel()` | 无副作用（`_entry` 已为 `None`） |

句柄的失效判定是**三重与**：

```python
    @property
    def valid(self) -> bool:
        """Whether a fire started now would still reach the callback."""

        return (
            not self._disposed
            and self._scope is self._service.owner.effects
            and self.epoch == self._service.owner.epoch
        )
```

- `not self._disposed`：显式取消或 owner 清理；
- `self._scope is owner.effects`：owner 进了新 scope（release / dispose / restart）；
- `self.epoch == owner.epoch`：owner 进了新 epoch。

第一、二条覆盖"owner 已经 dispose/unload"；第三条覆盖"同一个 owner 经过 restart 后，旧 epoch 的定时器绝不能重新入队"。第 03 篇会用测试分别固定它们。

`TimerService` 侧只加三个小工具：创建前检查 owner 状态、把 entry 挂上 `valid` 谓词、以及统一的调用入口：

```python
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
```

`_begin` 定义了**创建契约**：owner 不是 `ACTIVE` 时抛 `InactiveEffectError`。这不只是一个断言，它把"owner 已经 dispose 之后还能创建 timer"变成一个明确的、可测试的错误，而不是悄悄创建一个永远不会被回收的资源。

`timeout` 本身只有三行：

```python
        handle = self._begin(TimerKind.TIMEOUT, callback, label)
        handle._entry = self._arm(
            handle, delay, lambda: self._fire_once(handle, callback)
        )
        return handle
```

统一的调用入口负责失败账本：

```python
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
```

这里有一个刻意的**双账本**：

| 账本 | 记录什么 | 谁保证不丢 |
| --- | --- | --- |
| `service.failures`（`TimerFailure`） | 谁（kind/label）、第几次连续失败、观察到的虚拟时间 | service 在重新抛出前记录，带 owner 归属 |
| `clock.failures`（`ClockFailure`） | 某个计划条目失败了这个事实 | 时钟的隔离边界，对所有条目一视同仁 |

service 记录后**重新抛出**，异常随后被时钟的 `_fire` 捕获。这样隔离点只有一个（时钟），而归属信息保留在 service：同一个坏 callback 既不会消失，也不会拖垮同一次 `advance` 里的其他条目。

### Checkpoint C：timeout 与 interval 的可观察行为

```python
from chapter19 import Fiber, ManualClock, TimerService

clock = ManualClock()
fiber = Fiber("demo")
fiber.activate()
timers = TimerService(clock, fiber, interval_failure_limit=2)
calls = []
h = timers.timeout(1.0, lambda: calls.append(("once", clock.now)), label="once")
tick = timers.interval(1.0, lambda: calls.append(("tick", clock.now)), label="tick")
print("start", clock.now, clock.scheduled)
clock.advance(1.0)
print("t=1", calls, "pending", clock.pending)
clock.advance(4.0)
print("t=5", calls, "pending", clock.pending, "interval calls", tick.calls)
h.cancel(); tick.cancel()
print("after cancel", clock.pending, len(fiber.effects))
```

预期输出：

```text
start 0.0 (ScheduledCall(at=1.0, label='once'), ScheduledCall(at=1.0, label='tick'))
t=1 [('once', 1.0), ('tick', 1.0)] pending 1
t=5 [('once', 1.0), ('tick', 1.0), ('tick', 5.0)] pending 1 interval calls 2
after cancel 0 0
```

从 `t=1` 到 `t=5` 之间跳过了四个周期，但 interval 只触发了一次。这不是 bug，而是 Step 2 那个 `self._now = target` 的直接后果：周期任务按 `now + period` 重新入队，跳过的 tick 被**跳过**，而不是补发。

### Checkpoint D：失败隔离

```python
from chapter19 import Fiber, ManualClock, TimerService

clock = ManualClock()
fiber = Fiber("demo")
fiber.activate()
timers = TimerService(clock, fiber)
seen = []
def boom():
    seen.append("boom")
    raise ValueError("bad tick")
timers.timeout(1.0, boom, label="boom")
timers.timeout(1.0, lambda: seen.append("healthy"), label="healthy")
clock.advance(1.0)
print("seen", seen)
print("service failures", [(f.label, str(f.error)) for f in timers.failures])
print("clock failures", [(f.label, str(f.error)) for f in clock.failures])
```

预期输出：

```text
seen ['boom', 'healthy']
service failures [('boom', 'bad tick')]
clock failures [('boom', 'bad tick')]
```

同一个时刻的两个 timeout：第一个抛异常，第二个照常执行；异常留下两条记录，没有静默消失。

## Step 5：`interval` 的失败契约

interval 与 timeout 的差别只在"触发之后还要不要再入队"，但正是这里最容易写出三种坏行为：

```text
静默消失：一次失败后不再入队，心跳永久停止，没有任何记录；
疯狂追赶：按"计划时间 + period"入队，一次大跳跃补发所有错过的 tick；
失控重试：失败后立即重试，把一次故障放大成忙循环。
```

本章选择的契约必须同时排除这三种行为，并且每条都能被测试：

| 情况 | 契约 |
| --- | --- |
| 单次触发成功 | 记录 `calls`，`consecutive_failures` 归零，按 `now + period` 重新入队 |
| 单次触发失败 | 记录 `TimerFailure`（含 `recurrence`），**继续**按 `now + period` 重新入队，并重新抛出给时钟隔离 |
| 连续失败次数达到 `interval_failure_limit`（默认 3） | 停止自己：`stop_reason = "failure_limit"`，句柄进入 `disposed`，不再入队 |
| 一次大跳跃跨过多个 period | 只触发一次（不追赶） |
| 触发后 owner 失效或已取消 | 不再入队（`handle.valid` 为假） |

实现就是这段：

```python
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
```

三个细节值得单独指出：

1. 失败路径**先重新入队，再抛出**。异常交给时钟记账和隔离，但周期本身不中断；
2. `_stop("failure_limit")` 把"停止"变成一个可观察状态，而不是"从调度表里消失"。停止原因写在句柄上，测试可以断言；
3. 成功分支用 `consecutive_failures = 0` 复位，所以"偶发失败 + 恢复"不会被误判为连续故障。

对应测试的核心断言：

```python
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
```

`recurrence` 让"第几次连续失败"进入账本：如果实现偷偷在失败后立刻重试，`recurrence` 的时间戳会挤在同一个时刻；如果实现失败后停止但不设 `stop_reason`，断言会失败。

## Step 6：可取消的 `sleep`

`sleep` 与另外两个能力不同：它没有 callback，而是**等待者**。实现必须满足两条：

- 由虚拟时钟唤醒，不能使用真实 `asyncio.sleep`；
- owner 失效时立即取消，等待者收到 `asyncio.CancelledError`，并且时钟里不残留条目。

```python
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
```

时钟到点时执行的是"唤醒"，而不是用户回调：

```python
    def _wake(self, handle: TimerHandle) -> None:
        handle._entry = None
        handle.completed = True
        waiter = handle._waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(None)
```

`_cancel` 分支负责另一条路径：owner 清理时 `waiter.cancel()`，等待者从 `await waiter` 处抛出 `CancelledError`，`finally` 再调用幂等的 `handle.dispose()`。

### 测试怎么做到"不用 sleep 也能测 sleep"

既然禁止 `asyncio.sleep`（包括 `asyncio.sleep(0)`），测试需要一个**确定性同步点**。做法是让被测试的协程在挂起之前先给测试一个信号：

```python
    started: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def sleeper() -> None:
        started.set_result(None)
        await timers.sleep(2.0)
        woke.append("woke")

    task = asyncio.create_task(sleeper())
    await started  # the sleeper is parked and its entry is registered
    assert clock.pending == 1
```

`started.set_result(None)` 之后，协程在同一个事件循环步骤里继续执行到 `await timers.sleep(2.0)` 并完成注册，测试才会被唤醒，因此 `clock.pending == 1` 是确定的，不需要"让出一下事件循环"。唤醒路径同样确定：

```python
    clock.advance(1.0)
    assert task.done() is False
    assert woke == []

    clock.advance(1.0)
    await task
    assert woke == ["woke"]
    assert clock.pending == 0
```

取消路径断言"异常 + 最终资源状态"两件事：

```python
    fiber.dispose()

    assert clock.pending == 0
    assert len(scope) == 0
    with pytest.raises(asyncio.CancelledError):
        await blocked
    assert woke == ["woke"]
```

只断言 `CancelledError` 是不够的：如果实现把 future 取消了却忘记把条目从时钟里删除，`clock.pending` 就会是 1；如果忘记把自己从 scope 上摘掉，`len(scope)` 就会是 1。两条资源断言才是生命周期测试。

### Checkpoint E：三个基础能力一起通过

```bash
cd Tutorial/19-timer/src
PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider -q -k "timeout or interval or sleep or failure or cancel"
```

预期：

```text
6 passed, 4 deselected in 0.02s
```

命中的是 timeout、interval（含失败契约）、sleep、失败隔离和两个取消相关用例，全部通过，没有 `skipped`，整轮耗时远小于 1 秒——因为没有任何真实等待。

## 与正式源码对应

`cordis-py` 没有 Timer 模块、测试或依赖，本篇同样只是候选契约。"创建资源前 owner 必须可用"对应 Fiber 生命周期与 Effect 安装路径（[`fiber.py`](../../cordis-py/src/cordis/fiber.py)、[`effect.py`](../../cordis-py/src/cordis/effect.py)）；失败隔离的正式讨论见 [`08-async-errors.md`](../../cordis-py/docs/architecture/08-async-errors.md)；同样把"什么时候"留给宿主的正式组件是 [`reloader.py`](../../cordis-py/src/cordis/loader/reloader.py)；最小 Core 不含 Timer 的表述见 [`01-overview.md`](../../cordis-py/docs/architecture/01-overview.md)。

## 与 DSH Cordis 对照与扩展边界

`timeout` / `interval` / 可取消等待 / 异常上报分别对应 DSH 的定时调用、周期任务、等待与失败上报概念，但措辞一律是"结构对应：行为契约，不是 API 对齐"。本章没有对 `@deepseek-ai/cordis-plugin-timer` 1.1.2 做 API inventory，也没有双语言行为对照；`interval` 的失败契约是**本章选择**的确定、可测的候选契约，不是逐行翻译来的既有语义。

## 本章刻意简化

callback 必须是同步的；`interval` 没有相位补偿，按 `now + period` 重排（刻意的"不追赶"）；`sleep` 不支持中途查看剩余时间；没有指数退避等更高层重试策略；失败账本是内存列表，不接 Logger。

## 检查理解

1. 如果 `_call` 捕获异常后**不重新抛出**，`clock.failures` 和 `service.failures` 会怎样？隔离点还在不在？
2. `interval` 失败后为什么必须"先重新入队再抛出"？把 `_rearm` 放到 `raise` 之后会有什么后果？
3. `sleep` 的 `finally: handle.dispose()` 在正常唤醒时是否有副作用？为什么它可以无条件执行？
4. 尝试把 `interval` 的重新入队改成 `handle._entry.at + period`（按计划时间累加）。第 01 篇哪一条 Checkpoint 的输出会改变？为什么那正是"疯狂追赶"？
