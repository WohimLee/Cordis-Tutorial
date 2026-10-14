# 15 — package-root 导出与两个 Python 拼写

到这里 00–14 章已经覆盖了 Cordis Core 的全部能力。但读者仍然会问两个非常具体的问题：

```text
运行时的公共入口到底有哪些名字？
TypeScript 里能写的东西，Python 里该怎么写？
```

这一章不再引入新机制。它把已有能力整理成可核对的清单，并只保留唯一、文档化的 Python 拼写。

## 本章目标

- 对照 `cordis` package-root 的 41 个导出，标注每个契约由哪一章引入；
- 用可运行代码说明 `await fiber` 与 `fiber.wait()`；
- 说明 `Context.is_context()`；
- 说明 `EventOptions` 的 `global` 与 `Hook.global_`；
- 给出 Proxy、Symbol、prototype、thenable、AggregateError 的 Python 等价表达；
- 删除教学过程中的临时别名，不留第二套拼写。

教学实现自包含在 [`src/chapter15.py`](src/chapter15.py)，不导入 [`cordis`](../../cordis-py/src/cordis)。

## Step 0：先固定公共表面

正式实现的 package-root 导出集中在 [`src/cordis/__init__.py`](../../cordis-py/src/cordis/__init__.py) 的 `__all__`。本章把它抄成一份带章节归属的清单，作为可核对的数据：

```python
CORDIS_ROOT_EXPORTS: dict[str, str] = {
    "Context": "01",
    "Disposable": "02",
    "Effect": "02",
    ...
    "__version__": "extension",
}
```

| 契约 | 章节 | 说明 |
| --- | --- | --- |
| `Context` | 01 | 根/派生作用域 |
| `Disposable`、`Effect` | 02 | 可逆资源 |
| `Fiber` | 03 | 插件挂载 |
| `Plugin`、`PluginRuntime`、`RegistryService`、`PluginContext`、`PluginFunction`、`PluginObject` | 04 | 插件规范化与身份 |
| `Impl`、`ReflectService` | 05 | 服务解析 |
| `Inject` | 06 | 依赖声明 |
| `ConfigValidator`、`FiberState`、`ValidationError`、`resolveConfig` | 09 | 状态机与配置验证 |
| `EffectMeta` | 10 | Effect 诊断树 |
| `DispatchMode`、`EventOptions`、`EventsService`、`Hook`、`isBailed` | 12 | 事件系统 |
| `CordisError`、`CordisErrorCode`、`Property`、`Service` | 13 | 服务与错误 |
| `c16`、`c256`、`defaultFormatters`、`Exporter`、`Formatter`、`Logger`、`LoggerLevel`、`LoggerMethod`、`LoggerOptions`、`LoggerService`、`LoggerType`、`Message` | 14 | 日志 |
| `DisposableList` | later | 公共工具，第 16 章以后对照 |
| `__version__` | extension | Cordis-py 扩展，不属于 DSH Cordis |

清单里两个特殊标记必须显式存在，因为它们不是“还没讲到”，而是“本来就不属于 Core 对等”：

- `later`：尚未在教程章节中展开；
- `extension`：Python 项目自己的扩展，不构成兼容证据。

## Step 1：`await fiber` 与 `fiber.wait()`

TypeScript 里可以写 `await fiber`，因为 `await` 是关键字、`then` 是普通方法。Python 的 `__await__` 是协议，方法名不能叫 `await`，于是有两条等价路径：

```python
fiber = context.plugin(plugin)
waiter = asyncio.create_task(fiber.wait())   # 显式方法
assert await waiter is fiber
assert await fiber is fiber                  # __await__ 协议
```

两者都在“当前 refresh 工作全部稳定”后返回同一个 Fiber；`wait()` 在 `error` 非空时抛出激活错误，`await fiber` 同样如此。

测试用一个未 set 的 `Event` 固定顺序：Plugin 已经进入 callback 但还没返回时，`wait()` 必须仍未完成。

```bash
uv run pytest Tutorial/15-python-api/src/test_chapter15.py -k await_fiber
```

## Step 2：`Context.is_context()`

TypeScript 的 `Context.is(value)` 在 Python 中无法同名：

```python
@staticmethod
def is_context(value):
    return isinstance(value, Context)
```

`is` 是语法关键字，不能作为属性名，所以 `hasattr(context, "is")` 永远是 `False`。这不是能力缺失：身份判断的结果与 `010-context-filter` 场景一致。

```bash
uv run pytest Tutorial/15-python-api/src/test_chapter15.py -k is_context
```

### Checkpoint A

公共表面只有两种状态：已经由某一章引入，或被显式标记为 `later` / `extension`。没有“大概算 Core”的中间地带。

```bash
uv run pytest Tutorial/15-python-api/src/test_chapter15.py
```
