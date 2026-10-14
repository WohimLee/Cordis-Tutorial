# 兼容矩阵与教学别名清理

前面每一步都为了解释机制而临时简化过命名和形状。本章最后一步把这些痕迹清掉：教程的公共表面必须与文档化的 Python 拼写一一对应，不能同时存在两套。

## Step 9：把每个机制分类

`API_MATRIX` 是本章的可执行数据，每一项都说明 Cordis 机制、分类、Python 拼写和引入章节：

```python
@dataclass(frozen=True, slots=True)
class ApiEntry:
    concept: str
    status: Status
    python: str
    chapter: str
    note: str
```

| Cordis 机制 | 状态 | Python 契约 |
| --- | --- | --- |
| `Context()` | Exact | `Context()` |
| `Context.is(value)` | Language-specific | `Context.is_context(value)` |
| `ctx.extend(meta?)` | Exact | `Context.extend(meta)` |
| Context Symbol keys | Language-specific | `Context.filter` sentinel + `Context.metadata()` |
| `Fiber.await()` | Language-specific | `await fiber` / `fiber.wait()` |
| `EventOptions { global }` | Equivalent | 键 `global` + 字段 `global_` |
| thenable disposer | Equivalent | 可 await 且可调用的 `Effect` |
| Proxy | Equivalent | `ServiceProxy.__getattr__` + ContextVar |
| Symbol tracking | Language-specific | 不透明 sentinel identity |
| prototype inheritance | Equivalent | `Context.derive` 复制元数据 |
| `AggregateError` | Equivalent | `BaseExceptionGroup` |
| `Plugin.Runtime` namespace | Language-specific | 导出的 `PluginRuntime` |
| decorator initializers | Language-specific | 类/方法装饰器 + 构造后扫描 |
| Standard Schema symbol | Equivalent | `ConfigValidator` 适配器 |
| V8 stack rewriting | Language-specific | 原生 traceback + Effect label |

分类词汇只有四个：`exact`、`equivalent`、`language-specific`、`extension`，与 [`docs/cordis-core-compatibility.md`](../../cordis-py/docs/cordis-core-compatibility.md) 一致。

## Step 10：删除临时别名

教学过程中最危险的退化是“为了顺手”留下第二套拼写：

```python
await_fiber(fiber)          # 等价于 fiber.wait()
is_context(value)           # 等价于 Context.is_context(value)
AggregateError = BaseExceptionGroup
```

这些名字在正式实现里都不存在。本章的测试逐个断言它们**不可用**，同时断言 `__all__` 中的每个名字都真实存在：

```python
for alias in ("await_fiber", "is_context", "AggregateError", "global_", "is_"):
    assert not hasattr(chapter15, alias)
```

保留别名看起来方便，代价是兼容矩阵再也无法回答“Python 到底用哪个拼写”。删除之后，每个能力只剩一个入口。

```bash
uv run pytest Tutorial/15-python-api/src/test_chapter15.py -k alias_free
```

### Checkpoint C

到这里，读者可以用三种方式核对任何一条 API：

```text
CORDIS_ROOT_EXPORTS   它是不是 package-root 导出？由哪一章引入？
API_MATRIX            它是 Exact / Equivalent / Language-specific？
兼容矩阵              它有什么跨语言证据？
```

## 与正式源码对应

- package-root 导出清单：[`src/cordis/__init__.py`](../../cordis-py/src/cordis/__init__.py)
- `Context.is_context`、`extend`、metadata：[`src/cordis/context.py`](../../cordis-py/src/cordis/context.py)
- `Fiber.wait()` 与 `__await__`：[`src/cordis/fiber.py`](../../cordis-py/src/cordis/fiber.py)
- `ServiceView` 与 caller Context：[`src/cordis/service.py`](../../cordis-py/src/cordis/service.py)
- `Hook.global_` 与 dispatch 过滤：[`src/cordis/events.py`](../../cordis-py/src/cordis/events.py)
- `DisposableList` 与语言特有工具分类：[`src/cordis/utils.py`](../../cordis-py/src/cordis/utils.py)
- 完整语言差异表：[`docs/cordis-core-compatibility.md`](../../cordis-py/docs/cordis-core-compatibility.md)
- 公共 API 设计说明：[`docs/architecture/10-public-api.md`](../../cordis-py/docs/architecture/10-public-api.md)
- 相关场景：[`010-context-filter.json`](../../cordis-py/tests/compat/scenarios/010-context-filter.json)、[`023-service-facades.json`](../../cordis-py/tests/compat/scenarios/023-service-facades.json)、[`024-remaining-contracts.json`](../../cordis-py/tests/compat/scenarios/024-remaining-contracts.json)

## 与 DSH Cordis 对照

兼容矩阵的最终语言差异表与本章 `API_MATRIX` 一一对应：

| Cordis 机制 | Python 契约 | 原因 |
| --- | --- | --- |
| `Context.is()` | `Context.is_context()` | `is` 是 Python 语法 |
| `Fiber.await()` | `await fiber` 或 `fiber.wait()` | `await` 不能作为方法名 |
| `global` Hook 字段 | `EventOptions` 保持 `global`，存储字段用 `global_` | `global` 是关键字 |
| Promise/thenable disposal | 可调用且可等待的 `Effect` | 原生 await 协议 |
| Proxy 与 Symbol tracking | Context 绑定视图、sentinel 与显式字段 | 没有 prototype/Symbol identity |
| decorator initializers | 装饰器 + 构造后扫描 | 装饰器没有 initializer 钩子 |
| Standard Schema symbol | `ConfigValidator` 适配器 | 没有共享 symbol 属性 |
| `AggregateError` | `BaseExceptionGroup` | 原生聚合异常 |
| V8 stack rewriting | 原生 traceback + Effect label | 栈字符串不可移植 |
| `Plugin.Runtime` namespace | 导出的 `PluginRuntime` | 类型联合不能携带命名空间 |

每一条都能在 00–14 章里找到对应实现；本章不新增运行时能力，只负责把它们固定下来。

## 本章刻意简化

- 教学 `Context` 只保留 metadata、事件总线和 Fiber 挂载；Registry、Inject、隔离和 epoch 已在第 04–09 章单独实现，本章不重复。
- 教学 `Fiber` 只保留 `PENDING/ACTIVE/FAILED/DISPOSED`，没有依赖 epoch、refresh runner 和父子级联 Effect 所有权。
- 教学 `Events` 只保留 `on`、`emit` 和 `parallel`；`serial`/`bail`/`waterfall` 与 `internal/*` 属于第 12 章。
- `ServiceProxy` 不实现 `__call__` 和属性删除；第 13 章的 `ServiceView` 才是完整的服务视图。
- `DisposableList`、`Tracker`、`isConstructor`、`joinPrototype` 等公共工具不在本章代码里，只在清单中标记为 `later` / `language-specific`。
- `CORDIS_ROOT_EXPORTS` 是 41 个名字的快照，不通过导入 `cordis` 动态获取。

## 检查理解

1. 为什么 `await fiber` 和 `fiber.wait()` 必须是同一个契约，而不是两个近似方法？
2. `EventOptions` 保持 `global` 而 Hook 字段叫 `global_`，这会不会造成两套行为？
3. 把 `AggregateError` 直接别名成 `BaseExceptionGroup` 有什么问题？
4. `Context.filter` 为什么用 sentinel 而不是字符串常量？
5. 为什么 `CORDIS_ROOT_EXPORTS` 里的 `later` 和 `extension` 必须显式标记，不能省略？
6. 如果某天需要给 `Fiber.wait()` 加一个更短的别名，应该同时更新哪三处证据？
