# 01. 总体设计

## 目标语义

Cordis 的核心不是插件加载器，而是一个追踪插件所有权、服务依赖和可撤销副作用的运行时。Loader 只是把声明式配置转换为 `ctx.plugin()` 调用。

```text
                      ┌──────────────────┐
                      │      Loader      │
                      │ config / import  │
                      │ update / rollback│
                      └────────┬─────────┘
                               │ ctx.plugin()
                               ▼
┌──────────────────────────────────────────────────────┐
│                    Cordis Core                       │
│  Context ─────────▶ Service Registry                 │
│     │                       │ service change          │
│     ▼                       ▼                         │
│   Fiber ◀──────────── dependency refresh             │
│     ├── owns effects, listeners and child fibers     │
│     ├── tracks required services                     │
│     └── controls load, unload and reload              │
│  Events: emit / parallel / serial / bail / waterfall │
└──────────────────────────────────────────────────────┘
```

Cordis-py 必须保留以下能力：

- 一切扩展能力均可表示为插件；
- Context 是带作用域的服务容器；
- 插件通过依赖声明决定何时激活，不依赖手工加载顺序；
- 每次插件挂载由独立 Fiber 管理；
- 服务、监听器、子插件和其他资源都是可逆 Effect；
- 依赖变化会使插件安全卸载，并在条件恢复后重新激活；
- 同步与异步加载、清理遵守同一套生命周期规则；
- 事件分发模式具有明确且稳定的语义。

## 核心范围

核心包 `cordis` 包含：

1. `Context`：上下文、服务访问、派生、隔离和拦截配置；
2. `ReflectService`：服务实现、动态属性和方法转发；
3. `RegistryService`：插件规范化、Runtime 注册和 Fiber 创建；
4. `Fiber`：依赖状态、配置、激活、卸载、重启和清理；
5. `EventsService`：监听器注册、过滤和事件分发；
6. `Service`：具名服务基类；
7. `LoggerService`：分层日志、级别控制和 Exporter；
8. Effect、Disposable、配置验证、错误类型和辅助数据结构。

Loader、Include、Group、HMR、Timer 和 Console Logger 作为可选官方插件，不进入最小核心。

## 三条运行时不变量

1. 每个副作用都有 owner：Effect、Service 和 Listener 属于 Fiber，Child Fiber 属于 Parent Fiber；
2. Fiber 是否运行由依赖决定：全部 Inject 可用才激活，任一依赖不可用就卸载；
3. 服务变化沿依赖图传播：provide/unregister 触发依赖重算，再触发 unload/reload。

后续所有模块设计都必须保持这三条不变量。

## 运行时结构

```text
Application
└── Root Context
    ├── ReflectService ── service/accessor store
    ├── RegistryService ── plugin runtimes and fibers
    ├── EventsService ── hooks and dispatch
    ├── LoggerService ── loggers and exporters
    └── Root Fiber
        ├── Plugin Fiber A
        │   ├── scoped Context
        │   ├── dependency snapshot
        │   └── Effects
        └── Plugin Fiber B
```

Root Context 保存全局注册表和服务实现表。派生 Context 共享根级运行时，但带有自己的 Fiber、隔离标签、拦截配置和元数据。

## 推荐代码布局

```text
src/cordis/
├── __init__.py
├── context.py
├── fiber.py
├── registry.py
├── reflect.py
├── service.py
├── events.py
├── logger.py
├── config.py
├── errors.py
├── typing.py
└── utils.py

src/cordis_loader/
├── loader.py
├── model.py
├── resolver.py
└── yaml.py
```

核心模块保持单向依赖。共享接口放在 `typing.py`；运行时交叉引用使用延迟注解或局部导入。

## Python 等价表达

- JavaScript Proxy 对应 `Context.__getattr__`、描述符和显式反射 API；
- Symbol 隐藏字段对应私有属性或内部哨兵对象；
- declaration merging 对应 Protocol、泛型和类型存根；
- decorator metadata 对应 Python 装饰器属性；
- thenable Fiber 对应 `await fiber.wait()` 或 `Fiber.__await__`；
- Standard Schema 对应可插拔配置验证协议。

语法无需逐项兼容，但公开行为和生命周期事件序列应可进行跨语言对照测试。
