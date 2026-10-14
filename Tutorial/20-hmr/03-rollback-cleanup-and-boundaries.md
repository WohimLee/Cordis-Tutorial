# 20 — 回滚、清理与边界

前两节建立了"谁来告诉运行时"和"批处理做什么"。这一节处理最难的部分：**失败之后系统必须处于什么状态。**

先看三类必须能回答的问题：

```text
候选激活失败了      → 现在是谁在服务？它还能正常工作吗？
旧一代的 cleanup 抛了 → 其余资源释放了吗？错误去哪了？
close() 被调用两次   → 资源会被清理两次吗？
```

这三条都是"失败路径"，也恰恰是 HMR 真正的价值所在：能换代码不稀奇，**换坏了还能继续跑**才稀奇。

## 本章目标（承接前两节）

- 实现替换失败的完整回滚：用旧声明重新挂载，并恢复原来的 generation 号；
- 实现清理失败的聚合：不中断其余 cleanup、把错误类型保留下来、向宿主上报；
- 证明旧 epoch 的 listener 与 task 在替换后不再生效；
- 证明 `close()` 让所有 generation、listener、task、effect 归零且幂等；
- 明确本章的四个边界：无 OS watcher、无 mtime、无真实 `importlib` 缓存刷新、不与 DSH HMR 1.0.15 等价。

## Step 11：候选失败有两种，处理方式必须分开

回滚不是一段代码，而是**两个不同的失败点**：

| 失败点 | 旧一代状态 | 正确反应 |
| --- | --- | --- |
| 解析（`resolve` 抛错） | 完全未触碰 | 什么都不做，直接报错 |
| 激活（新代码 setup 抛错） | 已被释放 | 用它原来的声明重新挂载 |

第一种在第 02 节已经验证过：`test_failed_candidate_keeps_the_previous_generation_running` 里注入坏模块后 `trace` 完全没有变化。第二种必须回滚：

```python
        try:
            built = self._build(candidate, callback, target, current)
        except BaseException as error:  # noqa: BLE001 - transaction boundary: restore the previous generation
            rollback_errors: list[BaseException] = [error]
            if cleanup_error is not None:
                rollback_errors.append(cleanup_error)
            try:
                restored_callback = self.source.resolve(
                    previous.module, previous.version
                )
                restored = self._build(previous, restored_callback, target, current)
            except BaseException as rollback_error:  # noqa: BLE001 - report both failures
                rollback_errors.append(rollback_error)
                # The entry has nothing serving it any more, so it must not
                # keep claiming the generation that was just released.
                target.generation = previous_generation
            else:
                self.generations[candidate.id] = restored
                # The restore reuses the generation number the entry already had;
                # a failed attempt must not make the live entry look newer.
                target.generation = previous_generation
                if len(rollback_errors) == 1:
                    raise ExecutionError(
                        f"failed to replace {candidate.id!r}; restored the previous generation",
                        phase="activate",
                        cause=error,
                    ) from error
            cause = BaseExceptionGroup(
                "replacement and rollback failed", rollback_errors
            )
            raise ExecutionError(
                f"failed to replace {candidate.id!r}", phase="activate", cause=cause
            ) from cause
```

三条不变量：

1. **回滚用旧声明，不用旧对象。** `previous` 是 `ParsedEntry`（声明），重新挂载会得到新的 Fiber 与 Context。旧 Fiber 在被释放的那一刻就已经终结，不能复活。
2. **generation 号要还原。** 回滚成功或失败都要把 `target.generation` 恢复为替换前的值；否则一次失败让人以为 entry 变新了。
3. **两个错误都要保留。** 激活错误与回滚错误放进 `BaseExceptionGroup`，不吞掉任何一个。

测试断言的完整轨迹：

```python
    reloader.on_change(replace_event("broken", "app:broken", 2))
    reloader.drain()

    assert len(reloader.errors) == 1
    failure = reloader.errors[0]
    assert isinstance(failure, ExecutionError)
    assert failure.phase == "activate"  # resolve succeeded, activation did not
    assert isinstance(failure.cause, RuntimeError)

    # The previous generation is back in charge, with its original number.
    assert reloader.generation("broken") == generation
    assert target.generation == generation  # the failed attempt bumped nothing
    assert reloader.live("broken") is provider_v1
    current = reloader.entry("broken")
    assert current.fiber is not fiber  # the restore mounts a fresh fiber
    assert current.fiber is not None and current.fiber.epoch == 1
    assert all(snapshot.clean for snapshot in target.history)
    assert current.context is not None and current.context.get("value") == "v1:1"

    # A later, valid change still works: the failure did not poison the entry.
    reloader.on_change(replace_event("broken", "app:broken", 1))
    reloader.drain()
    assert reloader.generation("broken") > generation
```

```bash
cd Tutorial/20-hmr/src
PYTHONDONTWRITEBYTECODE=1 /tmp/cordis-tutorial-venv/bin/python -m pytest -p no:cacheprovider -q -k failed_candidate
```

预期：通过，且 `trace` 为 `["v1:start:1", "v1:stop:1", "v1:start:1", "v1:stop:1", "v1:start:1"]`——第一次 `stop/start` 是回滚，第二次是后续的合法替换。

### Checkpoint I

```text
解析失败：零副作用
激活失败：释放旧一代 → 重新挂载旧声明 → 报 ExecutionError(phase="activate")
回滚失败：BaseExceptionGroup[激活错误, 回滚错误]，entry 不再宣称任何 generation
```

## Step 12：清理失败不能吞，也不能中断

先看 `EffectScope.close()` 的语义，它是所有清理路径的基座：

```python
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

`close()` 会抛，但只在**跑完所有 disposer 之后**抛。替换路径需要一个"不抛"的版本，因为它必须先完成换代、再决定怎么汇报：

```python
def close_scope(scope: EffectScope) -> BaseException | None:
    """Close a scope, returning the aggregated cleanup failure instead of raising."""

    try:
        scope.close()
    except BaseException as error:  # noqa: BLE001 - the caller decides how to report it
        return error
    return None
```

然后记账必须写进 `finally`，否则"抛错的 cleanup"在账本上会消失：

```python
        def install(cleanup: Cleanup, label: str) -> Disposer:
            def release() -> None:
                try:
                    outcome = cleanup()
                    if inspect.isawaitable(outcome):
                        raise TypeError("async cleanups are out of scope in chapter 20")
                finally:
                    # A cleanup that raises is still *released exactly once*,
                    # so its failure shows up in both the ledger and the error.
                    ledger[label] = ledger.get(label, 0) + 1

            return scope.install(release, label)
```

于是清理失败留下的证据是逐项可断言的：

```python
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
    assert isinstance(failure.cause.exceptions[0], RuntimeError)
```

同一个测试还包含最小尺度的契约验证——一个 scope、四个 cleanup、其中两个抛错：

```python
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
    assert [type(error) for error in caught.value.exceptions] == [RuntimeError, RuntimeError]
    scope.close()  # idempotent: no second pass, no second exception
    assert runs == ["four", "three", "two", "one"]
```

```bash
PYTHONDONTWRITEBYTECODE=1 /tmp/cordis-tutorial-venv/bin/python -m pytest -p no:cacheprovider -q -k cleanup
```

### Checkpoint J

```text
清理的三条不变量
  全部执行     ：一个 disposer 抛错不影响其余
  逆序执行     ：后安装的先释放，与安装顺序相反
  聚合上报     ：BaseExceptionGroup("effect cleanup failed", [...])
  只执行一次   ：idempotent close()，账本也只 +1
```

## Step 13：旧 epoch 的 listener 与 task 必须失效

"替换成功"最容易被忽略的两处残留：**事件订阅**和**定时任务**。

订阅的失效靠 scope 记录：

```python
    def listener(self, callback: Listener) -> Listener:
        if not callable(callback):
            raise TypeError("listener callback must be callable")
        target = self._owner_target()
        listener_id = self._scope.next_listener_id
        self._scope.next_listener_id += 1
        self._scope.listeners[listener_id] = _ListenerRecord(
            target.generation, callback
        )

        def release() -> None:
            record = self._scope.listeners.get(listener_id)
            if record is not None and record.generation == target.generation:
                self._scope.listeners.pop(listener_id, None)

        target.register_listener(listener_id, callback)
        self._owner_install()(release, f"listener: {listener_id}")
        return callback
```

`emit()` 只触发当前 generation 的订阅：

```python
    def emit(self, payload: object) -> list[object]:
        generation = self._owner_target().generation
        results: list[object] = []
        for record in tuple(self._scope.listeners.values()):
            if record.generation != generation:
                continue
            results.append(record.callback(payload))
        return results
```

于是一个很旧的 Context 连"重新注册"都做不到——它已经被 `_detach`，任何新资源都会撞上 `StaleEpochError`：

```python
    with pytest.raises(StaleEpochError):
        first_context.listener(lambda payload: None)
    with pytest.raises(StaleEpochError):
        first_context.provide("late", 1)
    with pytest.raises(StaleEpochError):
        first_context.effect(lambda: (lambda: None))
```

定时任务的失效靠 `release_tasks()`：

```python
    def release_tasks(self) -> int:
        """Cancel every task scheduled by this epoch; return how many had run."""

        released = sum(1 for handle in self.tasks if handle.fired)
        for handle in self.tasks:
            handle.cancel()
        self.tasks.clear()
        return released
```

`cancel()` 之后的 `TaskHandle.invoke()` 是空操作，所以即使到期时间已经过去，旧一代的任务也不会执行：

```python
    ticks: list[str] = []
    long_task = reloader.schedule("provider", 1_000, lambda: ticks.append("old"))
    short_task = reloader.schedule("provider", 10, lambda: ticks.append("short"))
    reloader.advance(10)
    assert ticks == ["short"]

    reloader.on_change(replace_event("provider", "app:provider", 2))
    reloader.drain()
    assert long_task.cancelled is True and long_task.fired == 0
    assert target.history[0].tasks is False  # the epoch released its task refs

    assert reloader.advance(10_000) == 0
    assert ticks == ["short"]  # the replaced generation's task never ran
```

```bash
PYTHONDONTWRITEBYTECODE=1 /tmp/cordis-tutorial-venv/bin/python -m pytest -p no:cacheprovider -q -k old_epoch
```

### Checkpoint K

```text
旧一代失效的四条证据
  listener 不再进入 emit 的结果
  旧 Context 无法再注册 listener / provide / effect / set
  task handle 被 cancel()，到期也不执行
  它们对应的 disposer 各自只运行一次（账本 = 1）
```

## Step 14：`close()` 必须归零，且可重复调用

关闭是最后一道防线。它要回答的问题是：**所有权的根在哪里？**

本章的答案：`HotReloader` 拥有所有 generation；`target.effects`（当前 epoch 的 scope）拥有该代的 Effect、listener 与 service；`target.tasks` 拥有该代的定时任务。

```python
    def close(self) -> None:
        """Release every generation once; repeated calls are no-ops."""

        if self.closed:
            return
        self.closed = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._pending.clear()
        self.batches.clear()
        errors = self._dispose_all()
        self.errors.extend(errors)
        if errors:
            raise BaseExceptionGroup("hot reloader close failed", errors)
```

三处细节：

1. `self.closed` 在开头就置位，所以第二次调用直接返回，不会重跑清理；
2. 未决的 debounce 定时器与待处理事件被丢弃——关闭后不应该再有新的批处理；
3. `_dispose_all()` 逐个释放并**收集**错误，最后一次性抛 `BaseExceptionGroup`。

归零可以被逐项断言：

```python
    reloader.close()

    assert reloader.closed is True
    assert reloader.generations == {}
    assert reloader.generation("provider") is None
    assert reloader.pending_events == 0
    assert clock.pending() == 0
    assert task.cancelled is True and task.fired == 0
    assert provider.context.has("value") is False
    assert provider.context.listener_count() == 0

    for generation in (provider.target, worker.target):
        generation.state.check()  # raises AssertionError if anything leaked
        assert generation._scope is None or len(generation._scope) == 0
```

`EpochState.check()` 把"泄漏"变成一条断言：

```python
    def check(self) -> None:
        """Raise when any resource of this generation is still registered."""

        leaked = [
            name
            for name, alive in (
                ("effects", self.effects),
                ("listeners", self.listeners),
                ("tasks", self.tasks),
            )
            if alive
        ]
        if leaked:
            raise AssertionError(f"generation {self.generation} leaked {leaked}")
```

最后是幂等性：第二次、第三次 `close()` 不产生任何新的清理，账本快照不变。

```python
    snapshots = (
        dict(provider.target.cleanup_counts),
        dict(worker.target.cleanup_counts),
    )
    reloader.close()  # idempotent
    reloader.close()
    assert reloader.generations == {}
    assert (
        dict(provider.target.cleanup_counts),
        dict(worker.target.cleanup_counts),
    ) == snapshots
```

```bash
PYTHONDONTWRITEBYTECODE=1 /tmp/cordis-tutorial-venv/bin/python -m pytest -p no:cacheprovider -q -k close
```

### Checkpoint L

```text
close() 契约
  幂等            ：第二次调用直接返回
  零资源          ：generation / listener / task / effect / service 全部归零
  错误聚合        ：close 内部的 cleanup 失败被收集成 BaseExceptionGroup
  拒绝后续输入    ：on_change / mount 在 closed 之后抛 HotReloadError
```

## Step 15：整章一起跑

```bash
cd Tutorial/20-hmr/src
PYTHONDONTWRITEBYTECODE=1 /tmp/cordis-tutorial-venv/bin/python -m pytest -p no:cacheprovider -q
```

十条测试覆盖的契约：

| 测试 | 覆盖的契约 |
| --- | --- |
| `config_change_reuses_the_fiber_and_keeps_the_generation` | config-only 原地换 epoch，generation 不变 |
| `module_change_replaces_the_generation_and_uses_the_new_version` | 换代成功路径，旧账本归档且干净 |
| `debounce_merges_a_burst_into_one_batch` | burst 合并，窗口边界可逐毫秒断言 |
| `burst_with_a_module_event_is_classified_as_structural` | 分类优先级，同批 config 不丢失 |
| `failed_candidate_keeps_the_previous_generation_running` | 解析失败零扰动 + 激活失败回滚 |
| `cleanup_failure_is_aggregated_in_reverse_order_without_stopping` | 清理失败聚合、逆序、不中断、幂等 |
| `invalid_update_keeps_the_current_epoch_installed` | 非法配置在释放前被拒绝 |
| `old_epoch_listener_and_tasks_never_fire_after_replacement` | 旧 epoch 的订阅与任务全部失效 |
| `close_releases_every_resource_exactly_once` | 零资源、只清理一次、幂等 close |
| `host_input_is_validated_at_the_boundary` | 事件校验、未挂载 entry、未知模块 |

### Checkpoint M

```text
本章建立的能力
  宿主事件  → ChangeEvent 校验
  合并      → debounce_burst + ManualClock（无真实时间）
  分类      → config-only / structural
  事务      → 校验 → 释放旧 epoch → 激活新 epoch
  回滚      → 解析失败零扰动 / 激活失败重新挂载旧声明
  清理      → 逆序、全部执行、聚合上报、只执行一次
  失效      → 旧 epoch 的 listener / task / Context 一律不可用
  关闭      → 归零、幂等
```

## 与正式源码对应

- 宿主驱动重载（唯一入口）：[`src/cordis/loader/reloader.py`](../../cordis-py/src/cordis/loader/reloader.py)
- 事务、回滚与 `BaseExceptionGroup` 聚合：[`src/cordis/loader/runtime.py`](../../cordis-py/src/cordis/loader/runtime.py)
- 组合层（include/overlay）如何保持失败不扰动：[`src/cordis/loader/composer.py`](../../cordis-py/src/cordis/loader/composer.py)
- 设计说明与安全边界：[`docs/architecture/07-configuration-loader.md`](../../cordis-py/docs/architecture/07-configuration-loader.md)
- 上一章：Loader 的挂载与事务 [../17-loader/03-update-replace-and-rollback.md](../17-loader/03-update-replace-and-rollback.md)

| 本章教学概念 | 正式实现中的位置 | 说明 |
| --- | --- | --- |
| 换代后 generations 归零 | `Loader.close()` + `_dispose_forest()` | 结构对应：都走所有权路径 |
| 清理失败聚合 | `EffectScope` 等价的聚合清理 | 结构对应：不因第一个异常停止 |
| 回滚用 `previous` 声明 | `Loader.replace()` 的 `previous` 还原 | 结构对应：第 17 章已建立 |
| `HotReloadTarget.history` | 正式实现没有该概念 | 教学扩展：为可观察性引入 |

再次强调：**正式实现只有 host-driven reload 基础。** `ConfigReloader` 不创建 watcher、不轮询、不刷新模块缓存；模块代码重载由宿主刷新 import 后显式调用 `Loader.replace()`。本章的并行结构（事件 → debounce → 分类 → 事务）是围绕这个事实推导出来的教学契约。

## 与 DSH 及扩展边界对照

| DSH HMR 概念 | 本章表达 | 状态 |
| --- | --- | --- |
| HMR 不得绕过 Effect 清理 | 所有资源经同一个 scope，账本可验证 | 概念对应：本章测试覆盖 |
| reload 期间服务依赖由 Fiber 状态机协调 | **本章不实现** | 简化：Core 没有依赖 epoch |
| 模块删除 dispose 对应 Entry | **本章不实现** | 缺口：没有 delete 事件类型 |
| 文件监视 / watch 生命周期 | **不实现** | 宿主责任 |
| 与 DSH HMR 1.0.15 等价 | **不宣称** | HMR 1.0.15 在教学范围之外 |

三条必须写明的边界：

1. **不与 DSH HMR 1.0.15 等价。** 本章不实现 watcher、不读 mtime、不做真实 `importlib` 缓存刷新，也没有模块热替换闭环；它只证明"宿主送来事件时运行时所需的不变量"。
2. **不与 `cordis-py` 的现有能力混淆。** 正式仓库当前只有 host-driven reload 基础（`ConfigReloader.start()`/`reload()`），没有 `HotReloader`、没有 `ChangeEvent`、没有 debounce。
3. **`HotReloader` 是教学候选契约。** 命名、API 与用途都只为本章推导服务；要成为正式 API，需要独立的 API inventory 与双语言行为场景。

## 本章刻意简化

- 无 OS watcher：不监听文件系统、不轮询、不创建后台任务；
- 无 mtime：不做基于时间戳的变化判定，事件完全由宿主喂入；
- 无真实 `importlib` 缓存刷新：`MappingModuleSource` 用 mapping 与整数版本替代；
- 无并发：所有 `activate` / `update` / `replace` 都是同步的，本章的确定性来自"只有一个执行流"；
- 无依赖驱动重建：替换不会因为 `inject` 变化而重排，因为 Core 没有 Inject；
- 无 entry 增删：只处理已挂载 entry 的配置变化与模块换代。

## 检查理解

1. 回滚为什么必须用 `previous`（`ParsedEntry` 声明）重新挂载，而不能把被释放的旧 Fiber "叫回来"？
2. `install()` 里的账本递增为什么放在 `finally` 而不是 `try` 末尾？如果放在 `try` 末尾，验收测试会漏掉什么失败？
3. `close()` 先置 `self.closed = True` 再清理。如果先清理再置位，哪些测试会变得不稳定？
4. 本章明确不实现 watcher 与 mtime。请写出宿主侧为了补上这个缺口需要做的三件事，以及它们各自为什么不该进入 Cordis 核心。
5. `GenerationSnapshot.clean` 只检查 effects/listeners/tasks 三个布尔。如果你要新增"Timer 句柄"这一类资源，需要改动哪三处？哪些测试会因此变严？
