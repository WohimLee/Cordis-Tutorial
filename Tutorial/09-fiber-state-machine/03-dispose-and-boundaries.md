# Dispose、状态通知与本章边界

## Step 7：`dispose()` 进入不可逆终态

依赖丢失只是暂时卸载，最终状态是 `PENDING`；`dispose()` 则永久结束 Fiber：

```text
ACTIVE → UNLOADING → DISPOSED
FAILED → UNLOADING → DISPOSED
PENDING ───────────→ DISPOSED
```

DISPOSED Fiber 不接受 restart 或 update，也不会响应后续 refresh。多次 dispose 仍然只清理一次。

这正是下面两个说法的差别：

```text
“现在不能运行” → PENDING
“以后永远不运行” → DISPOSED
```

## Step 8：生命周期通知描述已经发生的变化

`internal/status` listener 在 state 写入之后调用，因此 callback 中：

```python
def listener(fiber, old_state):
    print(old_state, "->", fiber.state)
```

状态通知用于观察，不用于建立第二套 scheduler。真正决定下一状态的仍是 Fiber runner。

正式 Runtime 还会在最终删除时发送 `internal/plugin`，让 Registry 和外部观察者知道 Fiber identity 已移除。

## 与正式源码对应

- Fiber 状态与 lifecycle runner：[`src/cordis/fiber.py`](../../cordis-py/src/cordis/fiber.py)
- Plugin config validation：[`src/cordis/config.py`](../../cordis-py/src/cordis/config.py)
- 生命周期内部事件：[`src/cordis/events.py`](../../cordis-py/src/cordis/events.py)
- Registry 删除 Fiber：[`src/cordis/registry.py`](../../cordis-py/src/cordis/registry.py)

## 与 DSH Cordis 对照

本章保留以下可观察语义：

- 六个正式状态名称及其职责；
- `await fiber` 与 `wait()` 等待当前 lifecycle 稳定；
- 激活错误进入 FAILED，并由等待者看到；
- 同一 failed epoch 不自动反复启动；
- restart 强制新 activation；
- update 验证配置并驱动重启；
- dispose 永久且幂等；
- `internal/status` 提供 old state，new state 从 Fiber 读取。

Python 没有名为 `await` 的可调用方法，因为 `await` 是语言关键字。Cordis-py 使用 `await fiber` 和 `fiber.wait()` 表达同一能力。

## 本章故意留下的问题

本章 runner 保证生命周期操作串行，但还没有系统展开这些交错情况：

- LOADING 中发生 dispose；
- LOADING 中 Dependency Epoch 改变；
- UNLOADING 中依赖恢复；
- cleanup 自己失败。

它们需要可控制的异步门闩和错误聚合，会在第 11 章专门处理。第 10 章先把 Service、listener、child Fiber、task 等资源统一成 Effect，才能定义完整的清理边界。

## 检查理解

1. PENDING 和 FAILED 为什么不能合并？
2. 为什么同一个 failed epoch 不应自动重试？
3. restart 与 dependency recovery 有什么区别？
4. 为什么 invalid update 不应先卸载 ACTIVE epoch？
5. DISPOSED 为什么不能像 PENDING 一样恢复？
6. 状态 listener 为什么只能观察，不能取代 Fiber runner？
