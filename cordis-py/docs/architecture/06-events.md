# 06. 事件系统

## Hook 记录

每条监听保存注册 Context、callback、`prepend`、`global_` 以及所属 Fiber/Effect。`on()` 和 `once()` 返回 disposer，并自动挂到当前 Fiber。

事件分发前复制当前事件的 Hook 列表，因此 callback 可以安全注册或删除监听器，而不影响本次分发快照。

## 分发模式

| 模式 | 同步/异步 | 顺序 | 返回值 |
| --- | --- | --- | --- |
| `emit` | 同步 | 注册顺序 | 无 |
| `bail` | 同步 | 首个 bail 停止 | 首个 bail 值 |
| `serial` | 异步逐个等待 | 注册顺序，首个 bail 停止 | 首个 bail 值 |
| `parallel` | 异步并发等待 | 无完成顺序保证 | 无，错误聚合 |
| `waterfall` | callback 决定 | around-middleware | 最外层返回值 |

bail 值与 Cordis 保持一致：只有 `None` 和 `False` 继续，因此 `0` 和空字符串也会停止。

推荐用途：`emit` 广播已发生事实，`parallel` 等待互不依赖的异步观察者，`serial`/`bail` 实现有优先级的决策链，`waterfall` 实现请求拦截、权限策略和前后处理。

## Waterfall

```python
def middleware(request, next):
    request.tags.append("before")
    result = next()
    request.tags.append("after")
    return result
```

调用 `next()` 才进入下一个监听器或最终实现。不调用就是短路。同步和异步 waterfall 应分别提供明确入口，或统一要求调用者 await；不能返回不可预测的同步/异步混合类型。

## Context 过滤

带分发 Context 的事件可以提供过滤谓词。普通 Hook 仅在注册 Context 通过过滤时触发；`global_=True` 忽略过滤。隔离服务通知通过此机制只触达同一作用域。

## 内部事件

- `internal/plugin`：Fiber 创建或销毁；
- `internal/status`：Fiber 状态变化；
- `internal/config`：激活前配置 waterfall；
- `internal/service`：服务实现变化；
- `internal/update`：配置更新 waterfall；
- `internal/get`、`internal/set`：反射访问拦截；
- `internal/listener`：监听器注册拦截；
- `internal/dispatch`：公开事件分发诊断。

内部事件是高级扩展点，应标记稳定性等级。核心内部事件的异常策略必须逐项定义：观察型事件不能破坏 owner 清理，拦截型事件则允许明确 veto。

## 异常策略

- `emit` 的同步异常默认向调用者传播；生命周期通知可使用安全分发逐个记录错误；
- `bail` 和 `serial` 遇到 callback 异常立即失败；
- `parallel` 等待所有 callback 后聚合异常；
- `waterfall` 保留完整异常链；
- 同步 `emit` 遇到 async listener 应报错或记录明确警告，不能丢弃 coroutine。
