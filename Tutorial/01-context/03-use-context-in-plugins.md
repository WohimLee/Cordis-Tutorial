# 让 Plugin 使用 Context

我们已经能创建 Context。最后一步是把它传给 Plugin。

## Step 5：修改 Plugin 类型

第 00 章的 Plugin 接收字典：

```python
Plugin = Callable[[dict[str, object]], object]
```

现在改为接收 Context：

```python
Plugin = Callable[[Context], object]
```

`Context.plugin()` 将自己交给 `_Runtime.mount()`：

```python
def plugin(self, callback: Plugin) -> None:
    self._runtime.mount(self, callback)
```

内部 Runtime 再执行 Plugin：

```python
def mount(self, context: Context, plugin: Plugin) -> None:
    result = plugin(context)
    if callable(result):
        self.cleanups.append(result)
```

因此从哪个 Context 调用 `plugin()`，Plugin 就会收到哪个 Context。

运行：

```bash
uv run pytest Tutorial/01-context/src/test_chapter01.py -k plugin_receives
```

## Step 6：确认 child 仍连接同一个 Runtime

本章暂时通过 `ctx.services` 暴露上一章的共享字典：

```python
@property
def services(self) -> dict[str, object]:
    return self._runtime.services
```

Provider 可以从一个 Context 写入 Service，Consumer 可以从另一个 Context 读取：

```python
provider_ctx.plugin(provider)
consumer_ctx.plugin(consumer)
```

这证明 child Context 不是一套新的 Runtime。

但 `services` 只是本章为了延续示例保留的临时接口。正式 Cordis 不会让 Plugin 直接操作一个裸 Service 字典。后续会由 Reflect 记录 Service implementation，并由 Context 提供 `get()`、`provide()` 和动态属性访问。

## Step 7：关闭 root Context

为了保留第 00 章的能力，本章提供同步 `close()`：

```python
def close(self) -> None:
    if self is not self.root:
        raise RuntimeError("only the root context can close the runtime")
    self._runtime.close()
```

只有 root 可以关闭整个 Runtime。否则，一个 child Context 可能意外清理其他 Plugin 的所有资源。

这个限制并不能真正解决资源所有权，只是避免最明显的误用。正式实现会让每个资源归 Fiber 所有，并提供异步、完整等待的关闭过程。

运行本章全部测试：

```bash
uv run pytest Tutorial/01-context/src/test_chapter01.py -q
```

### Checkpoint C

本章完成后的执行关系是：

```text
root = Context()
child = root.extend({"name": "demo"})
child.plugin(plugin)
          │
          ▼
共享 _Runtime 执行 plugin(child)
```

我们已经解决：

- Plugin 不再接收无含义的裸字典；
- 每次调用都带有明确 Context；
- Context 可以形成 root/child 作用域树；
- metadata 可以继承和覆盖；
- 所有 Context 仍属于同一个 Runtime。

仍未解决：

- Cleanup 仍在全局列表，没有 Fiber owner；
- Provider 和 Consumer 仍然依赖挂载顺序；
- Service 仍然只是共享字典里的值；
- child Context 没有独立的生命周期；
- 同步 `close()` 不能等待异步 Cleanup。

## 与正式源码对应

正式实现见 [`src/cordis/context.py`](../../cordis-py/src/cordis/context.py)。本章只复现其中最基础的作用域行为：Context identity、root、`extend(meta)` 和从 Context 调用 Plugin。

正式实现还会把 `events`、`logger`、`reflect` 和 `registry` 绑定到每个 Context，并让派生 Context 保留正确的调用方信息。这些内容会在对应章节中出现。

## 与 DSH Cordis 对照

DSH Cordis 的 `Context.extend(meta)` 通过 JavaScript prototype 创建派生对象。Python 没有相同的 prototype 模型，因此本章用显式对象和 metadata 合并表达相同的可观察结果：

- child 与 root 属于同一 Runtime；
- child 继承 parent metadata；
- child 覆盖不会修改 parent；
- Plugin 接收调用 `plugin()` 的 Context。

## 本章不解决什么

下一章实现 **Effect**。我们会把一个普通 Cleanup 变成有 setup、有状态、可调用、可等待并且只执行一次的可逆资源。

Context 如何真正拥有 Effect，要等到最小 Fiber 出现后再连接起来。

## 检查理解

1. 为什么 `extend()` 不能直接调用普通的 `Context()`？
2. child 为什么要共享 `_runtime`，却不能共享同一个 `_meta` 字典？
3. 为什么只允许 root 调用当前的全局 `close()`？
4. 如果一个 child 被删除，它创建的 Cleanup 应该由谁保存？

第 4 个问题会把我们带向 Fiber 和 Effect 的资源所有权模型。
