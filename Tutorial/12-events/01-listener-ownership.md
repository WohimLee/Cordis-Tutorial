# 12 — 让 listener 成为 Fiber 拥有的资源

第 10 章把 listener、service、task 和子 Fiber 统一成 Effect，第 11 章又证明了这套 ownership 能熬过异步竞态。但前几章使用监听器时都假设调用者会自己保存 disposer：

```python
dispose = ctx.on("ready", handler)   # 忘记 dispose 会怎样？
```

一旦插件在卸载时忘记调用它，事件系统就成了唯一绕过 Effect 的长期资源。

## 本章目标

这一章从“监听器泄漏”这个失败场景出发，建立与 Cordis `events.ts` 语义一致的事件系统：

- listener 由注册它的 Fiber 拥有，随 Fiber 卸载自动撤销；
- `on`、`once`、`prepend` 和幂等 disposer；
- `emit`、`bail`、`serial`、`parallel`、`waterfall` 五种分发模式；
- dispatch Context、filter 和 global listener；
- `internal/listener` 替换与 `internal/dispatch` 诊断。

本章仍然不引入 Reflect、Logger 和 Loader。为了把注意力集中在事件所有权上，教学实现只保留 Fiber/Effect 中事件系统真正用到的最小部分；它自包含在 [`src/chapter12.py`](src/chapter12.py)，不导入 [`cordis`](../../cordis-py/src/cordis)。

## Step 0：从上一章 checkpoint 出发

第 11 章的 `RaceFiber` 已经能处理依赖竞态，事件系统可以直接挂在它的 EffectScope 上。本章不需要 epoch，所以教学版本用一个更小的 Fiber：

```text
Context.plugin(plugin) → Fiber
    Fiber.effects      EffectScope
    Fiber.wait()       等待激活结束
    Fiber.dispose()    关闭自己的 EffectScope
```

Fiber 创建时会把 `self.dispose` 注册到父 Fiber 的 EffectScope 里，所以 dispose 父 Context 会级联关闭所有子 Fiber。这与第 03 章的父子级联是同一件事，只是这里只保留事件系统需要的一层。

## Step 1：先写会泄漏的测试

朴素实现直接在字典里追加 listener：

```python
def on(self, name, listener):
    self.hooks.setdefault(name, []).append(listener)
    def dispose():
        self.hooks[name].remove(listener)
    return dispose
```

它的问题不是“可能忘记 dispose”，而是根本没有所有权：Plugin 卸载后，只要用户没保存 disposer，listener 就永远留在字典里。本章的第一个测试表达相反契约：

```python
fiber = context.plugin(plugin)   # plugin 里注册了两个 listener
await fiber
context.emit("owned")            # 两个都触发
await fiber.dispose()
context.emit("owned")            # 一个都不触发
```

运行：

```bash
uv run pytest Tutorial/12-events/src/test_chapter12.py -k owned_by_its_fiber
```

## Step 2：注册即安装 Effect

`on()` 不再直接改字典，而是先构造 `Hook`，再把“插入 + 移除”做成一个同步 Effect：

```python
hook = Hook(context, listener, global_)
hooks = self._hooks.setdefault(name, [])

def cleanup():
    try:
        hooks.remove(hook)
    except ValueError:
        return None       # 已经移除过，幂等
    if not hooks:
        self._hooks.pop(name, None)
    return True

def setup():
    hooks.insert(0 if prepend else len(hooks), hook)
    return cleanup

context.fiber.effects.install_sync(setup, f"ctx.on({name!r})")
```

这里有两个必须同时成立的性质：

- `setup()` 在注册时同步执行，所以 `on()` 返回后 listener 立刻可见；
- `cleanup` 由 Fiber 的 EffectScope 拥有，Fiber 卸载时逆序执行，用户是否保存 disposer 都不影响最终清理。

`dispose` 返回的函数只调用 `cleanup()` 并丢弃返回值，因此重复调用始终返回 `None`：撤销是幂等的，调用者不需要先查询状态。

`prepend` 通过 `hooks.insert(0, hook)` 实现，`once` 则在 callback 里先 `dispose()` 再调用真正的 listener：

```python
def callback(*args):
    dispose()
    return listener(*args)
```

“先移除再调用”意味着即使 listener 内部再次 emit 同一事件，也不会重入。

`Fiber.assert_active()` 在注册前检查，保证已经 dispose 的 Fiber 不能再创建新资源，这与正式实现里的 `INACTIVE_EFFECT` 契约一致。

### Checkpoint A

`ctx.on()` 的返回值只是便利的提前撤销入口，不是所有权凭证。真正的所有权在 `context.fiber.effects` 中，因此激活失败回滚时 listener 也会被释放：

```bash
uv run pytest Tutorial/12-events/src/test_chapter12.py -k owned_by_its_fiber
```
