# Lifecycle 不变量与 Cordis 对照

## 本章真正建立的规则

Dependency Epoch 不是一个为了记录版本号而存在的字段。它保证下面这些 **invariant**，也就是任何执行顺序下都必须成立的规则：

1. Plugin 只在全部 Inject Service available 时 ACTIVE；
2. ACTIVE Fiber 使用的 Impl identity 与当前解析结果一致；
3. 旧 Epoch 的 Cleanup 未完成时，新 Epoch 不执行 callback；
4. LOADING 结果过期后必须 Cleanup，不能发布 ACTIVE；
5. 多次通知可以合并，但不能遗漏最终状态；
6. DISPOSED Fiber 不再创建 Epoch。

## 为什么不保存递增数字

可以给每次变化增加版本号，但版本号本身不能说明具体哪个 Service 变了，也不能确认依赖是否恢复。

Impl identity tuple 直接表达 Fiber 实际使用的依赖：

```python
(database_impl, cache_impl)
```

只要 tuple 中任意 identity 改变，Epoch 就改变。重新解析还能自然得到 `None`，表示依赖不完整。

## 与正式源码对应

- Fiber dependency epoch 和 runner：[`src/cordis/fiber.py`](../../cordis-py/src/cordis/fiber.py)
- Impl identity 与 strict resolution：[`src/cordis/reflect.py`](../../cordis-py/src/cordis/reflect.py)
- dependent Fiber 索引：[`src/cordis/registry.py`](../../cordis-py/src/cordis/registry.py)

正式实现比本章教学版多出失败状态、配置更新、Effect tree、内部 Lifecycle events 和更完整的取消/错误聚合，但核心原则相同：以 Impl identity 计算 Epoch，并串行完成卸载和激活。

## 与 DSH Cordis 对照

DSH Cordis 的 Fiber 同样会：

- 在每次依赖通知后重新检查 Impl；
- Service 丢失时卸载 Consumer；
- Service 恢复时激活同一 Fiber 的新 Epoch；
- 在 LOADING 和 UNLOADING 交错时拒绝发布过期结果；
- 将永久 dispose 与暂时 PENDING 区分开。

TypeScript 使用 Promise 组织 runner，Python 使用 `asyncio.Task`。这是异步工具的语言差异，不改变 Lifecycle 顺序。

## 本章暂不处理

- 不同 isolation label 的 Epoch；
- Inject mapping 中的 Intercept config；
- restart、update 和 FAILED；
- Cleanup 多错误聚合；
- 全部 owner/dispose 竞态。

下一章把 isolation 和 Intercept 加入已经建立的依赖解析，而不会改变单一 runner 和 Epoch 原则。

## 检查理解

1. 为什么 Epoch 要保存 Impl identity，而不是 Service value？
2. LOADING callback 成功后为什么还要重新解析依赖？
3. 新 Provider 在 UNLOADING 时到达，为什么不能立刻启动？
4. PENDING 与 DISPOSED 的根本区别是什么？
5. 多次 Reflect notify 为什么不需要创建同样数量的 activation Task？
