# Impl 所有权与变化通知

## Step 6：`provide()` 必须可逆

`provide()` 返回一个 disposer：

```python
dispose = ctx.provide("message", "hello")
dispose()
```

Disposer 只删除自己创建的 Impl，不会误删同名的其他 Provider。重复调用返回 `False`，不会重复修改 Reflect。

## Step 7：让 Fiber 拥有 Impl

注册 Impl 时，同时把 disposer 交给 Provider Fiber：

```python
context.fiber.own(dispose)
```

Fiber dispose 时逆序执行自己拥有的 disposer。因此 Provider 卸载后，它提供的所有 Service 都会自动消失。

运行：

```bash
uv run pytest Tutorial/05-service-reflection/src/test_chapter05.py -k fiber_dispose
```

这里仍然遵守 Effect 的核心思想：长期资源必须有 owner 和 disposer。本章为缩小代码只保存 Cleanup；第 06 章重新接入完整 Fiber/Effect 路径。

## Step 8：通知 Service 变化

Reflect 知道 Impl 何时加入和删除，但它暂时不知道哪些 Consumer 依赖这个 Service。我们先提供一个最小 watcher：

```python
dispose_watch = ctx.reflect.watch("message", callback)
```

每次 `message` Impl 改变时，callback 收到 Service name：

```text
provide Impl → notify("message")
dispose Impl → notify("message")
```

运行：

```bash
uv run pytest Tutorial/05-service-reflection/src/test_chapter05.py -k notification
```

Watcher 还不是 Cordis 的依赖调度器。下一章会让 Registry 根据 Plugin `.inject` metadata 建立 Consumer Fiber，并在通知到达时重新检查依赖。

### Checkpoint C

```text
Provider Fiber
    └── owns Impl disposer

Reflect
├── 按 name + label 保存 Impl
├── strict / loose 解析
├── availability check
├── Provider shadow / restore
└── provide / dispose 时通知变化
```

## 与正式源码对应

- Reflect、Impl、Property：[`src/cordis/reflect.py`](../../cordis-py/src/cordis/reflect.py)
- Context 的 `get()`、`provide()`、`isolate()`：[`src/cordis/context.py`](../../cordis-py/src/cordis/context.py)
- Service availability：[`src/cordis/service.py`](../../cordis-py/src/cordis/service.py)

正式 Reflect 还管理 accessor、mixin、internal/get、internal/set、service notification filter 和 caller Context binding。这些内容分别留到 Service 与 Events 章节。

## 与 DSH Cordis 对照

DSH Cordis 使用 Proxy、Symbol 和 prototype 把 Service 看起来表现成 `ctx.database`。Python 没有相同对象模型，因此正式 Cordis-py 使用 Context-bound Reflect view 和 `__getattr__`，但保留相同能力：

- Service resolution 同时包含 name 与 isolation label；
- strict lookup 不暴露尚未 ACTIVE 或 unavailable 的 Service；
- Impl 记录 Provider Fiber；
- disposer 精确删除自己注册的 Impl；
- Impl 变化触发依赖重新检查。

## 本章没有解决什么

- Consumer 还不会声明 Inject；
- Service 变化只发通知，不会自动卸载和重新激活 Plugin；
- 没有 accessor、mixin 和 `Context.set()`；
- 没有完整 Service class。

下一章实现 **Inject 与延迟激活**，第一次把 Registry、Fiber 和 Reflect 连接成响应式依赖系统。

## 检查理解

1. 为什么 Impl 必须记录 Fiber，而不能只保存 value？
2. strict lookup 为什么不能返回 LOADING Provider 的 Service？
3. 为什么同名 Service 要保存列表，而不是覆盖字典值？
4. isolation 为什么要比较 label identity？
5. Reflect 通知变化后，为什么不应该自己直接执行 Consumer Plugin？
