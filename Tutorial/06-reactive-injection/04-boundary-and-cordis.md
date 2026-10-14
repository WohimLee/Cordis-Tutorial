# 本章边界与 Cordis 对照

## 为什么 Reflect 不直接启动 Consumer

Reflect 只负责 Service implementation。它不知道 Plugin callback、config、当前 activation Task 或 Cleanup。

如果 Reflect 直接执行 Consumer，就会出现第二套 Lifecycle 调度器：

```text
Registry/Fiber 能启动 Plugin
Reflect 也能启动 Plugin
```

两个入口会让状态和清理顺序失去唯一答案。正确分工是：

```text
Reflect：Service 发生变化
Registry：哪些 Fiber 关心这个名称
Fiber：现在是否应该激活，以及怎样激活
```

## 为什么这不是拓扑排序

拓扑排序会在启动前计算固定顺序，例如 Provider A → Consumer B。Cordis 面对的 Service 会在运行期间出现、消失和替换，固定顺序很快失效。

本章采用事件驱动方式：每次 Service 变化，只通知相关 Fiber 重新检查当前条件。

## 与正式源码对应

- Inject metadata：[`src/cordis/model.py`](../../cordis-py/src/cordis/model.py)
- Registry dependency index：[`src/cordis/registry.py`](../../cordis-py/src/cordis/registry.py)
- Fiber 依赖检查：[`src/cordis/fiber.py`](../../cordis-py/src/cordis/fiber.py)
- Reflect notification：[`src/cordis/reflect.py`](../../cordis-py/src/cordis/reflect.py)

正式实现不会使用本章的公开 watcher。它通过 lifecycle-owned internal events 和 label-filtered notify 连接组件，并处理调用用户 callback 时不能持有全局 Runtime lock 的要求。

## 与 DSH Cordis 对照

DSH Cordis 的核心语义同样是：

- Inject 是声明式 metadata；
- 缺少任一依赖时 Plugin Fiber 保持 PENDING；
- Provider 晚到后 Consumer 自动激活；
- Loader 不需要按依赖手工排序 Plugin；
- Service resolution 包含 name 和 isolation label。

Python class decorator 的执行模型与 TypeScript decorator initializer 不同，因此正式 Cordis-py 会在 class 构造后扫描 method-level Inject。函数 Plugin 则直接使用 `.inject`，不使用 class/method decorator 机制。

## 本章故意留下的问题

如果 ACTIVE Consumer 依赖的 Service 被删除，本章 Fiber 不会卸载：

```text
Consumer ACTIVE
    ↓ database 消失
本章：仍然 ACTIVE  ← 错误但尚未处理
```

直接调用 `refresh()` 还不够。我们需要记录“这一次 activation 使用的是哪些 Impl”，并保证旧 Cleanup 完成后才能开始新 activation。

这就是下一章的 **Dependency Epoch**。

## 检查理解

1. PENDING 为什么是正常状态而不是错误？
2. Provider 为什么在进入 ACTIVE 后还要再次通知 Service？
3. Registry 的反向索引为什么不等于拓扑排序？
4. Reflect 为什么不能直接调用 Consumer callback？
5. Service 丢失时，仅仅再次检查依赖为什么不足以保证安全？
