# 08. 异步、并发与错误

## asyncio 模型

Cordis-py 以 asyncio 为异步运行时，同时允许同步插件和同步事件。

- 核心生命周期方法为 `await fiber.wait()`、`await fiber.dispose()` 和 `await ctx.aclose()`；
- 同步封装不能在已有 event loop 内调用 `asyncio.run()`；
- 框架不创建隐式后台线程；
- async plugin 只在当前 loop 建立 task；
- 框架创建的 task 全部归 Fiber 跟踪；
- dispose 取消或等待未完成生命周期任务；
- `CancelledError` 触发清理，但不被普通异常聚合吞掉。

推荐采用 async-first 生命周期，纯同步便利接口只服务没有异步资源的场景。

## 重入与竞态不变量

1. 一个 Fiber 同时最多运行一个 load/unload 操作；
2. 每个 epoch 的 Effect 只由该 epoch 清理；
3. stale load 完成后不能重新发布服务；
4. provider 卸载时 consumer 必须进入卸载流程；
5. 清理器只执行一次，所有调用者可等待同一结果；
6. Registry、Hook 和 Effect 遍历使用快照；
7. 用户回调执行期间不持有全局互斥锁；
8. Fiber dispose 后不能复活；
9. 子 Fiber 生命周期由父 Fiber Effect 所有；
10. await 点前后都必须重新确认 epoch 和 dispose 状态。

单线程 asyncio 仍存在 await 点重入，不能把没有线程误认为没有竞态。

## 稳定错误模型

```python
class CordisError(RuntimeError):
    code: CordisErrorCode
```

建议错误码：

- `INACTIVE_EFFECT`；
- `DUPLICATE_SERVICE`；
- `MISSING_SERVICE`；
- `INVALID_PLUGIN`；
- `INVALID_EFFECT`；
- `CONFIG_VALIDATION_FAILED`；
- `LIFECYCLE_REENTRANCY`。

启动错误保存在 Fiber 上并由 `wait()` 重新抛出。清理阶段必须执行所有 disposer 后再聚合异常。Python 3.11+ 使用 `ExceptionGroup`；兼容更早版本时定义 `CordisAggregateError`。

错误上下文包含 plugin name、fiber uid、effect label、service name 和原始异常链，但不得破坏用户异常 traceback。

## 取消语义

- 外部取消 `wait()` 不应自动 dispose Fiber；
- 取消 `dispose()` 的调用者不能中止已经开始的 owner 清理；
- Fiber 内部任务被 dispose 取消时仍执行 finally 和 disposer；
- 超时只报告未完成，不能把 Fiber 标记成已安全释放；
- Loader shutdown 应等待全部顶层 Fiber 清理或明确报告超时清单。
