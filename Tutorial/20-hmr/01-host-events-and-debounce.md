# 20 — HMR：宿主事件、虚拟时钟与 debounce

第 17 章给了我们一棵可挂载、可更新、可替换的 Entry 树，第 18 章让它能从多个文件组合而来。但两章共有一个前提：**变化是别人告诉 Loader 的**。

正式 [`ConfigReloader`](../../cordis-py/src/cordis/loader/reloader.py) 只有两个方法：

```python
async def start(self) -> tuple[Entry, ...]: ...
async def reload(self) -> bool: ...
```

`reload()` 不知道配置什么时候变了。它重新读一遍源文件、比较内容，再把结果交给 `Loader.update()`。**谁发现变化、什么时候调用 reload，是宿主的责任。**

这一章处理这个缺口：如果宿主把"变化"送进来，运行中的插件树要怎样安全地换一代？

```text
Host:  东西变了（配置 / 模块），这是事件
本章:  合并事件 → 分类 → config update 或 replacement → 失败回滚 → 旧 epoch 全部释放
Host:  真正去看文件、真正重新 import 模块
```

## 本章目标

- 用 `ChangeEvent` 定义宿主与运行时间的**唯一输入契约**：本章实现从不自己检测变化；
- 用 `ManualClock` + `debounce_burst` 把短时间内的重复事件合并成一次处理，不使用真实时间或 `asyncio.sleep`；
- 区分 config-only update 与 structural/module replacement，让两条路都具备事务语义；
- 用 generation（epoch）证明：替换成功后旧一代的 Effect、listener、task 引用全部撤销且只清理一次；
- 用 `MappingModuleSource` 显式注入模块版本，使"候选无效，保留旧版本"无需触碰 `importlib` 即可测试。

教学实现自包含在 [`src/chapter20.py`](src/chapter20.py)，只导入标准库，不导入 [`cordis`](../../cordis-py/src/cordis)，也不导入第 17/18/19 章。

## Step 0：先纠正四个错误预期

**错误一：以为正式实现里有 watcher。** 正式 Python 实现里没有 watcher、线程、轮询任务，也没有 `importlib.reload`。架构文档写得很直白：

> Python 实现提供宿主驱动的 `ConfigReloader`，不在核心内创建 watcher、线程或轮询任务。
> 模块代码重载由宿主负责刷新 import 状态，再显式调用 `Loader.replace()`，避免 Cordis 猜测进程级模块缓存策略。
> —— [`docs/architecture/07-configuration-loader.md`](../../cordis-py/docs/architecture/07-configuration-loader.md) 的 "HMR" 一节

所以本章实现的是**教学候选契约**，不是"复现 DSH HMR 1.0.15"，也不声称与它等价。

**错误二：以为 debounce 需要 `asyncio.sleep`。** 真实 sleep 无法确定性测试：要么慢，要么抖动。本章用虚拟时钟，时间只在测试明确要求时前进。

**错误三：以为 config 变化应该重建整棵树。** 改一个 URL 却走 replacement，整棵树会 stop/start 一次。第 17 章已确立优先级：结构没变就原地 update。

**错误四：以为"替换成功"就够了。** 替换难的不是"新代码跑起来"，而是"旧代码的资源全都放下、且只放下一次"。旧 epoch 的 listener 还能被调用、或它的 cleanup 跑了两次，长时间运行后就会出现重复行为与资源泄漏。

## Step 1：最小 Core——EffectScope 与 Disposer

Core 只需要支撑"一代插件"这一个概念，比第 17 章小：依赖调度、Inject、隔离都不在这里。

```python
class Disposer:
    """Idempotent, awaitable cleanup handle owned by exactly one scope."""

    def __init__(self, execute: Callable[[], object], label: str) -> None:
        self.label = label
        self.runs = 0
        self._execute = execute
        self._ran = False

    def dispose(self) -> None:
        if self._ran:
            return
        self._ran = True
        self.runs += 1
        self._execute()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        errors: list[BaseException] = []
        for disposer in reversed(self._items):
            try:
                disposer.dispose()
            except BaseException as error:  # noqa: BLE001 - cleanup continues after the first failure
                errors.append(error)
        self._items.clear()
        if errors:
            raise BaseExceptionGroup("effect cleanup failed", errors)
```

三条约定与第 17 章一致，但在这里更关键：

| 约定 | 为什么本章需要它 |
| --- | --- |
| 逆序清理 | 后安装的 listener 可能引用先安装的 service，先撤销后安装的 |
| 幂等 | 一代可能先被 replacement 清理，随后又被 `close()` 触及 |
| 不因第一个异常停止 | 一个资源泄漏不该让其余资源永远不释放 |

`close()` 用 `BaseExceptionGroup` 聚合失败，后面会反复用到。

## Step 2：Fiber 的 epoch 与"当前 scope"必须能被换掉

config-only update 的语义是"同一个 Fiber，新的 epoch"，所以 Fiber 不能在构造时把 cleanup 栈定死：

```python
class Fiber:
    def activate(
        self,
        config: object,
        *,
        name: str,
        target: HotReloadTarget | None = None,
        scope: EffectScope | None = None,
    ) -> tuple[EffectScope, list[Disposer]]:
        owner = self.target if target is None else target
        if owner is None:
            raise StaleEpochError("fiber has no generation target")
        effects = EffectScope() if scope is None else scope
        disposers: list[Disposer] = []
        install = owner.bind(effects)
        self.context._bind(owner, effects, install, generation=owner.generation)
        try:
            result = self.callback(self.context, config)
            if result is not None:
                cleanup = cast(Cleanup, result)

                def release() -> None:
                    outcome = cleanup()

                disposers.append(install(release, f"plugin: {name}"))
        except BaseException:
            self.context._detach()
            close_scope(effects)
            raise
        self.config = config
        self.target = owner
        self.scope = effects
        self.disposers = disposers
        self.epoch += 1
        return effects, disposers
```

关键是**调用方传入新的 scope**：首次挂载 `scope=None`（内部新建）；config update 显式传入全新 `EffectScope()`；任何一次激活失败，本次安装的东西在 `except` 里被清掉，旧 epoch 完全不受影响。这比"Fiber 自己持有 `self.effects`"更明确，因为它强制每一步都想清楚：**这一代现在归谁。**

## Step 3：ManualClock——时间只在被要求时前进

任务句柄要先说明一件事：**取消必须让回调永不执行**。

```python
class TaskHandle:
    """One scheduled callback owned by the generation that scheduled it.

    ``cancel()`` is what makes HMR safe: a scheduled callback that has been
    cancelled never runs, even if its deadline has already been computed.
    """

    def __init__(
        self, due_ms: int, sequence: int, callback: Callable[[], object]
    ) -> None:
        self.due_ms = due_ms
        self.sequence = sequence
        self.callback = callback
        self.cancelled = False
        self.fired = 0
        self._cancel: Callable[[], None] | None = None

    def cancel(self) -> None:
        if self.cancelled:
            return
        self.cancelled = True
        if self._cancel is not None:
            self._cancel()

    def invoke(self) -> object:
        """Run the callback unless the handle was cancelled in the meantime."""

        if self.cancelled:
            return None
        self.cancelled = True
        self.fired += 1
        return self.callback()

    def advance(self, delta_ms: int) -> None:
        if self.closed:
            raise HotReloadError("clock is closed")
        if delta_ms < 0:
            raise ValueError("delta_ms must not be negative")
        target = self._now_ms + delta_ms
        for _ in range(10_000):
            self._timers.sort(key=lambda item: (item[0], item[1]))
            if not self._timers or self._timers[0][0] > target:
                break
            due_ms, _, fire = self._timers.pop(0)
            self._now_ms = due_ms
            fire()
        else:
            raise HotReloadError("virtual clock did not settle; a timer looped")
        self._now_ms = target
```

两个刻意的选择：

1. `for/else` 的 10,000 次上限是**哨兵**：若某个回调在被调用时又登记一个立即到期的回调，`advance` 会报"时钟不收敛"，而不是挂死测试；
2. `cancel()` 让 `invoke()` 变成空操作——这正是"旧 epoch 的 task 不再执行"的实现基础（第 03 节验证）。

### Checkpoint A

```text
ManualClock 的性质
  时间只在 advance() 时前进
  到期顺序确定：先看到期时间，再看登记顺序
  取消过的回调一定不执行
  没有任何真实时间、sleep 或后台线程参与
```

## Step 4：debounce 把 burst 合并成一次处理

每次新事件都取消上一个定时器并重新计时：

```python
def debounce_burst(
    clock: ManualClock,
    callback: Callable[[], object],
    *,
    delay_ms: int,
) -> Callable[[], TaskHandle]:
    """Return a ``schedule`` function that merges a burst into one call."""

    handle: TaskHandle | None = None

    def schedule() -> TaskHandle:
        nonlocal handle
        if handle is not None:
            handle.cancel()
        handle = clock.call_later(delay_ms, callback)
        return handle

    return schedule
```

`handle.cancel()` 就是"重新计时"的全部实现。没有它，三个事件会变成三次处理；有了它，最后一个事件之后满 `delay_ms` 才处理一次。

`HotReloader` 接入调度器，并对外提供"喂事件 / 推进窗口 / 应用"三个入口：

```python
# HotReloader.__init__ 中：
self._schedule = debounce_burst(clock, self._drain, delay_ms=debounce_ms)


def on_change(self, event: ChangeEvent) -> None:
    """Accept one host-fed change and (re)arm the debounce window."""

    if self.closed:
        raise HotReloadError("hot reloader is closed")
    self._pending.append(event)
    self._timer = self._schedule()

def _drain(self) -> None:
    self.flush()

def flush(self) -> ChangeBatch | None:
    """Merge the pending events into one batch and hand it to the router."""

    if not self._pending:
        self._timer = None
        return None
    batch = ChangeBatch(tuple(self._pending))
    self._pending.clear()
    self._timer = None
    self.batches.append(batch)
    self.handle(batch)
    return batch

def drain(self, *, advance: bool = True) -> int:
    before = len(self.batches)
    if advance and self._pending:
        self.clock.advance(self.debounce_ms)
    return len(self.batches) - before

def advance(self, delta_ms: int) -> int:
    """Move the virtual clock, apply whatever it releases, return the batch count."""

    before = len(self.batches)
    self.clock.advance(delta_ms)
    return len(self.batches) - before
```

第一个可验证行为：**三个事件，一次处理**。

```python
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
```

```bash
cd Tutorial/20-hmr/src
PYTHONDONTWRITEBYTECODE=1 /tmp/cordis-tutorial-venv/bin/python -m pytest -p no:cacheprovider -q -k debounce
```

预期：`-k debounce` 收集 1 个测试并通过。`advance(59) == 0` 与 `advance(1) == 1` 是本章最重要的证据形式——**窗口边界可以逐毫秒断言**，因为时间没有被真实调度器接管。

### Checkpoint B

```text
burst:     config@0, config@40, config@80   （debounce_ms=100）
时钟推演:   deadline 依次为 100 → 140 → 180
t=179:     0 个批次（窗口未闭合）
t=180:     1 个批次，events = (2, 3, 4)
```

## Step 5：ChangeEvent 是宿主的输入契约

字段要表达"改成什么"，而不是"我是怎么发现的"：

```python
class ChangeKind(StrEnum):
    CONFIG = "config"
    MODULE = "module"


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    """One host-fed notification; the tutorial never detects changes itself."""

    kind: ChangeKind
    entry: str
    module: str | None = None
    version: int | None = None
    payload: object = field(default_factory=_no_payload)

    def __post_init__(self) -> None:
        if not self.entry:
            raise InvalidChangeError("change event requires a non-empty entry id")
        if self.kind is ChangeKind.CONFIG and not self.has_payload:
            raise InvalidChangeError("config change event requires a payload")
        if self.kind is ChangeKind.MODULE and not self.module:
            raise InvalidChangeError("module change event requires a module name")
```

`payload` 的默认值是哨兵对象 `_MISSING`，于是 `payload=None`（明确的配置值）与"没有携带 payload"（`has_payload is False`）可以被区分开。

一个 burst 合并后是一批事件，`ChangeBatch` 负责读成"这次要做什么"：

```python
@dataclass(frozen=True, slots=True)
class ChangeBatch:
    """The merged result of one debounce window."""

    events: tuple[ChangeEvent, ...]

    @property
    def structural(self) -> bool:
        """Any module event in the burst wins over config-only events."""

        return any(event.kind is ChangeKind.MODULE for event in self.events)

    @property
    def kind(self) -> ChangeKind:
        return ChangeKind.MODULE if self.structural else ChangeKind.CONFIG

    @property
    def payload(self) -> object:
        for event in reversed(self.events):
            if event.has_payload:
                return event.payload
        return _MISSING
```

分类规则只有一条，但要有理由：**burst 里只要有一个 module 事件，整批按 structural 处理**。模块换代代价更高、覆盖面更广；先做 replacement 再补 config 会让同一代被处理两次，而换新模块本来就该带上新配置。

```python
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
    assert batch.version == 2
    assert batch.payload == 5  # the config part of the burst is not lost
```

```bash
PYTHONDONTWRITEBYTECODE=1 /tmp/cordis-tutorial-venv/bin/python -m pytest -p no:cacheprovider -q -k structural
```

### Checkpoint C

```text
宿主输入：ChangeEvent(kind, entry, module?, version?, payload?)
合并窗口：debounce_burst，最后一个事件之后 delay_ms 闭合
分类     ：config-only → update_config
          任意 module 事件 → replace（且不丢失同批的 config）
```

到这里**输入侧**闭环成立：宿主喂事件，运行时得到一次明确的、可分类的批处理。下一节处理"这批要做什么"。

## 与正式源码对应

- 宿主驱动重载的完整契约：[`src/cordis/loader/reloader.py`](../../cordis-py/src/cordis/loader/reloader.py)
  （只有 `start()` / `reload()`，由宿主调用；不存在 watcher、mtime 监视或模块缓存刷新）
- 重载如何落到 Loader 事务：[`src/cordis/loader/runtime.py`](../../cordis-py/src/cordis/loader/runtime.py) 的 `Loader.update()` / `Loader.replace()`
- include 组合后再重载：[`src/cordis/loader/composer.py`](../../cordis-py/src/cordis/loader/composer.py)
- 设计说明与边界：[`docs/architecture/07-configuration-loader.md`](../../cordis-py/docs/architecture/07-configuration-loader.md) 的 "HMR" 与 "安全边界"

| 本章教学概念 | 正式实现中的位置 | 说明 |
| --- | --- | --- |
| 宿主喂入 `ChangeEvent` | 宿主调用 `ConfigReloader.reload()` | 结构对应：变化检测都在核心之外 |
| debounce 合并 burst | 正式实现没有 debounce | 未实现：契约假定宿主自己决定重载频率 |
| `MappingModuleSource` | 宿主刷新 `importlib` 后调用 `Loader.replace()` | 教学替身：用 mapping 代替真实模块缓存 |
| `ChangeBatch.structural` | `Loader.update()` 内部的 identity 比较 | 结构对应：分类依据不同（事件种类 vs 声明身份） |

正式实现**只有 host-driven reload 基础**：不监视文件、不读 mtime、不刷新模块缓存，因此"检测变化"在本章和正式实现中都在宿主侧。

## 与 DSH 及扩展边界对照

`@deepseek-ai/cordis-plugin-hmr` 1.0.15 的文件监视与模块热替换闭环**在教学范围之外**。本章不导入它、不模拟它的 API，也不构成任何等价证据。

| DSH HMR 概念 | 本章表达 | 状态 |
| --- | --- | --- |
| 变更通知 | `ChangeEvent` | 教学候选契约：字段由本章定义 |
| 变更合并 | `debounce_burst` + `ManualClock` | 教学替代：明确不属于 DSH HMR |
| 配置变更走 update | `HotReloader.update_config()` | 概念对应，见下一节 |
| 模块变更走 replacement | `HotReloader.replace()` | 概念对应，见下一节 |
| 文件监视 / mtime / 模块缓存 | **不实现** | 宿主责任，本章不宣称 |
| 与 DSH HMR 1.0.15 等价 | **不宣称** | 需要独立的 API inventory 与双语言行为场景 |

`cordis-py` 目前没有 HMR 实现；`ConfigReloader` 是宿主驱动的重载适配器，不是 watcher。本章的 `HotReloader` 是**为教学推导出的候选契约**，回答"如果宿主送来事件，运行时需要哪些不变量"，但不是任何已发布组件的移植。

## 本章刻意简化

- 不实现真实 OS watcher、不读文件 mtime、不做真实 `importlib.reload` 或模块缓存刷新；
- 没有 debounce 的实际等待：`ManualClock` 不让出控制权，`advance()` 是同步的；
- Core 只有 `EffectScope`/`Fiber`/`Context`/service 表，没有 Inject、依赖 epoch、隔离与 intercept；
- Fiber 只区分"有 target / 无 target"，没有第 17 章的六态状态机：本章关注 generation，而不是状态迁移图；
- 不实现多线程或多事件循环下的正确性——本章的确定性来自"只有一个执行流"。

## 检查理解

1. 删掉 `debounce_burst` 里的 `handle.cancel()`，Step 4 的哪个断言会先失败？为什么它会同时破坏"合并"和"只处理一次"两件事？
2. `ManualClock.advance()` 的 `for/else` 上限保护的是什么错误？一个合法的重载流程会不会触碰它？
3. 为什么 `ChangeEvent.payload` 需要哨兵默认值，而不是直接 `payload: object = None`？举出一个会被这两种设计区分开的行为。
4. `ChangeBatch.entry` 取 `events[-1].entry`。如果宿主在一个 burst 里送了两个不同 entry 的事件，当前实现会发生什么？这属于"没实现"还是"实现错了"？
