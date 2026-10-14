# Accessor、Mixin 与三层职责边界

服务解决“对象如何被解析”，但有些 Context 属性不是服务：它们由现有状态计算出来，或者只是把已有对象的成员暴露到 Context 上。本章最后补上 accessor 和 mixin，并划清 Service、ServiceView 与 Reflect 的边界。

## Step 9：accessor 是声明出来的计算属性

```python
plugin_context.accessor(
    "computed",
    {"get": lambda receiver: state["value"], "set": set_value},
)
```

`AccessorProperty` 保留了 Cordis 的字段名：

```python
class AccessorProperty(TypedDict):
    type: NotRequired[Literal["accessor"]]
    get: Callable[[Context], object]
    set: NotRequired[Callable[[Context, object], object]]
```

Python 没有 JavaScript 的 `this`，所以 getter/setter 显式接收 receiver Context，这是兼容矩阵记录的等价表达。

`Context.get` 的第一分支就是 accessor：

```python
accessor = self.reflect.accessor_record(name)
if accessor is not None:
    return accessor.getter(self)
```

因此 `ctx.computed` 不需要注册服务，也不进入 `implementations()`。写入通过 `ctx.set` 路由：

```python
if accessor.setter is None:
    raise AttributeError(f"Context accessor {name!r} is read-only")
return accessor.setter(context, value) is not False
```

只读 accessor 抛 `AttributeError`，而不是静默失败；setter 返回 `False` 时 `set` 返回 `False`，让调用者可以看到“写入被拒绝”。

accessor 是 Effect：provider 卸载后 `accessor_record` 消失，`ctx.get("computed")` 回到 `None`。测试同时覆盖读取、写入、只读失败和卸载清理。

```bash
uv run pytest Tutorial/13-service/src/test_chapter13.py -k accessor
```

## Step 10：mixin 把已有成员接到 Context

`mixin(source, members)` 为每个成员生成一个 accessor：

```python
def getter(receiver, key=source_name):
    target = receiver.get(source) if isinstance(source, str) else source
    return getattr(target, key)
```

- `source` 是字符串时按服务名解析，于是 mixin 能转发服务成员；
- `source` 是对象时直接转发，不要求它是 Service；
- `members` 是序列时源名与目标名相同，是 Mapping 时可以改名。

冲突在安装时统一检查：

```python
if len(set(names)) != len(names) or any(
    name in self._accessors or name in self._labels for name in names
):
    raise TypeError("mixin target property is already declared")
```

重复成员名、与已有 accessor 冲突、与已有服务名冲突都会 `TypeError`。mixin 产生的所有 accessor 由同一个 Effect 拥有，卸载时一起删除。

```bash
uv run pytest Tutorial/13-service/src/test_chapter13.py -k mixin
```

### Checkpoint C

accessor 与 mixin 不新增第二套解析机制：它们只是往同一张 `_accessors` 表里登记记录，读取仍走 `Context.get`，写入仍走 `Reflect.set`。

## 与正式源码对应

- Service、ServiceView、caller Context、extend、config resolution：[`src/cordis/service.py`](../../cordis-py/src/cordis/service.py)
- Impl、label、provide/get/set、accessor、mixin、notify：[`src/cordis/reflect.py`](../../cordis-py/src/cordis/reflect.py)
- `ctx.inject/plugin/provide/accessor/mixin/get/set`：[`src/cordis/context.py`](../../cordis-py/src/cordis/context.py)
- provider Fiber 的 Effect 所有权与 activation epoch：[`src/cordis/fiber.py`](../../cordis-py/src/cordis/fiber.py)
- 行为测试：[`tests/test_reflect.py`](../../cordis-py/tests/test_reflect.py)
- 跨语言场景：[`018-reflect-service.json`](../../cordis-py/tests/compat/scenarios/018-reflect-service.json)、[`019-accessor-mixin.json`](../../cordis-py/tests/compat/scenarios/019-accessor-mixin.json)、[`023-service-facades.json`](../../cordis-py/tests/compat/scenarios/023-service-facades.json)

## 与 DSH Cordis 对照

分类引用 [`docs/cordis-core-compatibility.md`](../../cordis-py/docs/cordis-core-compatibility.md) 的 Reflect and Service 一节。

| 契约 | 状态 | Python 表达 |
| --- | --- | --- |
| `Service.name` 与注册 | Exact | 名称推导和生命周期所有权一致 |
| `Property.Service` / `Property.Accessor` | Equivalent | TypedDict 保留字段；callback 显式接收 receiver |
| `Impl` | Equivalent | 多存一个 label，因为 Python 没有 Proxy 作用域映射 |
| Reflect `get`/`set`/`provide`/`notify` | Equivalent | strict 解析、所有权、返回值与按 label 过滤的通知一致 |
| `accessor` / `mixin` | Equivalent | 决定、方法转发和卸载清理一致；无 Symbol/prototype |
| Service initialization hook | Equivalent | `init()` 在构造后运行，cleanup 归 Fiber 所有 |
| availability check | Equivalent | 可覆盖的 `available()` 取代 JavaScript 的 symbol 方法 |
| callable Service | Equivalent | 用 `__call__` 表达 |
| Service extension | Equivalent | `extend(**properties)` 产生未注册的派生实例 |
| caller Context tracking | Equivalent | `ServiceView` 在方法调用和 `__call__` 上设置 ContextVar |
| service config 与 custom merge | Equivalent | 默认浅合并，`Config.merge` 覆盖 |
| tracker metadata | Language-specific | 保留关联与调用追踪，但不复制 Symbol identity |

## 本章刻意简化

- 没有 RegistryService。Fiber 的 uid 由根 Context 上的计数器分配；正式实现由 Registry 统一分配，并在 dispose 时删除 identity 记录。
- 没有 `reflect.bind()`（注册 Context 绑定）和 `internal/get`、`internal/set` waterfall。这些属于事件章节与正式实现的诊断面。
- `ReflectServiceView.notify` 只接受单个 name；正式实现接受多个名字和可选 filter 谓词。
- 插件声明的 `provide` / `intercept` 元数据、config validator 和 `@Inject` 装饰器没有重复实现，它们分别属于第 04、06、09 章。
- Fiber 的 refresh runner 只处理“依赖是否齐全”和 epoch 变化；`restart()`、`update()`、`no_save` 属于第 09 章。
- `ServiceView` 不做调用栈改写，只做 ContextVar 绑定；正式实现同样不制造 JavaScript 栈字符串。
- `Effect` 只保留同步安装；异步 setup 的完整形态见第 10 章。

## 检查理解

1. 为什么服务必须作为 Effect 注册，而不是在字典里直接赋值？
2. `Service`、`ServiceView` 和 `ReflectService` 各自拥有什么？为什么 `Service` 不是服务的 owner？
3. `strict=False` 的解析为什么必须跳过 availability 检查？
4. 服务从可用变为不可用时，为什么 consumer 是卸载到 `PENDING`，而不是保持 `ACTIVE` 并报错？
5. `Service.extend()` 为什么不注册新服务？它在什么场景下有用？
6. `Context.get` 为什么先查 accessor，再查本 Fiber 的注入 store，最后才查 Reflect？
7. `mixin` 的冲突检测为什么必须在安装时完成，而不能等到第一次读取？
