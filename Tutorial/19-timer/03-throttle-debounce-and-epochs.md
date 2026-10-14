# 19 — Timer：throttle、debounce、epoch 与整章回归（Step 7–10）

前两步实现了"到点触发"的三类能力：一次、周期、等待。这一篇处理另一类时间需求：**由调用方触发、但受时间约束的函数**。

典型场景都在后面的 HMR 章节：

```text
文件在 200ms 内被连续保存三次 → 只想重载一次（debounce，等到安静之后）
进度上报每 10ms 触发一次 → 只允许每 500ms 真正写一次（throttle，领先边沿）
```

它们与 timeout/interval 的区别是：**触发时机由外部输入决定**，时钟只负责"窗口"或"安静期"。因此句柄本身必须可以像函数一样被调用，并且仍然挂在创建者的 owner 上。

## Step 7：`throttle`——领先边沿，窗口内最多一次

语义先定清楚，再写代码：

| 调用时刻 | 距上次真正执行 | 结果 |
| --- | --- | --- |
| 第一次 | 从来没有 | 立即执行，记录 `last_call` |
| 窗口内再次调用 | `< window` | 不执行，`suppressed += 1` |
| 恰好等于 window | `== window` | 执行（判定用 `< window` 抑制） |
| 句柄已取消 / owner 已失效 | — | 静默无操作 |

实现是"用虚拟时钟读时间，而不是排一个条目"：

```python
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
```

throttle **不向时钟注册条目**，所以 `clock.pending` 不会被它影响。这不是遗漏，而是语义：被抑制的调用根本不需要"未来的时间点"。真正的抑制依据是可观察的 `suppressed` 计数。

被取消或 owner 失效的句柄仍然可以被调用，只是什么都不做——这条由 `TimerHandle.__call__` 统一保证：

```python
    def __call__(self) -> None:
        """Invoke a throttle/debounce handle; a stale handle is a no-op."""

        if self._invoke is None:
            raise TypeError(f"{self.kind} handle is not directly callable")
        if not self.valid:
            return
        self._invoke()
```

"调用一个已取消的节流函数"是正常事件（例如 HMR 在旧 epoch 里还持有闭包），所以它是无操作，而不是报错。报错只会把所有权问题变成调用点的异常处理问题。

对应测试先断言领先边沿，再断言释放 owner 之后句柄失效：

```python
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
```

### Checkpoint F：throttle 的可观察行为

```python
from chapter19 import Fiber, ManualClock, TimerService

clock = ManualClock()
fiber = Fiber("demo")
fiber.activate()
timers = TimerService(clock, fiber)
calls = []
save = timers.throttle(1.0, lambda: calls.append(("save", clock.now)), label="save")
for _ in range(3):
    save()
clock.advance(1.0)
save()
print(calls)
print("suppressed", save.suppressed, "pending", clock.pending)
```

预期输出：

```text
[('save', 0.0), ('save', 1.0)]
suppressed 2 pending 0
```

三次连续调用只产生一次执行；窗口过去后恢复；`pending` 始终为 0。

## Step 8：`debounce`——尾部边沿，安静之后只做一次

debounce 与 throttle 相反：**不是丢弃调用，而是推迟调用**。每次调用都把尾部触发点往后推：

| 调用时刻 | 已经排队的尾部调用 | 结果 |
| --- | --- | --- |
| 第一次 | 没有 | 排一个 `now + delay` |
| 安静期内再次调用 | 有 | 取消旧的，重排 `now + delay` |
| 时钟推进到尾部触发点 | 有 | 触发一次，标记 `completed` |
| 已取消 / owner 失效 | 有或没有 | 不触发，`pending` 归零 |

实现复用了 timeout 的 `_fire_once`，只是"重新入队"由外部调用触发：

```python
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
```

两个语义决定写进测试：

1. **可复用**：尾部调用触发后，同一个句柄可以开启新的一轮 burst。所以 `invoke` 会把 `completed` 复位，`valid` 也不包含 `completed`；
2. **取消会撤销未触发的尾部调用**：`handle.dispose()` 触发 scope 里的 `_cancel`，旧条目被标记取消，时间推进时被 `_is_live` 跳过。

```python
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
```

最后一条断言是所有权证据：取消之后句柄从 owner 的 scope 上真正消失，而不是留下一个"已失效但仍占位"的条目。

### Checkpoint G：debounce 的可观察行为

把 throttle 与 debounce 放进同一个脚本：

```python
search = timers.debounce(1.0, lambda: calls.append(("search", clock.now)), label="search")
search()
clock.advance(0.5)
search()
clock.advance(1.0)
print(calls)
```

承接 Checkpoint F 的脚本，预期追加输出：

```text
[('save', 0.0), ('save', 1.0), ('search', 2.5)]
```

两次 `search()` 只换来一次执行，时刻是最后一次调用之后的 1.0 秒（`1.5 + 1.0 = 2.5`），而不是第一次调用之后。

## Step 9：epoch——owner 重启后旧定时器必须失效

这是本章最容易漏掉、也最难在真实时间里复现的一条。场景是：

```text
插件在 interval 的 callback 里触发了对自己的 reload（restart）；
interval 的 callback 已经返回，代码接下来要决定"要不要重新入队"。
```

如果重新入队只检查 `cancelled` 标志，旧 epoch 的定时器会出现在新 epoch 的调度表里：旧闭包继续引用旧服务，旧 epoch 的副作用泄漏进新生命周期。本章用三重判定 + 触发前重查把这个窗口关掉：

```text
每次 activate()/release()/dispose() 都替换 scope 并让 epoch +1
handle 在创建时冻结 (scope, epoch)
时钟每次触发前重新调用 handle.valid
interval 在重新入队前再检查一次 handle.valid
```

注意最后两条是**两个不同的检查点**：一次在"这个条目现在还能不能被触发"，一次在"刚才跑完的周期任务还能不能安排下一次"。测试对第二条的断言是：

```python
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
```

其中 `raw` 是直接由时钟调度的条目，它的 `valid` 谓词绑定在旧 epoch 上：

```python
    raw = clock.schedule(
        5.0,
        lambda: trace.append("raw"),
        label="raw",
        valid=lambda: fiber.epoch == epoch_one,
    )
```

`raw.cancelled is False` 而 `clock.pending == 0` 这一对断言很关键：条目**仍在堆里**，但它已经不能触发。这证明失效发生在"触发前的有效性检查"这一层，而不是依赖"取消时把条目从数据结构里删掉"。epoch 与 scope 的变化都能穿过这一层。

新 epoch 里创建 timer 必须完全正常，这是失效语义的另一半：

```python
    fresh = timers.timeout(1.0, lambda: trace.append("fresh"), label="fresh")
    assert fresh.epoch == fiber.epoch
    clock.advance(1.0)
    assert trace == ["tick", "fresh"]
    assert clock.pending == 0
```

### Checkpoint H：epoch 失效的直接证据

```python
from chapter19 import Fiber, ManualClock, TimerService

clock = ManualClock()
fiber = Fiber("demo")
fiber.activate()
timers = TimerService(clock, fiber)
trace = []
epoch_before = fiber.epoch

def tick():
    trace.append("tick")
    fiber.restart()

periodic = timers.interval(1.0, tick, label="tick")
clock.advance(1.0)
print("trace", trace, "epoch", epoch_before, "->", fiber.epoch)
print("valid", periodic.valid, "pending", clock.pending)
fresh = timers.timeout(1.0, lambda: trace.append("fresh"), label="fresh")
clock.advance(1.0)
print("trace", trace, "pending", clock.pending)
```

预期输出：

```text
trace ['tick'] epoch 1 -> 3
valid False pending 0
trace ['tick', 'fresh'] pending 0
```

epoch 从 1 到 3（release 一次、activate 一次），旧周期任务 `valid` 为假且没有复活，新 epoch 的 timeout 正常工作。

## Step 10：整章一起跑

```bash
cd Tutorial/19-timer/src
PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider -q
```

预期：

```text
..........                                                               [100%]
10 passed in 0.03s
```

十条测试覆盖的契约：

| 测试 | 覆盖的契约 |
| --- | --- |
| `timeout_fires_once…` | 正常路径：一次触发、时间查询、不重复、无失败 |
| `disposing_the_owner…` | 所有权与生命周期：dispose 清空 scope、旧 callback 不执行、非 ACTIVE 创建报错 |
| `cancel_and_dispose…` | 取消：幂等、重复取消、取消已完成句柄 |
| `interval_ticks…` | 周期：按周期触发、一次跳跃只触发一次、取消后停止 |
| `interval_failure_contract…` | 失败契约：记账、不追赶、连续失败达上限停止、成功复位 |
| `failing_callback_is_isolated…` | 失败隔离：兄弟 timer 照常、双层账本、owner 不受影响 |
| `throttle_keeps_the_leading_edge…` | throttle：领先边沿、抑制计数、不占用时钟、release 后失效 |
| `debounce_keeps_the_trailing_call…` | debounce：尾部边沿、重排、复用、dispose 撤销未触发调用 |
| `restart_opens_a_new_epoch…` | 新 epoch：旧句柄/旧条目失效、不复活、新 epoch 正常 |
| `sleep_wakes_on_advance…` | sleep：虚拟唤醒、owner dispose 取消、资源归零 |

同时跑静态检查（ruff 使用仓库固定的目标版本与无缓存模式）：

```bash
ruff check --target-version py311 --no-cache chapter19.py test_chapter19.py
ruff format --check --no-cache chapter19.py test_chapter19.py
```

预期分别是 `All checks passed!` 与 `2 files already formatted`。

### Checkpoint I：整章终点

```text
ManualClock（虚拟时间 + 触发前有效性 + 失败账本）
   → TimerService（owner 绑定）
      → timeout / interval / sleep / throttle / debounce
         → 句柄 = EffectScope 上的 Disposer
            → owner.dispose() / release() / restart() 撤销一切
```

此时仍然**不包含**：文件监视、模块缓存刷新、真实事件循环定时器、Logger 接入。这些分别属于第 20 章和最终 Harness：

- 第 20 章用 debounce 合并短时间内的重复文件变更，并验证旧 epoch 的服务、监听器、task 和模块引用全部撤销；
- 第 21 章用 Timer 驱动周期性心跳，并在关闭根 Context 后验证 Timer task 归零。

## 与正式源码对应

- 本章是候选契约，不是对照实现：`cordis-py` 目前**没有**任何 Timer 模块、测试或依赖。可以用 `grep -ri timer cordis-py/src cordis-py/tests cordis-py/pyproject.toml` 自行确认；
- "Timer 属于可选插件，不进入最小核心"：[`docs/architecture/01-overview.md`](../../cordis-py/docs/architecture/01-overview.md)；
- 所有权树的示意（`└── Effect: timer`）：[`docs/architecture/02-core-model.md`](../../cordis-py/docs/architecture/02-core-model.md)；
- Effect 与资源收集、Fiber 状态机与 epoch 的正式实现：[`src/cordis/effect.py`](../../cordis-py/src/cordis/effect.py)、[`src/cordis/fiber.py`](../../cordis-py/src/cordis/fiber.py)；
- 宿主驱动的重载适配器（无 watcher、无模块缓存刷新、无自己的时间源）：[`src/cordis/loader/reloader.py`](../../cordis-py/src/cordis/loader/reloader.py)；
- Loader / Include / HMR / Timer 不在 Core 对照范围：[`docs/cordis-core-compatibility.md`](../../cordis-py/docs/cordis-core-compatibility.md)；
- 仓库当前状态与"未运行章节测试"的记录：[`cordis-py/.agents/STATE.md`](../../cordis-py/.agents/STATE.md)；
- 上一章：[第 18 章 Include](../18-include/03-failure-isolation-and-boundaries.md)；第 17 章 Loader 的所有权与边界：[`../17-loader/04-reloader-and-boundaries.md`](../17-loader/04-reloader-and-boundaries.md)；
- 下一章规划：[`../README.md`](../README.md) 的第 20 章 "HMR" 一节。

## 与 DSH Cordis 对照与扩展边界

| DSH Timer 概念 | 本章的候选契约 | 边界措辞 |
| --- | --- | --- |
| `throttle` | 领先边沿 + `suppressed` 计数 | 结构对应：行为契约，不是 API 对齐 |
| `debounce` | 尾部边沿 + 可复用句柄 | 结构对应：行为契约，不是 API 对齐 |
| 定时资源随 Fiber 撤销 | `TimerHandle.valid` 三重判定 + `EffectScope` | 结构对应：本节测试覆盖的行为契约 |
| 旧 epoch 定时器失效 | `epoch` + 触发前重查 | 结构对应：本节测试覆盖的行为契约 |
| Timer 1.1.2 的完整 API 与边界行为 | **未实现、也未对照** | 不宣称等价 |

写清楚三条边界：

1. 本章**不构成** DSH `@deepseek-ai/cordis-plugin-timer` 1.1.2 的等价证据：没有逐条 API inventory，也没有规范化输出对齐；Timer 与 Loader、Include、HMR 一样被明确排除在 Core 兼容矩阵之外（见 [`docs/cordis-core-compatibility.md`](../../cordis-py/docs/cordis-core-compatibility.md) 开头的作用域说明；DSH Harness 侧的对照边界另见 [`docs/compatibility.md`](../../cordis-py/docs/compatibility.md)）；
2. interval 的失败契约（`failure_limit` + 不追赶）是**本章定义的候选行为**，不是从 DSH 翻译来的既有语义；
3. 本章没有更新 `cordis-py` 的任何状态文档，也没有对 Phase 0–5 的结论做任何补充：Timer 是否进入正式实现，需要独立的行为契约、inventory 和跨语言场景来证明。

## 本章刻意简化

- `throttle` 只有领先边沿，没有 trailing 选项；`debounce` 只有尾部边沿，没有 `maxWait`；
- 两个能力都不接收参数（`TimerHandle.__call__` 无参数）。真实场景常需要透传事件负载，本章只保留时间语义；
- `sleep` 不支持超时后返回句柄；取消只能来自 owner 或等待任务的取消；
- 不接 Logger，不接遥测；失败账本只在内存里；
- 教学实现用"触发前重查谓词"代替真实事件循环的 `call_later` + `handle.cancel()`，因此不能直接搬到 asyncio 生产代码里，只能搬运所有权语义。

## 检查理解

1. 为什么 `throttle` 不向时钟注册条目，而 `debounce` 必须注册？如果把 throttle 也实现成"排一个条目"会引入什么状态？
2. `debounce` 的 `valid` 刻意不包含 `completed`。如果把它加上，Step 8 的哪一条断言会失败？"可复用 burst"为什么会坏掉？
3. `raw` 条目在 restart 之后 `cancelled is False` 但 `clock.pending == 0`。请用这两条断言解释"失效"发生的位置，以及为什么这比"取消时从堆里删除"更难写错。
4. 如果 `Interval` 在 `restart` 之后仍然用旧 epoch 的 `handle.valid` 判断并重新入队，第 21 章的整栈关闭验证会出现什么现象？（提示：Timer task 计数能否归零。）
