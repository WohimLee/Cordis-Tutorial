# 10. 公共 API

## 兼容目标

公共层以 vendored `@deepseek-ai/cordis` 4.0.2 为规范。只要名称和调用形式在 Python 中合法且实用，就沿用 Cordis 的概念、名称、参数、返回契约和异常时机，而不是另外设计一套惯用 Python API。

每个公开项进入兼容矩阵并归为：完全一致、能力等价、语言特有、缺失或 Python 扩展。能力等价项必须通过同一场景的 TypeScript/Python 规范化结果对照；现有 Python 实现或本文示例本身不构成兼容证据。

## 使用草案

```python
from cordis import Context, Service, Fiber, FiberState, Inject

ctx = Context()
fiber = ctx.plugin(my_plugin, config)
await fiber.wait()

dispose_listener = ctx.on("app/ready", listener)
dispose_service = ctx.provide("cache", cache)

ctx.emit("event", value)
result = ctx.bail("decision", request)
result = await ctx.serial("decision", request)
await ctx.parallel("notification", payload)
result = await ctx.waterfall("pipeline", request, next_=default_handler)

await fiber.update(new_config)
await fiber.restart()
await fiber.dispose()
await ctx.aclose()
```

## 服务示例

```python
class Counter(Service):
    provide = "counter"

    def __init__(self, ctx):
        self.value = 0
        super().__init__(ctx)

    def next(self):
        self.value += 1
        return self.value
```

## 插件依赖示例

```python
def greeter(ctx, config):
    ctx.on("ready", lambda: print(ctx.counter.next()))


greeter.inject = ["counter"]
```

插件返回 disposer 或在 Context 上注册 Effect。用户无需手工把 listener/service 加入清理列表。

共享 Service 的注册方法通过 `self.caller_context` 创建调用方拥有的 Effect，确保 provider Fiber 卸载时自动撤销注册。

## API 分层

目标公共层：

- `Context`、`Service`、`Fiber`、`FiberState`；
- `Inject`、Plugin 形态与 `ctx.inject()`、`ctx.plugin()`；
- `CordisError`、`ValidationError`；
- Effect、Disposable、Plugin 和 ConfigValidator Protocol。

Cordis 公开的 ReflectService、RegistryService、EventsService 和 LoggerService 同样属于兼容面。只有上游实现私有的 epoch runner、临时缓存和索引不承诺对象布局一致。

## 类型策略

- 发布 `py.typed`；
- Service 和配置使用泛型；
- Plugin 定义为 Protocol，不限制函数、类和对象入口；
- Context 的动态服务无法完全由静态类型推导，建议应用定义带注解的 Context Protocol；
- 装饰器保留被包装对象类型；
- 公共 API 不暴露 `Any`，除非动态插件边界无法安全收窄。

## 语言差异边界

下列机制不机械复制，但必须保留其可观察能力或记录无法保留的理由：

- Proxy 用 `__getattr__`、描述符或显式方法表达；
- Symbol 用私有 sentinel、类属性或普通 metadata 表达；
- prototype inheritance 用明确的父 Context 和 metadata 继承表达；
- declaration merging 用 Protocol 和类型注解表达；
- explicit `this` 用显式 dispatch Context 和调用方绑定表达；
- thenable disposer 用 Python awaitable/callable 对象表达；
- `Fiber.await`、`Context.is` 和 `global` 等关键字冲突采用唯一、文档化的 Python 拼写。

Loader 的 Python 模块标识不在 Cordis Core API 对等范围内。

## 非目标

- 不保证 JavaScript 对象布局、prototype 链或 Symbol identity；
- 不为同一能力维护两套调度器、服务存储或 Effect 实现；
- 不因 Python 惯例任意改变一个本可直接表达的 Cordis API；
- 不把 DeepSeek Harness 业务服务或独立 Cordis Loader/HMR 插件计入 Core 对等结论。
