# 从 NaiveRuntime 走向 Cordis

这一节不要求阅读正式源码，只把刚刚遇到的问题和 Cordis 的术语对应起来。

## 问题与 Cordis 概念的对应关系

| 我们遇到的问题 | Cordis 使用的概念 |
| --- | --- |
| Plugin 应该在什么作用域中运行 | **Context** |
| 谁记录 Plugin identity 和挂载结果 | **Registry** 与 **PluginRuntime** |
| 一次 Plugin 挂载和它的状态由谁表示 | **Fiber** |
| Plugin 创建的资源由谁拥有 | **Effect** |
| Service 应该怎样注册和查找 | **Reflect** 与 Service **Impl** |
| Consumer 怎样声明所需 Service | **Inject** |
| Service 变化后如何区分新旧运行 | dependency **Epoch** |

这些概念各有职责，但它们会组成一个闭环：

```text
Context 接收 Plugin
    ↓
Registry 创建 Fiber
    ↓
Reflect 检查 Inject 所需的 Service
    ↓
依赖完整时，Fiber 运行 Plugin
    ↓
Plugin 创建的资源成为 Fiber 拥有的 Effect
    ↓
Service 改变时，旧 Epoch 清理，新 Epoch 再激活
```

## 与正式 Python 源码对应

| 本章对象 | 正式实现 |
| --- | --- |
| 传给 Plugin 的共享字典 | [`Context`](../../cordis-py/src/cordis/context.py)，但 Context 不是普通字典 |
| `mount(plugin)` | `Context.plugin()` 与 [`RegistryService`](../../cordis-py/src/cordis/registry.py) |
| Plugin 的一次执行 | [`Fiber`](../../cordis-py/src/cordis/fiber.py) 的一个 activation Epoch |
| 全局 Cleanup 列表 | 每个 Fiber 拥有的 [`Effect`](../../cordis-py/src/cordis/effect.py) 树 |
| `services[name]` | [`ReflectService`](../../cordis-py/src/cordis/reflect.py) 管理的 Impl |

暂时不要深入这些文件。后面的章节会从简单代码开始，一步步得到正式结构。

## 与 DSH Cordis 对照

DSH Cordis 调用 `ctx.plugin()` 后，不一定马上执行 Plugin：

```text
缺少 Inject 依赖
    ↓
PENDING：等待

依赖完整
    ↓
LOADING：正在启动
    ↓
ACTIVE：正在运行

依赖丢失
    ↓
UNLOADING：清理旧资源
    ↓
PENDING：等待恢复

永久销毁
    ↓
DISPOSED：以后不能复活
```

本章的 `NaiveRuntime` 不具备这些行为。它是为了暴露问题而写的起点，不是 Cordis 的简化兼容版本。

## 本章没有解决什么

下一章只引入最小 **Context**，解决“Plugin 应该接收怎样的作用域对象”。下一章仍然不会实现 Fiber、Effect 或响应式 Inject。

## 检查理解

请先自己想一想：

1. 如果给 `mount()` 增加 `requires=["message"]`，缺少依赖的 Plugin 应该保存在哪里？
2. Provider 后来出现时，谁负责找到等待中的 Consumer？
3. Consumer 再次运行前，怎样确认旧资源已经全部清理？
4. 如果异步 Cleanup 还没有结束，新 Epoch 能不能立即启动？

这些问题说明，依赖解析、生命周期状态和资源所有权不能各自独立处理。Cordis 的主要工作，就是让它们按照同一套规则协作。
