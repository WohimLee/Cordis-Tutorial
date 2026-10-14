# Fiber 关联、默认名称与可观测性边界

日志必须能回答“这条消息来自哪次插件挂载”。但记录关联本身不能改变生命周期，否则日志就会变成资源泄漏的另一个来源。

## Step 8：Fiber 关联是弱引用

`LoggerService.create` 在构造 Logger 时写入 Fiber 元数据：

```python
"meta": {"fiber": weakref.ref(context.fiber)}
```

`write` 构造 Message 时把 `fiber` 从 meta 中取出，作为独立参数传入：

```python
fiber = fields.pop("fiber", None)
exporter.export(Message(sn, ts, name, type, level, args, fiber, **fields))
```

三个后果：

- 日志持有的是弱引用，不会阻止 Fiber 被回收；
- exporter 可以用 `message.fiber()` 得到当时的 Fiber，也可以安全处理它已经消失的情况；
- Fiber 仍然活着时，`message.fiber() is fiber` 成立，测试可以直接断言。

测试还验证了所有权边界：Fiber `dispose()` 之后仍然被父 EffectScope 持有（父 Fiber 需要它来做级联清理），直到根 Context `aclose()` 完成，弱引用才真正失效。

```bash
uv run pytest Tutorial/14-logger/src/test_chapter14.py -k fiber_reference
```

## Step 9：默认名称来自 Fiber，而不是调用栈

```python
for value in context.intercepts.get("logger", ()):
    if isinstance(value, Mapping):
        config.update(value)
resolved = name or config.get("name") or _hyphenate(context.fiber.name)
```

- 显式 `ctx.logger("name")` 优先；
- 其次读取 `Context.intercept("logger", ...)` 的 `name`；
- 否则用注册 Fiber 的名字并连字符化：`CamelCase` → `camel-case`。

选择“注册时的 Fiber 名字”而不是运行时调用栈，意味着日志名称稳定、可预测，也不依赖昂贵的栈捕获。

拦截同样可以覆盖级别：

```python
intercepted = context.intercept("logger", {"name": "renamed", "level": LoggerLevel.ERROR})
intercepted.logger().debug("dropped")
```

测试证明 `debug` 被过滤、`error` 通过且名称为 `renamed`。

```bash
uv run pytest Tutorial/14-logger/src/test_chapter14.py -k hyphenation
```

## Step 10：Core Logger 与 cordis_observer 的边界

正式项目把日志拆成两层：

| 层 | 位置 | 内容 |
| --- | --- | --- |
| Core Logger | [`src/cordis/logger.py`](../../cordis-py/src/cordis/logger.py) | `LoggerLevel`、`Message`、`Logger`、`LoggerService`、`Exporter`、`defaultFormatters`、缓冲与 exporter 生命周期 |
| 学习用观察器 | [`src/cordis_observer/logger.py`](../../cordis-py/src/cordis_observer/logger.py) | 只有 `MemoryExporter`：收集 Message 并提供格式化文本 |

`cordis_observer.MemoryExporter` **不是** Cordis Core 的一部分，也不是 package-root API。它复用 Core 的 `Exporter` 与 `Logger.format` 契约，不复制日志分发、级别或格式化机制。

本章的测试遵守同一条边界：`MemoryExporter` 定义在 [`src/test_chapter14.py`](src/test_chapter14.py) 里，作为测试辅助，而不是 `chapter14` 的公开能力。这样读者不会误以为“内存收集器”是 Runtime 的一部分。

## 与正式源码对应

- 级别、Message、格式化、缓冲、exporter 生命周期：[`src/cordis/logger.py`](../../cordis-py/src/cordis/logger.py)
- `ctx.logger(...)` 与 `ctx.exporter(...)`：[`src/cordis/context.py`](../../cordis-py/src/cordis/context.py)
- Fiber 名字与元数据：[`src/cordis/fiber.py`](../../cordis-py/src/cordis/fiber.py)
- 教学观察器边界：[`src/cordis_observer/logger.py`](../../cordis-py/src/cordis_observer/logger.py)
- 行为测试：[`tests/test_logger.py`](../../cordis-py/tests/test_logger.py)
- 跨语言场景：[`020-logger-contracts.json`](../../cordis-py/tests/compat/scenarios/020-logger-contracts.json)、[`021-logger-options.json`](../../cordis-py/tests/compat/scenarios/021-logger-options.json)
- 设计说明：[`docs/architecture/09-observability.md`](../../cordis-py/docs/architecture/09-observability.md)

## 与 DSH Cordis 对照

分类引用 [`docs/cordis-core-compatibility.md`](../../cordis-py/docs/cordis-core-compatibility.md) 的 Logger 一节。表中“状态”是**正式 cordis-py** 相对 DSH Cordis 的结论；“本章实现”一列说明教学代码覆盖到哪一步。

| 契约 | 状态 | 本章实现 |
| --- | --- | --- |
| `LoggerLevel` | Exact | 名称与数值一致 |
| `Message` | Exact | 字段、序号、参数与 Fiber 关联一致 |
| `Logger` 四个级别方法 | Exact | 方法名和延迟格式化一致 |
| `LoggerOptions` | Equivalent | 同一 options 映射；meta 覆盖顺序一致 |
| `Exporter` | Equivalent | Python 用结构 Protocol |
| `defaultFormatters` | Exact | 占位符表一致；`%C` 只保留文本行为 |
| `c16` / `c256` | Exact | 未实现（见下一节） |
| `LoggerType`、`LoggerMethod`、`Formatter` | Equivalent | 以 callable 类型别名表达 |
| `LoggerService.Intercept` | Equivalent | 通过 `Context.intercept("logger", ...)` 传递 |
| LoggerService 可调用 | Equivalent | `__call__` 创建命名 logger |
| level 继承、缓冲、exporter 生命周期 | Exact | 三层阈值顺序、有界缓冲与 Effect 撤销 |

正式实现还导出 `c16`、`c256` 和自己的配色选择函数，本章没有实现，见下节。

## 本章刻意简化

- 没有 `c16` / `c256` 调色板和 `Logger.code()` / `Logger.color()`。`%C` 只返回 `str(value)`。配色属于终端呈现细节，把它带进主线会掩盖级别、消息和 exporter 生命周期这三个真正的契约。
- `Effect` 只保留同步安装和 callable 形态（`ctx.exporter(...)` 不需要等待）；可等待的 Effect 见第 10、15 章。
- Fiber 只保留 `PENDING/LOADING/ACTIVE/FAILED/UNLOADING/DISPOSED` 中本章用得到的子集行为，没有依赖 epoch 和 refresh runner。
- 没有 `internal/dispatch` 上的日志诊断、Console/JSON/文件 exporter 和指标接口。
- `_BufferExporter` 是内部实现细节；正式实现通过 `LoggerService.buffer`/`bufferSize` 暴露同样的可观察状态。

## 检查理解

1. 为什么 `sn` 会在阈值过滤之前分配？这会不会影响“关闭级别不格式化”的保证？
2. `Message.fiber` 为什么必须是弱引用？如果它是强引用会泄漏什么？
3. 三级阈值（命名 / `default` / logger 自身）为什么必须按这个顺序查找？
4. `log.error(wrapper)` 在 `wrapper.__cause__` 存在时为什么产生两条消息？
5. 为什么把 `MemoryExporter` 放在 `cordis_observer` 而不是 Core？
6. 如果 exporter 在 `export()` 中抛错，应该由谁负责处理？本章的 `write` 为什么没有捕获它？
