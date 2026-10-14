# Dispatch Context、filter 与 internal 事件

前面所有例子都只有一个事件总线。真实 Runtime 需要把“谁在观察”与“谁被允许观察”分开，也需要让 Framework 自己成为事件系统的使用者。

## Step 8：用 dispatch Context 过滤本地 Hook

分发时可以在事件名前面放一个 Context：

```python
context.emit(dispatch_context, "filtered", value)
```

事件系统据此解析出三件事：

```text
dispatch_context, name, args = self._resolve_dispatch(name, args)
```

- 第一个参数是 Context 时，它就是 dispatch Context，事件名是第二个参数；
- 只有 Context 而没有事件名时立即 `TypeError`，不能静默地把它当成事件名。

过滤谓词来自 dispatch Context 的 metadata：

```python
filter_ = dispatch_context.metadata(dispatch_context.filter)
...
hook.global_ or filter_ is None or filter_(hook.ctx)
```

普通 Hook 只在注册 Context 通过过滤时触发，`global_=True` 的 Hook 无条件触发。这就是第 08 章隔离语义在事件层的落点：服务通知只需要触达同一作用域的消费者。

运行：

```bash
uv run pytest Tutorial/12-events/src/test_chapter12.py -k global_hooks
```

## Step 9：internal/listener 允许替换注册

`on()` 在执行真正的注册前，先以 `bail` 模式询问 `internal/listener`：

```python
replacement = self.bail(
    context, "internal/listener", name, listener,
    {"prepend": prepend, "global": global_},
)
```

- 返回 `None` 表示不拦截，走正常注册；
- 返回 callable 表示替换：它成为本次 `on()` 返回的 disposer；
- 返回非 callable 且非 `None` 立即 `TypeError`，避免“看似注册成功，实际拿到一个不可撤销的东西”。

替换路径不再创建 Hook，也不安装 Effect：撤销责任转移给了替换者。测试用一个可变标志证明 disposer 的返回值被原样透传（第一次 `True`、第二次 `False`），这正是 `internal/listener` 能实现懒注册、缓存注册或代理注册的原因。

```bash
uv run pytest Tutorial/12-events/src/test_chapter12.py -k replace_registration
```

## Step 10：internal/dispatch 是分发诊断

每次公开分发都会报告模式、名称、参数和 dispatch Context：

```python
self.emit_safe("internal/dispatch", mode, name, args, dispatch_context)
```

两条规则保证它只是诊断：

- 名称以 `internal/` 开头的事件不报告，否则诊断自身会递归；
- 报告走 `emit_safe`，观察者抛错不会影响本次分发。

`parallel` 报告的模式字符串是 `"emit"`，与正式实现一致：并行分支本身不是第六种模式，它只是 `emit` 的等待版本。

```bash
uv run pytest Tutorial/12-events/src/test_chapter12.py -k internal_dispatch
```

### Checkpoint C

事件系统到这里有四个层次，缺一层都会出问题：

```text
Hook 记录注册 Context 和 global_ 标记
dispatch Context 决定本次分发的可见性
五种模式决定顺序、并发和短路
internal 事件让 Framework 参与而不破坏业务分发
```

## 与正式源码对应

- 五种模式、`isBailed`、filter 与 Hook：[`src/cordis/events.py`](../../cordis-py/src/cordis/events.py)
- Context-bound facade（`ctx.on/emit/bail/serial/parallel/waterfall`）：[`src/cordis/context.py`](../../cordis-py/src/cordis/context.py)
- Effect 所有权与 `install_sync`：[`src/cordis/effect.py`](../../cordis-py/src/cordis/effect.py)
- Fiber 与 `assertActive()`：[`src/cordis/fiber.py`](../../cordis-py/src/cordis/fiber.py)
- 行为测试：[`tests/test_events.py`](../../cordis-py/tests/test_events.py)、[`tests/test_internal_events.py`](../../cordis-py/tests/test_internal_events.py)
- 跨语言场景：[`tests/compat/scenarios/012-event-contracts.json`](../../cordis-py/tests/compat/scenarios/012-event-contracts.json)、[`010-context-filter.json`](../../cordis-py/tests/compat/scenarios/010-context-filter.json)

## 与 DSH Cordis 对照

以下分类直接引用 [`docs/cordis-core-compatibility.md`](../../cordis-py/docs/cordis-core-compatibility.md) 的 Events 一节；本章教学代码本身不是跨语言证据。

| 契约 | 状态 | Python 表达 |
| --- | --- | --- |
| `isBailed(value)` | Exact | `None` 和 `False` 继续，不使用 truthiness |
| `DispatchMode` | Exact | 五个模式字符串完全相同 |
| `on` / `once` / prepend / 幂等 disposer | Exact | 注册顺序、一次性移除和返回值一致 |
| `emit` | Equivalent | awaitable listener 通过运行中的 event loop 调度 |
| `bail` | Equivalent | 异步 listener 立即返回的 awaitable 也视为 bail |
| `serial` / `waterfall` | Equivalent | Python 暴露 async 方法 |
| `parallel` | Equivalent | 等待全部 listener，失败聚合为 `BaseExceptionGroup` |
| `EventOptions` / `Hook` | Equivalent | `EventOptions` 保持 `{"global": ...}`，存储的 Hook 字段拼作 `global_` |
| dispatch `thisArg` | Equivalent | 前导位置接受 dispatch Context |
| Context filter 与 global listener | Equivalent | `Context.filter` 是不透明 sentinel |
| `internal/listener` | Exact | 替换 disposer 与 options 参数一致 |
| `internal/dispatch` | Exact | 参数顺序与模式上报一致 |

区别只有两处，且都有语言原因：`global` 是 Python 关键字，所以 Hook 字段只能是 `global_`；`Context.filter` 在 TypeScript 里是 Symbol key，在 Python 里退化为模块级 sentinel 对象，但过滤行为一致。

## 本章刻意简化

- 没有 Reflect，因此注册时不做 `reflect.bind(listener)`，listener 内部不携带注册 Context 的服务访问能力；第 13 章补上 caller/registration Context tracking。
- 没有 `waterfall_sync`：正式实现为同步调用方提供独立入口，本章只保留 async `waterfall`。
- 只实现 `internal/listener` 与 `internal/dispatch`。`internal/plugin`、`internal/status`、`internal/config`、`internal/service`、`internal/update`、`internal/get`、`internal/set` 属于 Fiber/Registry/Reflect 章节。
- 教学 Fiber 用 `StrEnum` 状态和单个 activation task 代替正式实现里的 epoch runner。
- `Fiber.get_effects()` 返回 label 元组，只用于验证所有权；正式实现的 `EffectMeta` 还带 children 树。
- `EventsService.pending_tasks` 是教学诊断，不是 Cordis API。

## 检查理解

1. 为什么 `on()` 的 disposer 重复调用返回 `None`，而 `internal/listener` 替换路径的返回值却必须透传？
2. `emit` 遇到 async listener 时为什么不能直接 `await`？被调度的 task 失败后，异常去了哪里？
3. `bail` 用 `if result:` 会破坏哪些实际用例？
4. `parallel` 为什么在慢 listener 完成前不能抛出已发生的错误？
5. dispatch Context 与注册 Context 分别决定什么？`global_=True` 为什么必须绕过 filter？
6. `internal/dispatch` 为什么必须走 `emit_safe`，并且跳过 `internal/` 名称？
