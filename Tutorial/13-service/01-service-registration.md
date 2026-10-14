# 13 — Service：注册、所有权与 caller Context

第 05 章用 `provide(name, value)` 演示了服务注册，第 07 章用它验证了 Dependency Epoch。但当时只需要一个“值”：

```python
plugin_context.provide("value", 1)
```

真实插件提供的是有行为、有配置、有可用性判断的对象。这一章要回答三个容易混在一起的问题：

```text
谁拥有这个服务？        → Reflect 把它登记为 provider Fiber 的 Effect
consumer 拿到什么？     → ServiceView，绑定解析它的 Context
Service 自己负责什么？  → 名称、availability、init cleanup、config
```

## 本章目标

- `Service` 注册与 `provide`，以及 provider 卸载时的自动撤销；
- `init()` 返回的 cleanup 由 provider Fiber 拥有；
- caller Context tracking：`self.caller_context` 永远指向调用者；
- callable Service 与 `Service.extend()`；
- availability check 与依赖驱动的卸载/重新激活；
- `accessor` 与 `mixin`，并区分 Service、ServiceView 和 Reflect 的责任。

教学实现自包含在 [`src/chapter13.py`](src/chapter13.py)，仍然不导入 [`cordis`](../../cordis-py/src/cordis)，也不引入 Logger 和 Loader。

## Step 0：朴素服务表为什么会出错

最小实现可以只是一个字典：

```python
def provide(name, value):
    services[name] = value
    return lambda: services.pop(name, None)
```

它无法回答三个问题：

- provider Fiber 卸载时谁负责调用那个 disposer？
- 同一个 name 出现第二个 provider 时是覆盖还是报错？
- `ctx.cache` 返回原始对象，还是返回“通过这个 Context 解析出来”的视图？

第一个问题已经在第 10 章解决：任何长期资源都必须是 Effect。本章把服务注册写成 provider Fiber 上的同步 Effect：

```python
def setup():
    if label in self._implementations:
        raise CordisError(DUPLICATE_SERVICE, ...)
    self._implementations[label] = implementation
    return cleanup

context.fiber.effects.install_sync(setup, f"ctx.provide({name!r})")
```

`cleanup` 在 provider 卸载时执行，因此“服务消失”不是靠用户记得删除，而是 Effect 关闭的必然结果。

## Step 1：按 isolation label 存储实现

服务不能只按 name 存。第 08 章的 isolation 要求同名服务可以在不同作用域各自存在，所以键是 label：

```python
label = self.label(context, name)     # context.isolation 优先，否则共享默认 label
implementation = Impl(name, label, value, context.fiber, check)
```

`Impl` 记录了三个诊断必需的事实：名字、owner Fiber、可用性 check。`ReflectService` 只负责存取和通知，它不做依赖排序——排序仍然由 Fiber 的 refresh runner 决定。

运行：

```bash
uv run pytest Tutorial/13-service/src/test_chapter13.py -k registers_service
```

同一个作用域内重复 `provide` 同一个名字会抛 `CordisError(DUPLICATE_SERVICE)`，而且因为异常发生在激活期，Fiber 会进入 `FAILED` 并回滚：先注册的服务保持不变。这条测试同时验证错误类型和最终状态。

```bash
uv run pytest Tutorial/13-service/src/test_chapter13.py -k conflicts_raise
```

## Step 2：Service 只是“会自己注册的对象”

任何对象都能被 `provide`。`Service` 基类只加了四件事：

```python
class Service:
    provide: str | None = None
    Config: object | None = None

    def __init__(self, context, name=None):
        self.context = context
        self.name = name or self.provide or type(self).__name__.lower()
        context.provide(self.name, self, self.available)
```

- 名称来自 `provide` 声明、构造参数或类名的小写形式；
- 注册立刻发生在构造期，不需要插件再调用一次 `provide`；
- `available()` 是可覆盖的可用性谓词（下一步展开）；
- `init()` 由 Fiber 在构造之后调用，返回值作为 cleanup。

注意 Python 的一个拼写细节：容器类插件收到 `(ctx, config)` 两个参数，而 `Service.__init__` 的第二个参数是 `name`。所以 Service 子类通常显式声明

```python
def __init__(self, plugin_context, config=None):
    super().__init__(plugin_context)
```

否则 config 会被当成 name。

## Step 3：consumer 拿到的是 ServiceView

如果 `ctx.cache` 直接返回 Service 实例，服务方法内部就分不清“谁在调用”。`bind_service` 因此返回视图：

```python
def bind_service(value, context):
    if isinstance(value, Service):
        return ServiceView(value, context)
    return value
```

`ServiceView` 用 `__getattr__` 转发属性，但方法调用前后会设置一个 ContextVar：

```python
def bound(*args, **kwargs):
    token = _current_context.set(self._context)
    try:
        return callback(*args, **kwargs)
    finally:
        _current_context.reset(token)
```

于是 `Service.caller_context` 可以写成：

```python
@property
def caller_context(self):
    return _current_context.get() or self.context
```

- 方法通过某个 consumer Context 的视图调用时，`caller_context` 是那个 consumer；
- 直接读取（没有调用栈）时，退化为 provider 自己的 Context。

这就是“caller Context tracking”的全部机制：不是第二个服务存储，而是在调用边界上携带解析 Context。`__setattr__` 也转发到真实对象，所以对视图赋值等于改服务状态。

运行：

```bash
uv run pytest Tutorial/13-service/src/test_chapter13.py -k caller_context
```

### Checkpoint A

到这里有三层，职责不能混：

```text
ReflectService  存 implementation，按 label 解析，通知 dependents
Service         声明名称、availability、init cleanup 和 config
ServiceView     把一次解析绑定到调用方 Context
```

`Service` 不拥有服务（Fiber 才拥有），`ServiceView` 不存储状态（target 才存储）。
