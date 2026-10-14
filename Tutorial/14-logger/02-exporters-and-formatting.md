# Exporter 生命周期与格式化

级别和 Message 解决“产生什么”，Exporter 决定“送到哪里”。如果 Exporter 不能被安全替换，日志就会成为另一个绕过 Effect 的长期资源。

## Step 4：Exporter 是结构协议，注册是 Effect

```python
class Exporter(Protocol):
    def export(self, message: Message) -> None: ...
```

Python 用 Protocol 表达 Cordis 的结构类型：任何带 `export(message)` 的对象都可以注册，不需要继承。

注册走和 listener、service 完全相同的路径：

```python
def exporter_for(self, context, exporter):
    serial = 0

    def setup():
        nonlocal serial
        serial = self._add_exporter(exporter)
        return lambda: self.exporters.pop(serial, None)

    return context.fiber.effects.install_sync(setup, "ctx.exporter()")
```

于是：

- Exporter 在注册后立刻生效；
- 注册它的 Fiber 卸载时自动撤销；
- 撤销后新消息不再送给它，但已经导出的 Message 仍然属于调用者。

测试用一个插件注册 exporter，`await fiber.dispose()` 之后继续写日志，确认捕获数量不再增长，且 `exporters` 中只剩下内部缓冲。

```bash
uv run pytest Tutorial/14-logger/src/test_chapter14.py -k exporter_lifecycle
```

## Step 5：格式化默认由 exporter 触发

`Logger.format(exporter, message)` 是公开的纯函数，默认的 Console/JSON/内存 exporter 都在真正输出时调用它。它有三个必须记住的规则。

```python
def format(exporter, message):
    args = list(message.args)
    if args and isinstance(args[0], BaseException):
        args[0] = str(args[0])
        args.insert(0, "%s")
    elif not args or not isinstance(args[0], str):
        args.insert(0, "%o")
```

1. 第一个参数是异常时，转成字符串并按 `%s` 处理；
2. 第一个参数不是字符串（或没有参数）时，插入 `%o`，即 JSON 序列化；
3. `%%` 输出一个字面 `%`，未知占位符原样保留。

占位符表来自 `defaultFormatters`：

| 占位符 | 含义 |
| --- | --- |
| `%s` | `str(value)` |
| `%d` / `%i` | 取整 |
| `%f` | 浮点（整数值去掉小数点） |
| `%o` / `%O` | 紧凑 JSON |
| `%c` | 空字符串 |
| `%C` | 本章返回纯文本（配色见下文简化） |

exporter 可以覆盖单个占位符：

```python
exporter.formatters["s"] = record   # 自定义 %s
```

模板消费完后，剩余参数会追加到输出末尾：容器类型用 JSON，其他用 `str()`。因此 `log.info("left", 1, {"a": 1}, [2])` 得到 `left 1 {"a":1} [2]`。

```bash
uv run pytest Tutorial/14-logger/src/test_chapter14.py -k placeholders
```

## Step 6：截断按行进行

```python
maximum = getattr(exporter, "maxLength", 10240)
return "\n".join(
    line[:maximum] + ("..." if len(line) > maximum else "") for line in output.splitlines()
)
```

限制作用在每一行上，而不是整段文本：多行消息不会被压成一行，长行才加省略号。`maxLength` 属于 exporter，因此不同输出目的地可以有不同的限制。

## Step 7：异常参数的两条路径

```python
if len(args) == 1 and isinstance(args[0], BaseException):
    if args[0].__cause__:
        logger.error(args[0].__cause__)
    elif isinstance(args[0], BaseExceptionGroup):
        for error in args[0].exceptions:
            logger.error(error)
        return
```

- 有 `__cause__` 时，先单独记录 cause，**然后继续记录原异常本身**；
- 传入的是 `BaseExceptionGroup` 时，逐个记录其成员，并结束本次 write。

第二条 `return` 只属于 group 分支。这是可观察契约：`log.error(wrapper)` 产生两条消息（cause 与原异常），而 `log.error(group)` 每个成员各一条。

```bash
uv run pytest Tutorial/14-logger/src/test_chapter14.py -k exception_argument
```

### Checkpoint B

Exporter 承担了“过滤、格式化、截断、输出”四件事里的后三件，Logger 只负责级别和 Message 构造。这个分工让“换目的地”不需要改业务代码。
