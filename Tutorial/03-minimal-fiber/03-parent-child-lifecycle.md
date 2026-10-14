# Parent 与 Child Fiber

Plugin 可以继续挂载另一个 Plugin：

```python
def parent(ctx):
    child = ctx.plugin(child_plugin)
```

新 Fiber 的 parent 是当前 Context 对应的 Fiber。

## Step 5：把 Child Fiber 也看成资源

Child Fiber 是一种长期资源。Parent 卸载时，Child 也必须卸载。

我们不建立另一套 child cleanup 列表，而是把 `child.dispose` 注册成 Parent 拥有的 Effect：

```python
child = Fiber(self, callback, self.fiber)
self.fiber.install_effect(lambda: child.dispose)
child.start()
```

这样 Plugin 返回值、`ctx.effect()` 创建的资源和 Child Fiber 都沿用同一条清理路径。

运行：

```bash
uv run pytest Tutorial/03-minimal-fiber/src/test_chapter03.py -k child_fiber
```

预期顺序是：

```text
parent activate
child activate
child cleanup
parent cleanup
```

为什么 Child 先清理？Fiber 在执行 Parent callback 前，已经把 Parent 的主 Effect 放入 owner。callback 执行期间创建的 Child disposer 排在主 Effect 后面。逆序清理时，Child 自然排在前面。

## Step 6：root Fiber

root Context 也需要一个 Fiber。它不对应普通 Plugin，而是整棵 Fiber 树的根：

```text
root Fiber
└── Plugin Fiber
    └── Child Plugin Fiber
```

调用 `await root.aclose()` 等于 dispose root Fiber。因为每个 Child Fiber 都是 Parent 的 Effect，整棵树会从叶子向根部清理。

运行：

```bash
uv run pytest Tutorial/03-minimal-fiber/src/test_chapter03.py -k root_close
```

## Step 7：LOADING 时收到 dispose

异步 Plugin 可能还在 setup：

```text
LOADING：等待 ready
```

这时调用 dispose，Fiber 会先等待 activation Task。Plugin 完成 setup 后，Fiber 立即进入 UNLOADING 并清理刚创建的资源。

本章只处理这个最小顺序，不处理依赖 Epoch 同时变化的情况。

运行：

```bash
uv run pytest Tutorial/03-minimal-fiber/src/test_chapter03.py -k loading
```

### Checkpoint C

```text
root Context
└── root Fiber
    └── Plugin Fiber
        ├── Plugin 返回值 Effect
        ├── ctx.effect() Effect
        └── Child Fiber disposer Effect
```

每个长期资源现在都有一个 Fiber owner。

## 与正式源码对应

- Context 与 Fiber 连接：[`src/cordis/context.py`](../../cordis-py/src/cordis/context.py)
- Fiber Lifecycle：[`src/cordis/fiber.py`](../../cordis-py/src/cordis/fiber.py)
- Effect 所有权：[`src/cordis/effect.py`](../../cordis-py/src/cordis/effect.py)

正式实现需要处理依赖 Epoch、失败恢复、restart、update、诊断 metadata 和更多竞态。本章只建立所有后续能力依赖的最小所有权闭环。

## 与 DSH Cordis 对照

DSH Cordis 同样让每个 Plugin Fiber 拥有它创建的 disposer，并把 Child Fiber 的 dispose 纳入 Parent Lifecycle。Python 使用 `asyncio.Task` 表示正在进行的 activation 和 disposal；TypeScript 使用 Promise。

两边共同维持的关键语义是：

- `ctx.plugin()` 立即返回可等待的 Fiber；
- Plugin 在属于该 Fiber 的 Context 中执行；
- Effect 随 owner Fiber 卸载；
- Parent dispose 会等待 Child dispose；
- Fiber 一旦进入 DISPOSED 就不再激活。

## 本章没有解决什么

下一章实现 **PluginRuntime 与 Registry**，解决 Plugin identity、同一 Plugin 多次挂载和按 Plugin 查找 Fiber 的问题。

此时还没有 Inject，因此 Consumer 仍然必须排在 Provider 后面。

## 检查理解

1. 为什么 Context 不能代替 Fiber 表示一次 Plugin 挂载？
2. 为什么 Child Fiber 应通过 Effect 进入 Parent 的清理路径？
3. 为什么 `fiber.dispose()` 需要返回共享 Task？
4. LOADING 时收到 dispose 请求，为什么仍要处理 setup 已经创建的资源？
