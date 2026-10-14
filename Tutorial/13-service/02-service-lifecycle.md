# 服务生命周期：init、availability、extend

注册是静态事实，可用性是动态事实。一个服务可能存在、但暂时不可用；也可能在不可用期间让 consumer 卸载，恢复后再产生新的 activation epoch。

## Step 4：init cleanup 属于构造它的 Fiber

容器插件（`ctx.plugin(SomeServiceClass)`）的执行顺序是：

```text
构造实例   → 实例内部 provide 服务
调用 init() → 返回 cleanup
install cleanup 到 provider Fiber 的 EffectScope
```

教学实现的 Fiber：

```python
if inspect.isclass(target):
    instance = constructor(self.context, self.config)
    initialize = getattr(instance, "init", None)
    return initialize() if callable(initialize) else None
```

返回的 cleanup 和其他 Effect 一样跟随 provider 卸载，并且只执行一次：

```bash
uv run pytest Tutorial/13-service/src/test_chapter13.py -k init_cleanup
```

`init()` 可以返回 `None`、同步函数或异步函数——`EffectScope` 会按类型等待。

## Step 5：availability 只影响 strict 解析

`ReflectService.implementation()` 是唯一决定“能不能用”的地方：

```python
implementation = self._implementations.get(self.label(context, name))
if implementation is None:
    return None
if strict and implementation.fiber.state is not ACTIVE:
    return None
if strict and implementation.check is not None and not implementation.check():
    return None
return implementation
```

三个条件分别对应：

- 没有 provider；
- provider 还没激活或已经卸载；
- provider 存在但自己声明暂时不可用。

`strict=False` 的宽松查询（例如 `ctx.set` 要找到 owner）会跳过后两个条件。这正是第 05 章“strict 与 loose lookup”的完整含义：availability 是 strict 解析的一部分，而不是注册时的开关。

运行：

```bash
uv run pytest Tutorial/13-service/src/test_chapter13.py -k availability
```

## Step 6：可用性变化触发依赖刷新，而不是直接命令 consumer

`available()` 变成 `False` 后，consumer 不会自动知道。事件来自显式通知：

```python
service.enabled = False
await context.reflect.notify("switchable")
```

`notify` 的过程与第 07 章的 epoch 完全一致：

```text
notify(name) → refresh_scope 找出解析同一 label 的 dependents
             → request_refresh()（合并重复通知）
             → 每个 Fiber 的 refresh runner 重新解析
```

重新解析得到 `None` 时，处于 `ACTIVE`/`FAILED` 的 consumer 进入 `UNLOADING → PENDING`；恢复可用后得到同一个 epoch，但 consumer 处于 `PENDING`，因此执行一次新的激活。测试断言 `activations == [True, True]`：一次初始激活，一次恢复激活。

同一个测试还放了一个 `context.isolate("switchable")` 下的 consumer。`refresh_scope` 只通知 label 相同的 dependents，所以隔离作用域里的 consumer 始终 `PENDING`：

```text
root scope:      provider 存在 → ACTIVE
isolated scope:  没有 provider → PENDING（不受 root provider 变化影响）
```

## Step 7：callable Service 与 extend

callable Service 让“服务本身就是入口”：

```python
class Callable(Service):
    def __call__(self):
        return self.caller_context
```

调用发生在 `ServiceView.__call__` 中，ContextVar 已设置为调用方，所以返回的正是 consumer Context。

`extend()` 用于派生一个共享状态、但不注册的视图：

```python
def extend(self, **properties):
    service = copy.copy(self)
    vars(service).update(properties)
    return service
```

测试验证三件事：派生对象不是原对象、额外属性只存在于派生对象、内部状态仍然是同一个字典。因为 `extend` 不调用 `provide`，Reflect 里仍然只有一个 implementation。

```bash
uv run pytest Tutorial/13-service/src/test_chapter13.py -k extend
```

## Step 8：config 来自多层 intercept 的合并

consumer 可以在 inject 声明中携带配置：

```python
consumer.inject = {"configurable": {"timeout": 3}}
```

Fiber 创建时把非 `None` 的 intercept 追加到自己 Context 的 `intercepts` 中。`Service.resolve_config` 按“外层 → 内层 → head”合并：

```python
for config in (base, *context.intercepts.get(self.name, ()), head):
    ...
merge = getattr(self.Config, "merge", None)
if callable(merge):
    return merge(*configs)
result = {}
for config in configs:
    result.update(config)
```

- 默认是浅合并，后者覆盖前者；
- `Config.merge` 存在时由服务自己决定合并语义；
- 非 Mapping 的 intercept 立即 `TypeError`，不静默忽略。

测试用的 `MergeConfig.merge` 返回配置个数，于是 `base + inject intercept + head` 得到 `{"count": 3}`，与正式实现场景 `018-reflect-service` 的 `config` 结果一致。

```bash
uv run pytest Tutorial/13-service/src/test_chapter13.py -k config_merge
```

### Checkpoint B

可用性、epoch 和 config 三者互不越权：

```text
availability  决定 strict 解析是否成功
epoch        决定是否需要一次新的 activation
config merge 决定这次 activation 看到什么配置
```
