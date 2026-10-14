# 框架资源也只是 Effect

## Step 6：Service registration

`provide(name, value)` 的 setup 把 Service 写入 store，cleanup 把同一个 registration 删除：

```python
def setup():
    store[name] = value
    return lambda: store.pop(name, None)
```

它不需要 Fiber dispose 中的“额外删除 Service”分支。

## Step 7：Event listener

`on(event, listener)` 的 setup 加入 listener，cleanup 移除 listener。Plugin epoch 结束时，不会留下已经失效的 callback。

## Step 8：Child Fiber 与 Task

Child Fiber 的 cleanup 是 `child.dispose`；background task 的 cleanup 会 cancel 并等待 task 结束。二者都可以是 async cleanup，因此仍走同一个 `_collect()` 和 dispose 流程。

运行完整资源场景：

```bash
uv run pytest Tutorial/10-unified-effects/src/test_chapter10.py -k framework
```

## 与正式源码对应

- Effect、EffectScope 与 metadata：[`src/cordis/effect.py`](../../cordis-py/src/cordis/effect.py)
- Context 的资源入口：[`src/cordis/context.py`](../../cordis-py/src/cordis/context.py)
- Service registration：[`src/cordis/reflect.py`](../../cordis-py/src/cordis/reflect.py)
- Listener ownership：[`src/cordis/events.py`](../../cordis-py/src/cordis/events.py)
- Child Fiber ownership：[`src/cordis/fiber.py`](../../cordis-py/src/cordis/fiber.py)

## 与 DSH Cordis 对照

本章保留 setup、嵌套 Effect、callable disposer、awaitable setup 和统一资源 ownership 的能力。Python 用 async iterable、`BaseExceptionGroup` 和 `ContextVar` 表达对应语言能力。

正式 Cordis-py 的 `Effect` 同时 callable 和 awaitable：调用它开始 dispose，await 它等待 setup。教学代码保留相同形状。

## 本章到下一章的连接

统一 ownership 后，才能回答竞态中的关键问题：异步 setup 还没返回 cleanup，owner 就 dispose 了怎么办？

答案不能是“忽略尚未完成的 setup”。第 11 章会让 dispose 等待 setup，并立即清理它稍后产生的资源。

## 检查理解

1. 为什么 Service 和 listener 不需要独立 cleanup registry？
2. 为什么 cleanup 使用逆序？
3. setup 产生一半资源后失败，已经产生的资源怎么办？
4. metadata tree 为什么不是第二套生命周期？
5. 为什么 task cleanup 必须等待 task 真正结束？
