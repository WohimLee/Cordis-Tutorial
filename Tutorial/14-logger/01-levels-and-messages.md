# 14 — Logger：消息、级别与缓冲

前面的章节都用列表和断言观察行为。真实 Runtime 需要的是结构化日志：谁在什么级别、从哪个 Fiber、发出了什么参数。直接用 `print` 会立刻出现四个问题：

```text
级别无法按名字过滤
消息没有序号和时间，无法排序或去重
调用者不知道消息属于哪个 Fiber
输出目的地被写死，无法在运行时替换
```

## 本章目标

- `LoggerLevel`、`Message` 与序列号；
- `Logger` / `LoggerService` / `LoggerServiceView` 的分工；
- level 继承顺序：命名级别 → `default` → logger 自身级别；
- 默认格式化、占位符、截断与有界缓冲；
- Fiber 关联（弱引用）与默认 logger 名称；
- 解释 Core Logger 与 `cordis_observer` 的边界。

教学实现自包含在 [`src/chapter14.py`](src/chapter14.py)，不导入 [`cordis`](../../cordis-py/src/cordis)，也不引入 Loader。

## Step 0：级别是数值，不是字符串

```python
class LoggerLevel(IntEnum):
    ERROR = 0
    INFO = 1
    WARN = 2
    DEBUG = 3
```

数字越小越严重，因此“允许输出”的条件是 `threshold < level` 时跳过。这与 Cordis 的 `LoggerLevel` 完全一致，也让 `levels` 配置可以直接比较。

`Logger` 的四个方法只是把 `(type, level, args)` 交给服务：

```python
def info(self, value, *args):
    self._write("info", LoggerLevel.INFO, (value, *args))
```

延迟格式化是这里的关键：`Logger` 不拼接字符串，只保存原始 `args`。测试因此可以证明被过滤的调用没有产生任何格式化动作。

## Step 1：Message 是唯一的事实载体

```python
class Message:
    def __init__(self, sn, ts, name, type, level, args, fiber=None, **meta):
        self.sn, self.ts, self.name = sn, ts, name
        self.type, self.level, self.args, self.fiber = type, level, args, fiber
        self.__dict__.update(meta)
```

三个字段值得单独说明：

- `sn` 由 `LoggerService` 单调递增，跨 exporter 共享，因此合并多个输出源后仍能排序；
- `ts` 是毫秒整数；
- `fiber` 是 `weakref.ref`，日志不会延长 Fiber 的生命周期。

## Step 2：级别继承有三层

`write` 里每个 exporter 单独计算阈值：

```python
levels = getattr(exporter, "levels", {})
threshold = levels.get(logger.name, levels.get("default", logger.level))
threshold = LoggerLevel.INFO if threshold is None else threshold
if threshold < level:
    continue
```

顺序是：

```text
exporter.levels[logger.name]   命名 logger 的精确配置
exporter.levels["default"]     该 exporter 的默认配置
logger.level                   创建 Logger 时声明的级别
INFO                           都没有时的兜底
```

注意两件容易混淆的事：

- 阈值判断发生在构造 `Message` 之前，被跳过的调用不会格式化、也不会进入任何 exporter；
- 但 `sn` 在阈值判断之前就分配好了，所以它表示“到达日志服务的 write 序号”，而不是“成功输出的消息数”。

运行：

```bash
uv run pytest Tutorial/14-logger/src/test_chapter14.py -k thresholds
```

## Step 3：有界缓冲是服务自己的 exporter

`LoggerService` 在构造时注册一个内部 exporter：

```python
def export(self, message):
    self.service.buffer.append(message)
    if len(self.service.buffer) > self.service.bufferSize:
        if self.service.bufferSize:
            del self.service.buffer[: -self.service.bufferSize]
        else:
            self.service.buffer.clear()
```

- 默认 `bufferSize = 1000`；
- `bufferSize = 0` 表示完全不保留历史；
- 缓冲是最后手段的诊断入口，任何用户 exporter 都能同时看到同样的 Message。

```bash
uv run pytest Tutorial/14-logger/src/test_chapter14.py -k buffer
```

### Checkpoint A

到这里，“日志”被拆成三个正交决定：

```text
级别  决定这条消息是否产生
Message 决定消息长什么样
exporter 决定消息去哪里
```
