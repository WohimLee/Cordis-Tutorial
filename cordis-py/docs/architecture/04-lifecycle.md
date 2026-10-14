# 04. Fiber 与 Effect 生命周期

## Fiber 状态

| 状态 | 含义 |
| --- | --- |
| `PENDING` | Fiber 存在，依赖未全部可用 |
| `LOADING` | 正在验证配置并执行插件入口 |
| `ACTIVE` | 当前依赖快照有效，插件完成激活 |
| `FAILED` | 配置验证或插件启动失败 |
| `UNLOADING` | 正在执行当前 epoch 的清理器 |
| `DISPOSED` | Fiber 永久删除，不能重新激活 |

```text
create
  │
  ▼
PENDING ── dependencies ready ──▶ LOADING ── success ──▶ ACTIVE
   ▲                                  │                    │
   │                                  └── error ──▶ FAILED │
   │                                                       │
   └──────── dependencies lost ◀── UNLOADING ◀─────────────┘

any live state ── dispose ──▶ DISPOSED
```

依赖在加载或卸载中变化时，只更新 epoch；当前过程完成后再决定继续卸载或重新加载，不并发执行第二套生命周期。

## 激活流程

1. 读取全部依赖服务；
2. 检查隔离标签和服务 availability check；
3. 生成依赖快照与 epoch；
4. 解析 intercept 配置；
5. 验证插件配置；
6. 进入 `LOADING`；
7. 调用插件入口；
8. 收集返回的 disposer；
9. epoch 仍有效则进入 `ACTIVE`，否则立即卸载。

## 卸载流程

1. 进入 `UNLOADING`；
2. 阻止创建新 Effect；
3. 固化当前 Effect 集合；
4. 执行清理器；同一 Effect 内逆序，顶层清理可并发；
5. 聚合错误，单个失败不得阻断其他清理；
6. 清除依赖快照；
7. 已 dispose 则进入 `DISPOSED`；
8. 新 epoch 就绪则重新加载，否则进入 `PENDING`。

## 生命周期操作

- `wait()`：等待生命周期稳定并重新抛出启动错误；
- `restart()`：卸载当前 epoch，再用相同原始配置激活；
- `update(config)`：验证配置，经过 `internal/update` waterfall 后重启；
- `dispose()`：永久删除 Fiber，从父 Fiber 和 Runtime 移除；
- disposer 必须幂等，多次调用共享同一完成结果。

## Effect 返回形式

Effect setup 可以返回：

- `None`；
- 同步或异步 disposer；
- disposer 的 iterable/generator；
- disposer 的 async iterable/async generator；
- awaitable，完成后返回上述结果。

无效返回值立即抛出 `TypeError`。

## Effect 不变量

- setup 立即执行；
- 只能在可活动且不处于 `UNLOADING` 的 Fiber 创建；
- disposer 只执行一次；
- 手工 dispose 后，owner 卸载不得重复执行；
- setup 失败时回滚已经产生的 disposer；
- setup 与 owner dispose 竞态时，dispose 等待 setup 并清理其结果；
- 嵌套 Effect 形成包含 label 和 children 的诊断树；
- PENDING Fiber 也可能拥有框架 Effect，最终 dispose 必须清理；
- 不依赖垃圾回收器完成业务资源清理。

## 框架能力也是 Effect

`ctx.provide()` 注册服务并返回注销服务的 disposer；`ctx.on()` 注册 Hook 并返回移除 Hook 的 disposer；`ctx.plugin(child)` 把 Child Fiber 的 dispose 注册到 Parent Fiber。它们不是 Effect 之外的特殊清理路径。

因此 provider 卸载可能产生级联过程：服务撤销，Reflect 通知 consumer，consumer 进入 UNLOADING，撤销自己的服务和监听器，再继续通知下游。
