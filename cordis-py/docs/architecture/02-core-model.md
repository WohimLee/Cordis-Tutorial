# 02. 核心对象模型

## Context

Context 是插件看到的唯一运行时入口：

```python
class Context:
    root: Context
    fiber: Fiber
    reflect: ReflectService
    registry: RegistryService
    events: EventsService
    logger: LoggerService

    def extend(self, **metadata) -> Context: ...
    def isolate(self, name: str, label: object | None = None) -> Context: ...
    def intercept(self, name: str, config: object) -> Context: ...
    def plugin(self, plugin: Plugin, config: object = None) -> Fiber: ...
    def effect(self, setup: EffectSetup, label: str = "anonymous") -> Disposable: ...
```

Context 不复制服务。服务由 Root Context 保存，子 Context 通过服务名和隔离标签解析实现。

在 TypeScript Cordis 中，Context 是 Proxy。读取 `ctx.llm` 会依次判断原生属性、查询服务定义、选择当前隔离域、验证依赖权限，并返回本次 Fiber 激活时绑定的实现。Python 使用 `__getattr__` 和显式 `get()` 表达同一语义，不能把动态服务降级为普通字段。

- `extend()` 只增加元数据；
- `isolate()` 改变指定服务的解析标签；
- `intercept()` 为某服务累计插件级配置；
- 所有派生操作都不得修改父 Context。

## Service

Service 是占用稳定 Context 名称的能力对象：

```python
class Database(Service):
    provide = "database"

    def __init__(self, ctx, config):
        super().__init__(ctx)
```

构造时调用 `ctx.provide(name, self)`。注册属于当前 Fiber，Fiber 卸载时自动撤销。一个隔离作用域内同名服务最多有一个活动实现。

Service 可声明默认服务名、额外可用性检查、配置验证器和专用配置合并器。

## Plugin

支持三种插件形态：

```python
def plugin(ctx, config): ...


class PluginClass:
    def __init__(self, ctx, config): ...


class PluginObject:
    def apply(self, ctx, config): ...
```

Registry 将其统一为：

```python
@dataclass(frozen=True)
class PluginSpec:
    callback: Callable
    name: str
    inject: Mapping[str, object | None]
    provide: tuple[str, ...]
    validator: ConfigValidator | None
    intercept: frozenset[str]
```

插件通过函数属性、类属性或装饰器声明元数据。Registry 只在边界处规范化一次，Fiber 不重复判断插件形态。

必须始终区分：Plugin 是定义，Fiber 是该定义某次运行的实例，Context 是该实例看到的运行环境。

## PluginRuntime

同一 callback 对应一个 Runtime，但可以挂载多次：

```python
@dataclass
class PluginRuntime:
    spec: PluginSpec
    fibers: set[Fiber]
```

`registry.delete(plugin)` 卸载该 Runtime 的全部 Fiber。Runtime 不保存单次挂载配置。

## Fiber

Fiber 代表一次 `ctx.plugin()` 调用，持有：

- 唯一 uid；
- 父 Context 和插件 Context；
- PluginRuntime；
- 原始配置和验证后配置；
- Inject 声明；
- 当前依赖实现快照；
- 当前 epoch；
- Effect 树和清理任务；
- 启动错误和生命周期状态。

同一插件多次挂载产生多个 Fiber，它们拥有独立配置、作用域和 Effect。

## 所有权关系

- Root Fiber 拥有顶层 Plugin Fiber；
- Plugin Fiber 通过 Effect 拥有子插件；
- Fiber 拥有激活期间创建的 Service、Hook 和用户资源；
- Runtime 仅建立索引，不替代父 Fiber 的所有权；
- Context 是能力视图，不独立拥有资源。

示例所有权树：

```text
Parent Fiber
├── Effect: provide("foo")
├── Effect: on("message")
└── Effect: Child Fiber
    ├── Effect: provide("bar")
    └── Effect: timer
```

销毁 Parent 会递归撤销整棵子树，插件不需要在全局维护资源 id。

## 运行时拓扑

Cordis Core 可以从九个关键概念理解：Plugin 定义行为，PluginRuntime 索引同一 Plugin 的所有挂载，Registry 创建 Fiber，Fiber 管理一次挂载的生命周期，Context 提供带作用域的能力视图，Reflect 解析 Service，Effect 管理可撤销资源，Events 负责通信，Logger 负责日志。Service 是 Plugin 通过稳定 Context 名称提供的能力。

```text
Application
├── Root Context
│   ├── RegistryService
│   ├── ReflectService
│   ├── EventsService
│   └── LoggerService
└── Root Fiber
    ├── Plugin Fiber A
    │   ├── Plugin Context A
    │   ├── dependency snapshot
    │   ├── Effects
    │   │   ├── Service registration
    │   │   ├── Event listener
    │   │   └── user resource
    │   └── Child Fiber B
    │       ├── Plugin Context B
    │       └── Effects
    └── Plugin Fiber C
        ├── Plugin Context C
        └── Effects
```

Fiber 与 Context 表达两个相关但不同的结构：

```text
生命周期所有权                    Context 派生与作用域

Root Fiber                       Root Context
├── Fiber A                      ├── Plugin Context A
│   └── Fiber B                  │   ├── isolated Context
└── Fiber C                      │   └── Plugin Context B
                                 └── Plugin Context C
```

Fiber 树决定谁负责卸载谁。Context 派生结构决定插件从哪个作用域解析服务以及继承哪些 metadata、isolation label 和 intercept 配置。两者不严格同构：每个 Fiber 有一个主要 Plugin Context，但 `extend()`、`isolate()` 和 `intercept()` 可以在不创建 Fiber 的情况下继续派生 Context；这些派生 Context 仍指向原 Fiber。只有再次调用 `ctx.plugin()` 才会创建 Child Fiber 及其主要 Plugin Context。

### Context 派生形式

`Context.derive()` 是 Python 实现内部使用的统一构造原语。公开的 `extend()`、`isolate()` 和 `intercept()` 都先派生 Context，再修改各自负责的数据；`plugin()` 则由 Registry 创建新 Fiber，并为该 Fiber 派生主要 Plugin Context。

| 操作 | 新 Context | 新 Fiber | metadata | Service scope | Service intercept config |
| --- | --- | --- | --- | --- | --- |
| `Context.derive(parent, fiber=None)` | 是 | 否 | 复制 | 复制 | 复制 |
| `ctx.extend(meta)` | 是 | 否 | 继承并增加或覆盖 | 不变 | 不变 |
| `ctx.isolate(name, label=None)` | 是 | 否 | 继承 | 为指定 Service 选择新 label | 不变 |
| `ctx.intercept(name, config)` | 是 | 否 | 继承 | 不变 | 为指定 Service 追加一层配置 |
| `ctx.plugin(plugin, config)` | 是 | 是 | 继承 | 继承 | 继承，并应用 Plugin Inject 中的配置 |

`derive(parent, fiber=existing_fiber)` 可以显式替换新 Context 的 `fiber` 引用，但它本身不创建 Fiber。普通 Context 派生沿用 `parent.fiber`；`Registry.plugin()` 先创建 Fiber，再把该 Fiber 传给 `derive()`。因此“创建新 Context”和“创建新生命周期实例”是两个独立动作。

```text
ctx.extend()       ──▶ 新 Context，原 Fiber，附加 metadata
ctx.isolate()      ──▶ 新 Context，原 Fiber，切换 Service label
ctx.intercept()    ──▶ 新 Context，原 Fiber，追加 Service config
ctx.plugin()       ──▶ 新 Fiber + 新 Plugin Context
```

`Service.extend()` 不属于 Context 派生：

```text
Context.derive / extend / isolate / intercept
└──创建新的 Context 视图

Service.extend
└──创建未重新注册的 Service facade
    ├──共享原 Service 状态
    └──可以附加或覆盖 facade 属性
```

`Service.extend()` 不创建 Context、Fiber、Service implementation 或新的 isolation scope。它只派生调用者看到的 Service facade；原 Service 的注册记录和 owner Fiber 保持不变。

关键引用关系如下：

```text
Fiber.ctx             ──▶ 该次挂载的主要 Plugin Context
Context.fiber         ──▶ 当前 Context 所属的 Fiber
Fiber.runtime         ──▶ 共享的 PluginRuntime
PluginRuntime.fibers  ──▶ 同一 Plugin 的所有存活 Fiber
Fiber effects         ──▶ 当前 activation 创建的长期资源
Service Impl.fiber    ──▶ 提供该 Service 的 Fiber
```

Context 是能力视图，不拥有所解析的服务。Service implementation、Listener、Child Fiber 和用户资源最终都由 Fiber 的 Effect 机制管理。Child Fiber 没有独立于 Effect 的第二套清理路径；它的 disposer 是 Parent Fiber 拥有的 Effect，因而与其他资源一起按逆注册顺序清理。

## 共享资源与局部资源

| 层级 | 资源 | 生命周期与可见性 |
| --- | --- | --- |
| Application | Root Context、Root Fiber、Registry、Reflect、Events、Logger | 创建一次；派生 Context 使用绑定到调用方 Context 的 facade，共享同一 backing service。 |
| Plugin identity | PluginRuntime | 同一 callback 一份，索引该 Plugin 的所有存活 Fiber，不拥有这些 Fiber。 |
| Context | metadata、isolation label 映射、intercept 配置、当前 Fiber 引用 | 从父 Context 派生；改变能力视图，不复制 backing service。 |
| Fiber | uid、配置、Inject、依赖实现快照、状态、activation epoch、Effect | 每次 `ctx.plugin()` 一份；由 Parent Fiber 管理。 |
| Service scope | Service implementation | 存入共享 Reflect backing store，由服务名与 isolation label 选择，生命周期属于 Provider Fiber。 |
| Effect | setup、一个或多个 disposer、诊断 metadata | 属于创建它的 Fiber；卸载时完整等待并逆序清理。 |

“共享”不等于所有 Context 看到相同结果。Registry、Reflect、Events 和 Logger 的 backing service 由 Root Context 建立，但 facade 保留调用方 Context；Reflect 会依据当前 Context 的 isolation label 和 Fiber dependency snapshot 选择 Service implementation，Events 会依据 dispatch Context 过滤 Hook，Logger 会依据 Context intercept 解析名称和级别。

## 树与图的组合

Cordis 使用树表达所有权，使用图表达连接关系：

```text
树
├── Fiber tree：插件与资源的生命周期所有权
├── Context derivation：metadata、isolation 和 intercept 的继承
└── Effect metadata tree：嵌套资源的诊断与逆序清理

图
├── PluginRuntime ──▶ Fiber：一个 Plugin identity 对应多个挂载
├── Consumer Fiber ──▶ Service Impl：多对多依赖
└── Event ──▶ Hook：多发布者、多订阅者通信
```

服务依赖图可以跨越 Fiber 树分支。一个 Provider Fiber 可以被多个 Consumer Fiber 依赖，一个 Consumer Fiber 也可以注入多个 Service。Reflect 中的实现变化会刷新受影响的 Consumer；Fiber 使用新的依赖实现集合建立 activation epoch，而不是由 Registry 预先生成固定拓扑顺序。

## 主要运行路径

```text
ctx.plugin(plugin, config)
    ↓
Registry 规范化 Plugin，并获取或创建 PluginRuntime
    ↓
创建 Fiber 和主要 Plugin Context
    ↓
Reflect 检查 Inject 所需的 Service implementation
    ↓
依赖完整时，Fiber 运行 Plugin callback
    ↓
Service、Listener、Child Fiber 和用户资源进入 Effect 所有权
```

读取服务时，Context 把 `ctx.service_name` 交给 Reflect。Reflect 验证当前 Fiber 的 Inject 权限，按服务名和 isolation label 找到实现，并返回当前 activation snapshot 绑定的值。永久销毁 Fiber 时，Cordis 使 Fiber 停止激活，统一逆序清理 Effect，等待 Child Fiber、Service 注销、Listener 移除和用户 disposer 完成，最后进入 `DISPOSED`。
