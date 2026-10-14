# 21 — 最小 Harness：从请求入口到整栈闭环（启动与稳态）

前 20 章每章只回答一个问题：Context 怎么派生、Effect 怎么收集、Fiber 怎么换 epoch、Loader 怎么做事务、Include 怎么组合配置、Timer 怎么随 owner 释放、HMR 怎么安全替换。到了最后这一章，问题换成了另一种：

```text
把这些契约放进同一个进程，它们还能同时成立吗？
一个真实请求穿过 gateway → consumer → provider 之后，谁拥有哪一份资源？
配置变了、provider 换了、候选坏了、进程关了，这四件事之后系统还剩什么？
```

答案不能靠"看起来对"。这一章要让整栈跑起来，并且在每次更新、失败和关闭之后都给出可数的证据。

## 本章目标

完成后能够运行：

- 从**多文件 YAML**（`config/cordis.yaml` + `include`）冷启动一棵插件树；
- 用 **`ManualClock` 推进虚拟时间**驱动 heartbeat，不使用真实时间；
- 让 **config-only 变化走原地 update**、**模块变化走 candidate replacement**；
- 在替换窗口内观察请求进入 `PENDING`，替换完成后恢复；
- 注入**无效候选**，验证旧 generation 继续工作、事务回滚；
- 关闭根 Context 后，registry / services / listeners / timer handles / watcher subscriptions / effects **全部归零**。

教学实现自包含在 [`src/chapter21.py`](src/chapter21.py)，本地插件在 [`src/plugins/`](src/plugins/)，配置在 [`config/`](config/)，可运行脚本是 [`src/run.py`](src/run.py)。本章只使用标准库与项目已锁定的 PyYAML（仅 `safe_load`），不导入 [`cordis`](../../cordis-py/src/cordis)，也不跨章 import。

## 当前问题：为什么不能直接复用第 17–20 章

第 17 章的 Loader 与第 18 章的 Composer 是真实契约，但它们的"运行侧"只是一个字典树；第 19、20 章的 Timer 与 HMR 是教程定义的候选契约。把三份代码简单拼起来，会立刻暴露四个缺口：

| 缺口 | 后果 |
| --- | --- |
| 没有统一的 Effect owner | timer 句柄、事件 listener、服务、child fiber 各自为政，关闭后数不清 |
| 没有 dependency scheduler | 只能手工把 provider 排在 consumer 前面，无法表达"等 provider" |
| 没有真实插件文件 | 配置里的 `module` 是虚构字符串，"链接真实存在"无法验证 |
| 没有宿主边界 | HMR 自己猜文件变化，无法区分 config 与 module，也无法控制 debounce |

所以本章不新增概念，而是把已有契约**放进同一棵所有权树**，然后用一个可数的 snapshot 证明它们一起工作。

## Step 0：先约定这一章的观察方式

整章只依赖两组可观察量：

```text
请求路径：harness.request("ping") -> "v1:alpha:ping" | "v2:beta:PING" | "PENDING"
关闭快照：harness.snapshot() -> {services, fibers, entries, listeners,
                                timer_handles, watcher_subscriptions, effects, ...}
```

只要这两组量是确定的，整栈行为就是可验证的；下面每一步都只增加让它们成立的机制。

## Step 1：最小所有权核心

Timer 句柄、事件 listener、服务、child fiber 必须共用同一套 owner。本章沿用第 17 章已经验证过的形状，但裁剪到最小：

```python
class EffectScope:
    def install(self, execute, label) -> Disposer:
        if self.closed:
            raise InactiveError(f"owner is disposed; cannot install {label!r}")
        ...

    async def close(self) -> None:
        self.closed = True
        items, self._items = self._items, []
        errors = []
        for disposer in reversed(items):        # 逆序
            try:
                await disposer.dispose()
            except BaseException as error:
                errors.append(error)            # 一个失败不中断其余清理
        if errors:
            raise BaseExceptionGroup("effect cleanup failed", errors)
```

`Fiber` 是插件的一次激活，`Context.provide()`、`Context.effect()`、`Context.on()` 都挂到当前 fiber 的 `EffectScope`：

```python
def provide(self, name, value) -> Disposer:
    if name in self._scope.services:
        raise HarnessError(f"service {name!r} is already provided")
    self._scope.services[name] = value
    def release():
        self._scope.remove(name, value)     # 移除服务会触发依赖重算
    disposer = self.fiber.effects.install(release, f"provide({name!r})")
    self._scope.request()
    return disposer
```

依赖就绪由 fiber 自己判断，Loader 不排序：

```python
def _ready(self) -> bool:
    services = self._scope.services
    return all(name in services for name in self.inject)
```

服务出现或消失时，`Scope.request()` 把 fiber 放进待刷新队列，`Context.stabilize()` 把它抽干：

```python
async def stabilize(self) -> None:
    guard = 0
    while True:
        fiber = self._scope.take()
        if fiber is None:
            return
        guard += 1
        if guard > _STABILIZE_LIMIT:
            raise HarnessError("dependency stabilization did not converge")
        try:
            await fiber.refresh()
        except BaseException as error:
            self._scope.errors.append((fiber.name, error))   # 失败被记录，不打断其余 fiber
```

`stabilize` 有收敛上限（`_STABILIZE_LIMIT`）；如果依赖变化永远无法收敛，它会抛出 `HarnessError`，而不是静默死循环。

### 为什么 epoch 要在这里说清楚

`Fiber.update()` 是本章最容易写错的地方。先看正确版本：

```python
async def update(self, config) -> None:
    if self.state is FiberState.DISPOSED:
        raise InactiveError("cannot update a disposed fiber")
    validated = validate_config(self._validator(), config)
    if not self._ready():
        self.config = validated
        return
    await self._release()                 # 先撤掉旧 epoch 的服务/任务/listener
    self.config = validated
    self.state = FiberState.PENDING
    await self.context.stabilize()        # 让消费者看到"服务消失"这个空窗
    if self.state is FiberState.PENDING:
        await self.load()
    self.version += 1
```

如果像最初那样把 `self.context.stabilize()` 放在 `self.config = validated` **之前**，provider 会在队列里被自己重新加载，用的还是旧配置，随后 `load()` 又因为状态已 ACTIVE 而直接返回——表面上 update 成功了，实际服务对象还是旧的。这一类错误只有"更新后重新发起请求并检查返回值"的测试能抓住。

同理，如果 `_release()` 之后不 drain，consumer 会继续持有被撤销的旧服务引用。`stabilize()` 的位置（在服务回来之前）就是"依赖 epoch 正确"的全部秘密。

## Step 2：可控时钟驱动 heartbeat

时间必须是可控输入。`ManualClock` 只在 `advance()` 时触发：

```python
def advance(self, seconds: float) -> int:
    target = self._now + seconds
    fires = 0
    while (item := self._queue[0] if self._queue else None) is not None and item.at <= target:
        heapq.heappop(self._queue)
        if item.cancelled:
            continue
        self._now = max(self._now, item.at)
        fires += 1
        if fires > _MAX_FIRES:
            raise HarnessError("clock advance exceeded the fire budget")
        try:
            item.callback()
        except BaseException as error:
            self.errors.append((item.label, error))   # 失败隔离到这一次触发
    self._now = target
    return fires
```

`interval` 的关键顺序是**先排下一次，再执行回调**：

```python
def fire() -> None:
    handle.fires += 1
    handle.item = self.clock.call_at(period, fire, label)   # 回调失败也不会丢节奏
    callback()
```

所有句柄都挂在创建它的 fiber 上：

```python
handle._disposer = owner.fiber.effects.install(lambda: handle.cancel(), label)
```

因此 `Fiber.dispose()` 会顺带取消 interval，旧 epoch 的回调在 owner 卸载后不可能再执行。heartbeat 插件只做三件事：`timer.interval(...)`、`context.provide("heartbeat", beat)`、在 tick 里 `events.emit("heartbeat", beat)`（见 [`src/plugins/heartbeat.py`](src/plugins/heartbeat.py)）。

## Step 3：YAML 组合出基础设施 / provider / consumer

配置被拆成三份，由根文件 `include`：

```yaml
# config/cordis.yaml
include:
  - infra.yaml
  - consumer.yaml
  - provider.yaml

overlays:
  heartbeat:
    config:
      period: 0.5
```

- [`config/infra.yaml`](config/infra.yaml)：`gateway`（稳定请求入口）与 `heartbeat`（周期任务）；
- [`config/consumer.yaml`](config/consumer.yaml)：`consumer`（agent loop，`inject` provider）；
- [`config/provider.yaml`](config/provider.yaml)：`provider`（可替换的实现，声明 `preamble`）。

`ConfigComposer` 的规则与第 18 章一致：`include` 相对**声明它的文件**解析、用规范化路径检测循环、按 entry id 应用 overlay、重复 id 报错、每个条目保留来源文件（`SourceLocation`）。overlay 让根文件可以在不改动 `infra.yaml` 的情况下把 heartbeat 周期从 1.0 覆盖成 0.5。

注意 include 的顺序：**consumer 声明在 provider 之前**。这是故意的，Step 5 会用它证明激活顺序来自依赖调度而不是书写顺序。

## Step 4：specifier 必须指向真实存在的插件文件

如果 `module: gateway:plugin` 只是一个字符串，配置和实现之间的联系就无法验证。本章的 `LocalModuleSource` 把 specifier 解释为 `<plugins_dir>/<name>.py` 的 `<attribute>` 入口，并且把模块声明的元数据一起返回：

```python
def resolve(self, specifier: str) -> PluginRef:
    path = self.path_for(specifier)            # 找不到文件 -> ResolveError
    module = self._cache.get(path) or self._import(path)
    target = getattr(module, attribute, None)
    if not callable(target):
        raise ResolveError(f"{path} has no callable entry point {attribute!r}")
    return PluginRef(
        specifier=specifier, path=path, module=module, attribute=attribute,
        callback=target,
        inject=normalize_inject(getattr(module, "inject", None)),
        Config=getattr(module, "Config", None),
    )
```

`PluginRef` 是"一个插件"的完整描述：入口点、`inject` 声明、`Config` 校验器。这一点很容易踩坑——如果 `resolve()` 只返回入口函数，函数上既没有 `inject` 也没有 `Config`，所有 fiber 都会因为"没有依赖"而立刻激活；测试里的 `activate:provider` 会排在 `activate:consumer` 之前，暴露这个错误。

本地插件共五个：

| 文件 | 角色 | 关键契约 |
| --- | --- | --- |
| [`plugins/gateway.py`](src/plugins/gateway.py) | 稳定请求入口 `requests` | **不** inject provider，调用时动态解析，缺失时返回 `PENDING` |
| [`plugins/heartbeat.py`](src/plugins/heartbeat.py) | 周期 heartbeat | 用 `timer.interval`，句柄属于自己 |
| [`plugins/provider_v1.py`](src/plugins/provider_v1.py) | provider v1 | `inject` gate/trace，`Config` 要求非空 `preamble` |
| [`plugins/provider_v2.py`](src/plugins/provider_v2.py) | provider v2 | 另一个真实文件；payload 大写，使版本可观察 |
| [`plugins/consumer.py`](src/plugins/consumer.py) | agent loop | `inject` provider/heartbeat/events/trace，用 `context.on` 订阅 heartbeat |

gateway 故意不 inject provider：否则 provider 换代时 gateway 会跟着卸载，请求入口就不"稳定"了。它改为在每次调用时 `context.get("provider")`，把"当前没有 provider"翻译成 `PENDING`。

## Step 5：Loader 挂载整棵树，不手工排序

`Loader.mount()` 逐个 `mount_entry`，每个条目都走 `Context.plugin()`：

```python
entry.fiber = await context.plugin(
    wrapper, parsed.config, name=parsed.id, inject=wrapper.inject,
)
```

`Context.plugin()` 只做一次 `refresh()`：依赖齐全就激活，否则停在 `PENDING`。所有根挂载完之后 `_mount_forest()` 调用一次 `context.stabilize()`，把依赖变化一次抽干。于是声明顺序为 `gateway, heartbeat, consumer, provider`，实际激活顺序由依赖决定：

```text
mount gateway   -> ACTIVE
mount heartbeat -> ACTIVE
mount consumer  -> PENDING（provider 还没有）
mount provider  -> ACTIVE（provide 触发 request）
stabilize       -> consumer ACTIVE（此时才注入 provider）
```

### Checkpoint A：冷启动闭环

```bash
cd Tutorial/21-mini-harness/src
PYTHONDONTWRITEBYTECODE=1 /tmp/cordis-tutorial-venv/bin/python -m pytest -p no:cacheprovider -q -k cold_start
```

预期通过，并确认四件事：

```python
assert harness.request("ping") == "v1:alpha:ping"
assert harness.trace.order("activate:provider") < harness.trace.order("activate:consumer")
assert harness.timer.pending == 1        # 只有一个 heartbeat interval
assert harness.snapshot()["watcher_subscriptions"] == 1
```

同一个测试还会遍历真实配置里的每个 specifier，用 `LocalModuleSource.path_for()` 断言对应文件存在——这就是"配置与插件链接真实存在"的机器可检验版本。

## Step 6：host-driven HMR 与 debounce

变化检测属于宿主，不属于运行时。本章的宿主边界是一个显式的 feed：

```python
class ChangeSource:
    def subscribe(self, handler) -> Disposer: ...
    def publish(self, event: ChangeEvent) -> None:
        for handler in tuple(self._handlers):
            handler(event)
```

`MiniHarness.start()` 订阅它，`close()` 退订。`HotReloader.notify()` 接到事件后只做两件事：入队、重新 arm debounce：

```python
def notify(self, event: ChangeEvent) -> None:
    if event.kind not in {"config", "module"}:
        raise HarnessError(f"unsupported change kind {event.kind!r}")
    self.pending_events.append(event)
    self._debounced.arm()
```

debounce 本身是 `TimerService` 的一个句柄，归 harness 根 Context 所有；连续变化只会保留一个待触发的时钟条目，因此一段 burst 最终只处理一次。测试不调用真实时间，而是显式推进时钟：

```python
harness.source.publish(ChangeEvent(kind="config"))
assert await harness.reloader.flush() == 0          # 时钟没走，什么都不发生
harness.clock.advance(harness.reloader.debounce)    # 越过静默期
assert await harness.reloader.flush() == 1
```

### Checkpoint B：稳态可观察

```bash
PYTHONDONTWRITEBYTECODE=1 /tmp/cordis-tutorial-venv/bin/python -m pytest -p no:cacheprovider -q -k "manual_clock or burst"
```

预期：`clock.advance(1.0)` 触发 2 次 heartbeat（overlay 把周期改成 0.5），三次连续变化在 debounce 后只产生一个批次，`clock.pending` 说明 burst 只占用**一个** debounce 条目。

下一步进入 [02-update-pending-and-recovery.md](02-update-pending-and-recovery.md)：配置变化与模块变化各自走哪条事务，以及替换窗口、失败回滚与恢复。
