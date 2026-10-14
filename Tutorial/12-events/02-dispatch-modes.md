# 五种分发模式

所有权解决“listener 什么时候消失”，分发模式解决“一次事件如何触达 listener”。两者必须分开，否则每个调用点都要自己决定顺序、并发和短路规则。

## Step 3：emit 是同步广播，async listener 交给事件循环

`emit` 按注册顺序同步调用每个 callback：

```python
for callback in self._callbacks(name, dispatch_context):
    result = callback(*args)
    if inspect.isawaitable(result):
        self._schedule(result)
```

同步 `emit` 不可能等待一个协程，但也绝不能丢弃它。Cordis 的语义是 fire-and-forget：把 awaitable 交给事件循环，并把 task 记录在 `_tasks` 中，完成回调负责取回异常，避免“Task exception was never retrieved”。

测试用两个 `Event` 固定顺序，而不是靠时间：

```python
context.emit("async")
assert not entered.is_set()   # emit 已经返回，listener 主体还没运行
await entered.wait()          # listener 确实被调度了
assert not completed.is_set() # emit 没有等待它
release.set()
await completed.wait()
```

运行：

```bash
uv run pytest Tutorial/12-events/src/test_chapter12.py -k schedules_awaitable
```

生命周期诊断使用 `emit_safe`：逐个调用 observer，逐个收集异常，最后返回错误元组。观察者失败不能中断 Framework 清理。

## Step 4：bail 的判定不能用 truthiness

`bail` 返回第一个 bail 值，但“什么算 bail”必须与 Cordis 完全一致：

```python
def isBailed(value):
    return value is not None and value is not False
```

于是 `False` 和 `None` 继续，`0`、`""` 和任何对象都会停止分发。Python 的 `if result:` 会错误地把 `0` 和 `""` 当成“继续”，这正是需要显式 `is not` 的原因。

```bash
uv run pytest Tutorial/12-events/src/test_chapter12.py -k none_and_false
```

## Step 5：serial 逐个 await

`serial` 与 `bail` 的判定相同，但会等待每个 listener：

```text
await listener[0] → bail? → 返回
await listener[1] → bail? → 返回
```

测试用一个未 set 的 `Event` 证明第一个 listener 确实让出了控制权：

```python
task = asyncio.create_task(context.serial("chain"))
assert not task.done()   # 第一个 listener 还没拿到 release
release.set()
assert await task == "stop"
```

## Step 6：parallel 等所有 listener，再聚合错误

`parallel` 用 `asyncio.gather(..., return_exceptions=True)` 并发调用全部 listener，只有全部结束后才处理异常：

```python
errors = [result for result in results if isinstance(result, BaseException)]
if errors:
    raise BaseExceptionGroup(f"parallel event {name!r} failed", errors)
```

这对应 Cordis 的 `AggregateError` 语义：失败不取消其他 listener，调用者拿到的是一个包含全部错误的聚合异常。测试让一个 listener 立即失败、另一个等待可控 `Event`，从而确定性地证明 `parallel` 在慢 listener 完成前不会抛出。

```bash
uv run pytest Tutorial/12-events/src/test_chapter12.py -k aggregates_failures
```

## Step 7：waterfall 是 around-middleware

`waterfall` 把 listener 串成洋葱模型，最后一个 callback 是 `next_`：

```python
context.on("flow", outer)
context.on("flow", inner)
await context.waterfall("flow", "value", next_=lambda: "default")
# outer(inner(default))
```

每个 listener 收到 `(*args, next_)`，是否调用 `next_()` 由它决定：不调用就是短路，后续 listener 和最终 callback 都不会执行。这与 `bail` 的区别是，`waterfall` 的返回值属于最外层 listener，而不是“第一个非 `None`”。

### Checkpoint B

五种模式的契约可以并列成一张表：

| 模式 | 同步/异步 | 顺序 | 返回值 |
| --- | --- | --- | --- |
| `emit` | 同步 | 注册顺序 | 无，awaitable 交给事件循环 |
| `bail` | 同步 | 首个 bail 停止 | 首个 bail 值 |
| `serial` | 异步逐个等待 | 注册顺序，首个 bail 停止 | 首个 bail 值 |
| `parallel` | 异步并发等待 | 无完成顺序保证 | 无，错误聚合 |
| `waterfall` | callback 决定 | around-middleware | 最外层返回值 |

```bash
uv run pytest Tutorial/12-events/src/test_chapter12.py
```
