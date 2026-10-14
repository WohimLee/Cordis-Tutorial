# 19 — Timer：虚拟时钟与所有权（Step 0–3）

第 18 章结束时，运行栈已经能做三件事：Loader 把配置声明变成 Fiber 树，Include 把多个文件组合成一棵树，失败时不扰动旧树。但整棵栈仍然**没有时间**。没有时间就没有心跳、没有超时、没有合并抖动、没有"延迟到安静之后再做一次"。

给 runtime 加时间看起来是最简单的一章：包一个 `loop.call_later` 就结束了。真正的难点不在计时，而在**所有权**。三个具体的失败场景会立刻出现：

```text
1) 测试依赖真实时间：一次 timeout 测试要真的等 2 秒，抖动、慢、不可复现；
2) owner 已经 dispose/unload，旧 callback 仍然执行：
   插件被替换后，旧 epoch 的定时器还在往新的服务里写数据；
3) callback 抛异常：要么被静默吞掉（再也没人知道心跳停了），
   要么炸断同一次时间推进里的其他 timer（一个坏回调拖垮同一个 owner）。
```

这一章围绕这三个失败场景建立最小实现。它不是"再写一个调度器"，而是回答四个问题：

```text
谁拥有这个 callback？
owner 消失、卸载、重启成新 epoch 之后，旧 callback 怎么失效？
callback 失败时，隔离边界在哪里，异常去哪里？
怎样在不真实等待的前提下测试上面全部内容？
```

最后一条是形式要求，也是设计约束：如果测试必须真实等待，说明时间被放在了错误的位置。

## 本章目标

- 用 `ManualClock` 把"时间"变成可查询、可推进、可断言的虚拟量；
- 用一个最小所有权模型（`Disposer` / `EffectScope` / `Fiber`）让每个 timer 句柄挂在**创建它的调用方**身上；
- 定义并实现 `timeout`、`interval`、可取消的 `sleep`、`throttle`、`debounce` 五类时间能力；
- 明确 callback 失败、取消/重复取消、dispose 后再创建、owner 重启（新 epoch）四类边界契约；
- 让所有测试都由虚拟时钟驱动，**不出现任何真实 sleep**。

教学实现自包含在 [`src/chapter19.py`](src/chapter19.py)，只导入标准库，不导入 [`cordis`](../../cordis-py/src/cordis)，也不导入第 17、18 章的教学文件。测试在 [`src/test_chapter19.py`](src/test_chapter19.py)。

## Step 0：先确认正式实现里没有 Timer

跟随本章之前，先确认真实事实，避免把"教程定义的契约"误当成"正式实现已经有的东西"：

```bash
grep -ri "timer" cordis-py/src cordis-py/tests cordis-py/pyproject.toml
```

这条命令**没有匹配**。`cordis-py` 当前没有 Timer 模块、没有 Timer 测试、也没有 Timer 依赖。文档层面也是同一结论：

- [`docs/architecture/01-overview.md`](../../cordis-py/docs/architecture/01-overview.md) 明确把 Timer 列为"可选官方插件，不进入最小核心"；
- [`docs/cordis-core-compatibility.md`](../../cordis-py/docs/cordis-core-compatibility.md) 说明 Loader / Include / HMR / Timer 这些独立包**不在** Core 对照范围内；
- [`docs/architecture/02-core-model.md`](../../cordis-py/docs/architecture/02-core-model.md) 的所有权树里出现的 `└── Effect: timer` 只是"一个 timer 应该长在 Fiber 的 Effect 上"的示意，不是已实现的模块。

所以本章的定位必须写清楚：

> 本章的 Timer 是**教程定义的候选契约（candidate contract）**。它描述"未来若要进入正式实现，应当满足哪些可观察行为"，而不是对现有 `cordis-py` 的对照，也不是 DSH `@deepseek-ai/cordis-plugin-timer` 1.1.2 的等价证据。

正式源码里唯一与"时间/重载"相邻的真实文件是 host-driven 的 [`ConfigReloader`](../../cordis-py/src/cordis/loader/reloader.py)：它只提供 `start()` / `reload()`，由宿主决定何时调用，既没有 watcher 也没有自己的时间源。Timer 必须自己解决所有权，这一点两处是一致的。

## Step 1：先写会失败的行为测试

目标不是"实现一个时钟"，而是"让 time 变成可断言的行为"。所以第一条测试描述行为，不描述内部结构：

```python
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
```

这条测试一次性表达了本章一半的设计约束：

- 时间由 `clock.now` / `clock.advance` 驱动，测试自己决定时间走到哪里；
- 调度状态是**可查询**的：`clock.pending` 是"还能触发的条目数"，`clock.scheduled` 是快照；
- 句柄挂在 owner 的 effect scope 上（`len(fiber.effects) == 1`），因此 owner 是唯一的回收入口；
- 一次性 timeout 触发后不会重复，也不会残留。

此时运行它必然会失败，因为 `chapter19` 还不存在：

```bash
uv run pytest Tutorial/19-timer/src/test_chapter19.py -k timeout
```

预期：

```text
E   ModuleNotFoundError: No module named 'chapter19'
```

失败原因不是"时钟写得不对"，而是"还没有任何可以推进的时间"。下一步只加最小的时间载体。

## Step 2：`ManualClock`——虚拟时间与同步触发

`ManualClock` 只做四件事：记录虚拟时间、接受延迟调度、推进时间并同步触发、查询还能触发的条目。数据部分极小：

```python
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
```

`valid` 是一个**每次触发前重新求值的谓词**。这是整章的关键机制：句柄被取消、owner 换了 scope、owner 进了新 epoch，都会让谓词变假，时钟在真正调用 callback 之前就会跳过这个条目。用惰性跳过而不是从堆里删除，是为了让取消路径不依赖堆的内部位置：

```python
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
```

推进时间是本章的第一个语义决定：

```python
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
```

注意 `self._now = target` 出现在触发循环**之前**。这不是笔误：

| 决定 | 后果 |
| --- | --- |
| 先把时钟跳到目标时刻 | callback 里读到的 `clock.now` 是推进后的时间 |
| 再按计划时间顺序触发到期条目 | 同一个 `advance` 内顺序确定（先到期先触发） |
| 周期任务在触发后按 `now + period` 重新入队 | 一次大跳跃只触发一次，不做"追赶爆发" |

最后一条正是第 02 篇 interval 失败契约的一半。剩下两个小方法把"取消"和"不静默"补齐：

```python
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
```

`_fire` 是**隔离边界**：异常被记录进 `clock.failures` 并继续触发下一个条目。它既不是静默吞掉（有账本），也不是让一个坏 callback 炸断整次时间推进。

### Checkpoint A：时钟可以独立运行

此时还没有 TimerService，但时钟本身已经可以演示。运行（在 `Tutorial/19-timer/src` 下）：

```python
from chapter19 import ManualClock

clock = ManualClock()
clock.schedule(2.0, lambda: print("tick at", clock.now), label="tick")
print("now", clock.now, "pending", clock.pending, "scheduled", clock.scheduled)
clock.advance(5.0)
print("now", clock.now, "pending", clock.pending)
```

预期输出：

```text
now 0.0 pending 1 scheduled (ScheduledCall(at=2.0, label='tick'),)
tick at 5.0
now 5.0 pending 0
```

`tick at 5.0` 就是"一次跳跃不追赶"的直接证据：条目计划在 2.0 触发，但 callback 观察到的时间是推进后的 5.0。此时 `test_timeout...` 仍然失败，因为 `TimerService` 还不存在。下一件事不是继续加功能，而是先确定"谁拥有 timer"。

## Step 3：所有权——`Disposer`、`EffectScope` 与 epoch

第 17 章已经建立了"每个副作用都有 owner"的不变量：Effect 属于 Fiber，销毁 Fiber 就递归撤销整棵子树。本章只保留这个模型的最小骨架。`Disposer` 是可幂等的清理句柄，并且**在 dispose 时把自己从 scope 中移除**，于是 `len(scope)` 始终等于"仍然安装着的资源数"，这是一个可以写进断言的强不变量：

```python
    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._scope._forget(self)
        self._execute()
```

`EffectScope` 按安装的逆序清理，并且即使某个 disposer 抛异常也继续清理其余的：

```python
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
```

owner 是"Fiber 风格"的调用方。它比第 17 章的 Fiber 小得多，只保留本章真正需要的部分：状态、epoch、当前 effect scope。核心决定是**每次状态变化都同时开一个新 epoch、换一个新的 scope，并清理旧 scope**：

```python
    def _open_epoch(self, state: FiberState) -> None:
        previous = self.effects
        self.state = state
        self.epoch += 1
        self.effects = EffectScope()
        previous.dispose()
```

于是三种生命周期事件共享同一条失效路径：

| 事件 | 结果 |
| --- | --- |
| `dispose()` | 终态 `DISPOSED`；旧 scope 清空；之后不能再创建 timer |
| `release()` | 依赖消失，进入 `INACTIVE`；旧 scope 清空；可以再次 `activate()` |
| `restart()` | `release()` + `activate()`，epoch 前进两步；旧 epoch 的句柄与新 epoch 完全隔离 |

为什么清理之外还需要 epoch？因为句柄的失效判定必须是**多重**的：scope 相同但 epoch 变了（restart），或者 epoch 对但 scope 被换掉（release 后新 epoch）都不行。epoch 还是"周期任务自我复活"的兜底防线：interval 在 callback 返回后要决定是否重新入队，如果只看"没被 cancel"，一个在 callback 里 `restart()` 了自己的 owner 会让旧定时器重新出现在新 epoch 里。第 03 篇用测试固定这一点。

### Checkpoint B：owner 一消失，时间推进就是空的

```python
from chapter19 import Fiber, ManualClock, TimerService

clock = ManualClock()
fiber = Fiber("demo")
fiber.activate()
timers = TimerService(clock, fiber)
handle = timers.timeout(1.0, lambda: print("must not run"))
fiber.dispose()
print("disposed", handle.disposed, "pending", clock.pending, "effects", len(fiber.effects))
clock.advance(10.0)
```

预期输出：

```text
disposed True pending 0 effects 0
```

没有 `must not run`。旧 callback 之所以不执行，不是因为宿主记得手动取消它，而是因为 owner 的清理路径和时钟的有效性谓词是同一件事的两个出口。

## 与正式源码对应

本章是候选契约：`cordis-py` 没有 Timer 资产；相邻的正式组件只有 host-driven [`reloader.py`](../../cordis-py/src/cordis/loader/reloader.py)，所有权模型对应 [`effect.py`](../../cordis-py/src/cordis/effect.py) 与 [`fiber.py`](../../cordis-py/src/cordis/fiber.py)，"Timer 不在最小 Core"见 [`01-overview.md`](../../cordis-py/docs/architecture/01-overview.md) 与 [`02-core-model.md`](../../cordis-py/docs/architecture/02-core-model.md) 的所有权树示意。上一章的失败隔离见 [`../18-include/03-failure-isolation-and-boundaries.md`](../18-include/03-failure-isolation-and-boundaries.md)。

## 与 DSH Cordis 对照与扩展边界

`TimerHandle` 安装在创建者 `EffectScope` 上、`handle.valid` 三重判定、`advance` 作为时间源，分别对应 DSH 的定时资源生命周期、失效语义与时间推进概念；但本章不做 API inventory，也**不宣称**与 `@deepseek-ai/cordis-plugin-timer` 1.1.2 等价。Timer 与 Loader/Include/HMR 一样不在 Core 兼容矩阵范围内，见 [`cordis-core-compatibility.md`](../../cordis-py/docs/cordis-core-compatibility.md)。

## 本章刻意简化

同步 `Disposer`（取消不需要 `await`）；只允许 `ACTIVE` owner 创建 timer（没有 `LOADING` 状态）；scope 用惰性跳过 + 列表删除；不接 isolation / intercept / Logger。

## 检查理解

1. 为什么 `advance` 要把 `self._now` 先设为 `target`，再触发到期条目？如果改成"逐条触发时把 now 设为该条目的计划时间"，interval 的行为会变成什么？
2. `_is_live` 为什么要调用 `valid()`，而不是只检查 `cancelled` / `completed`？请给出一个只检查这两个标志就会出错的场景。
3. `Fiber._open_epoch` 为什么必须**先换 scope 再 dispose 旧 scope**？如果反过来会怎样？
4. `clock.failures` 的存在解决了三个失败场景中的哪一个？另外两个分别由谁解决？
