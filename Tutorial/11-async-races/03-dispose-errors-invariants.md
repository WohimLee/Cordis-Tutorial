# Setup/Dispose 竞态与错误后的最终状态

## Step 5：dispose 必须等待正在运行的 setup

如果 owner 在 setup 完成前关闭，Effect 已经被登记，但 cleanup 还没产生：

```text
setup started
    │ await
dispose requested
    │ 等待
setup returns cleanup
    ↓
cleanup immediately runs
```

Effect 用 `setup_done` Event 建立这条顺序。Scope 先标记 closed，因此 setup 完成后也不能把 Effect 当成仍然可用的资源返回给调用者。

## Step 6：cleanup 失败也必须完成最终删除

一次 cleanup 抛错时：

- 继续执行其他 cleanup；
- 聚合所有错误；
- Fiber 仍进入 DISPOSED；
- identity 仍从 Registry 删除；
- 最后才把聚合错误交给调用者。

失败不能成为资源和 Registry 记录永久泄漏的理由。

## Step 7：dispose intent 具有最高优先级

一旦 `_dispose_requested` 为真：

- 后续 refresh 不再创建 runner；
- 当前 LOADING 完成后只会回滚；
- 当前 UNLOADING 完成后进入 DISPOSED；
- Service 恢复也不能重新激活 Fiber。

这保证“disposed Fiber never reactivates”。

## 与正式源码对应

- setup/dispose 协调与 cleanup 聚合：[`src/cordis/effect.py`](../../cordis-py/src/cordis/effect.py)
- serialized refresh runner：[`src/cordis/fiber.py`](../../cordis-py/src/cordis/fiber.py)
- 确定性竞态测试：[`tests/test_fiber_races.py`](../../cordis-py/tests/test_fiber_races.py)

## 与 DSH Cordis 对照

本章保留依赖变化期间的 rollback、恢复后的新 activation、幂等 dispose 和错误后的最终清理语义。Python 使用 `asyncio.Lock`、`Event`、Task 和 `BaseExceptionGroup` 表达这些约束。

TypeScript Promise 和 Python coroutine 的调度细节不同，所以对等目标不是复制每个 microtask 顺序，而是保持外部能观察到的状态、cleanup 次数和最终资源状态。

## 本阶段成果

第 09～11 章合在一起完成了可靠生命周期内核：

```text
状态机定义合法阶段
Effect 定义资源所有权
serialized runner 处理异步交错
```

接下来的 Events、Service、Logger 都必须建立在这套 ownership 上，不能绕开它创建长期资源。

## 检查理解

1. 为什么 refresh notification 可以合并，而 Dependency Epoch 检查不能省略？
2. LOADING 时依赖消失，为什么仍要等待 callback 产生 cleanup？
3. UNLOADING 时依赖恢复，为什么不能立即启动新 epoch？
4. Scope 为什么要在等待 setup 前先标记 closed？
5. cleanup 抛错后，Fiber 为什么仍必须进入 DISPOSED？
6. lifecycle lock 和 refresh flag 分别解决什么问题？
