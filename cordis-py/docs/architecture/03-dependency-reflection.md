# 03. 依赖注入与反射

## Inject

```python
inject = ["database", "cache"]

inject = {
    "database": {"transactional": True},
    "cache": None,
}
```

列表只声明依赖；映射值同时作为对应服务的 intercept 配置。函数插件使用静态 `inject` 元数据；`@Inject(...)` 为类和方法写入相同元数据。行为统一由 Registry 和 Fiber 实现。

## 服务实现记录

```python
@dataclass
class Implementation:
    name: str
    label: object
    value: object
    fiber: Fiber
    check: Callable[[], bool] | None
```

解析 key 是 `(service_name, isolation_label)`。严格读取要求 provider Fiber 处于 ACTIVE 且可用性检查通过。

## 依赖变化传播

服务注册、撤销、替换或 availability 变化后：

1. 查找声明该服务的 Fiber；
2. 只通知隔离标签匹配的 Fiber；
3. 更新依赖实现快照；
4. 比较新旧 epoch；
5. 触发加载、卸载或保持不变；
6. 发出 `internal/service`。

建议维护 `service_name -> fibers` 反向索引。通知时使用集合快照，允许回调安全修改 Registry。

## Dependency Epoch

Fiber 根据当前依赖实现的身份计算 epoch。概念上：

```text
llm 由 Fiber 12 提供
tools 由 Fiber 18 提供
epoch = ":12:18"
```

若 llm 被 Fiber 25 的新实现替换，epoch 变为 `:25:18`。Fiber 必须卸载旧激活产生的全部 Effect，再使用新依赖重新运行插件。依赖缺失时 epoch 使用内部 `INACTIVE` 哨兵，使 Fiber 回到 PENDING。

Epoch 比较使用 implementation identity 或稳定 provider generation，不能只比较服务值是否相等。

## 隔离作用域

`ctx.isolate("database")` 为该服务生成新标签。其下 provider 和 consumer 只在该标签内互相可见。两个 Context 使用同一显式 label 时加入同一隔离域。

Root Context 应为每个首次声明的服务生成稳定默认标签，不能用 `None` 同时表达默认域和未声明状态。

隔离与 Intercept 的区别：隔离选择另一个服务实例作用域；Intercept 保留同一服务解析关系，只叠加局部策略配置。

## Intercept 配置

合并优先级：

```text
base < root intercept < ... < nearest intercept < dependency head config
```

服务有专用 merge 方法时调用它；否则只对 Mapping 做浅合并。非 Mapping 配置由服务合并器处理，核心不得猜测。

## ReflectService API

- `get(name, strict=True)`：读取当前作用域实现；
- `set(name, value)`：只允许 provider 所属 Fiber 修改实现；
- `provide(name, value, check=None)`：注册服务 Effect；
- `accessor(name, getter, setter=None)`：声明计算属性；
- `mixin(source, members)`：把服务成员转发到 Context；
- `notify(names, filter=None)`：通知依赖变化；
- `bind(callback)`：为回调保留注册 Context 的追踪信息。

`ctx.foo` 可作为 `ctx.get("foo")` 的语法糖。框架内部优先使用显式方法，避免与 Context 固有成员冲突。

## 写入约束

- 未 provide 的服务不能直接 set；
- 一个 Fiber 不能覆盖另一个 Fiber 的服务；
- 同一隔离域不能有两个活动 provider；
- provider 激活前，其服务不应被 strict consumer 读取；
- provider 卸载时应先通知 consumer，再完成自身依赖快照清除。
