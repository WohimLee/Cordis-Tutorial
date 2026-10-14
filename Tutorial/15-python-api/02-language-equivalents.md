# 语言等价表达

每个 JavaScript 机制都要回答一个问题：它是**可移植的运行时契约**，还是**语言特有的声明方式**？前者必须有 Python 等价物，后者只需保留可观察能力。

## Step 3：`global` 与 `global_`

`EventOptions` 的键保持 Cordis 原样：

```python
EventOptions = TypedDict("EventOptions", {"prepend": bool, "global": bool}, total=False)
```

只有**存储字段**改名，因为 `global` 是 Python 关键字，不能作为属性声明：

```python
@dataclass(frozen=True, slots=True)
class Hook:
    ctx: Context
    callback: Listener
    global_: bool = False
```

因此注册时读 `options["global"]`，Hook 上写 `hook.global_`。行为完全一致：`global_=True` 的 Hook 绕过 dispatch Context 的 filter。

```bash
uv run pytest Tutorial/15-python-api/src/test_chapter15.py -k global_key
```

## Step 4：thenable disposer → 可等待且可调用的 Effect

TypeScript 的 disposer 既是 Promise 又可以调用。Python 用两个协议同时表达：

```python
class Effect:
    def __await__(self):
        return self._wait().__await__()   # 等待 setup 完成

    def __call__(self):
        if self._dispose_task is None:
            self._dispose_task = asyncio.create_task(self._dispose())
        return self._dispose_task         # 调用即开始销毁
```

测试同时验证两件事：`inspect.isawaitable(effect)` 为真；`effect()` 返回 Task，重复调用返回同一个 Task，cleanup 只执行一次。

```bash
uv run pytest Tutorial/15-python-api/src/test_chapter15.py -k awaitable_and_callable
```

## Step 5：Proxy → 显式视图 + ContextVar

Python 没有 `Proxy`，也没有 prototype。第 13 章的 `ServiceView` 用 `__getattr__` 转发，并在调用边界绑定调用方：

```python
def __getattr__(self, name):
    value = getattr(self._target, name)
    if not callable(value):
        return value
    def bound(*args, **kwargs):
        token = _current_context.set(self._context)
        try:
            return value(*args, **kwargs)
        finally:
            _current_context.reset(token)
    return bound
```

可观察契约是：读取转发、方法调用期间的 caller Context 正确、`target` 本身没有被修改（`not isinstance(target, ServiceProxy)`）。

```bash
uv run pytest Tutorial/15-python-api/src/test_chapter15.py -k service_proxy
```

## Step 6：Symbol → 不透明 sentinel

`Context.filter` 在 TypeScript 里是 Symbol key。Python 用模块级 sentinel：

```python
class Context:
    filter = object()
```

它只依赖身份，不依赖字符串或枚举值：

- `Context.filter is Context.filter` 永远成立；
- 每个派生 Context 的 metadata 独立存储，互不干扰；
- 没有 key 时 `metadata()` 返回 `None`。

把 sentinel 当成普通 key 使用，就不会出现“魔法字符串”冲突。

```bash
uv run pytest Tutorial/15-python-api/src/test_chapter15.py -k sentinel
```

## Step 7：prototype inheritance → 显式父 Context

`Context.derive` 复制父作用域的数据，而不是链接原型：

```python
context = object.__new__(cls)
context.__dict__ = parent.__dict__.copy()
context._metadata = dict(parent._metadata)
```

于是：

- 子 Context 修改 metadata 不影响父 Context；
- 字符串元数据用 `setattr` 只写在子 Context 上；
- `child.root is root`，共享同一个根作用域与事件总线。

不存在原型链，也不存在“修改原型影响所有实例”的隐藏行为。

```bash
uv run pytest Tutorial/15-python-api/src/test_chapter15.py -k inherits_metadata
```

## Step 8：AggregateError → BaseExceptionGroup

`parallel` 分发必须等待全部 listener，再聚合失败：

```python
results = await asyncio.gather(*(invoke(hook.callback) for hook in hooks), return_exceptions=True)
errors = [result for result in results if isinstance(result, BaseException)]
if errors:
    raise BaseExceptionGroup(f"parallel event {name!r} failed", errors)
```

`BaseExceptionGroup` 是 Python 原生聚合异常，可以包含任意 `BaseException`，也可以嵌套。它与 JavaScript `AggregateError` 的可观察语义一致：失败不取消其他 listener，调用者拿到全部错误。

```bash
uv run pytest Tutorial/15-python-api/src/test_chapter15.py -k aggregate_error
```

### Checkpoint B

五个机制里有三个只需要换拼写（`global_`、`is_context`、`wait`），两个需要换实现策略（Proxy、Symbol），一个只需要换异常类型（AggregateError）。它们都不改变生命周期语义。
