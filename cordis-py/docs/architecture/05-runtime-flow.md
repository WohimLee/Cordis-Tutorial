# 05. 完整运行流程

本章把 Registry、Context、Reflect、Fiber 和 Effect 串成端到端时序。各对象的静态职责见前四章。

## 示例插件

```python
def agent_loop(ctx, config):
    loop = AgentLoop(ctx.llm, ctx.tools)
    ctx.provide("agent_loop", loop)
    ctx.on("agent/run", loop.run)
    return loop.close


agent_loop.inject = ["llm", "tools"]
```

## 挂载与激活

调用 `fiber = ctx.plugin(agent_loop, config)` 后：

1. Registry 识别插件入口并生成 PluginSpec；
2. 创建或复用 callback 对应的 PluginRuntime；
3. 创建 Fiber，保存 parent、Inject 和 raw config；
4. 创建带该 Fiber 的插件子 Context；
5. 将 Child Fiber 的 dispose 注册为 Parent Fiber 的 Effect；
6. 发布 `internal/plugin`，允许 Loader 关联 Entry 或补充元数据；
7. Reflect 查找 llm 和 tools 的当前作用域实现；
8. Fiber 根据 implementation identity 计算 dependency epoch；
9. 依赖不全则保持 PENDING，`plugin()` 返回但不执行入口；
10. 依赖满足则从 PENDING 进入 LOADING；
11. 解析 intercept，验证配置；
12. 调用插件入口；
13. `provide`、`on` 和返回的 `loop.close` 被收集为 Effect；
14. 再次验证 epoch，未过期则进入 ACTIVE；
15. `agent_loop` 服务变为严格可见，并通知其下游 consumer。

`await fiber.wait()` 等待流程进入稳定状态。PENDING 是正常稳定状态，不代表错误。

## 服务晚到

consumer 可以先于 provider 挂载：

```text
mount consumer
  └── missing llm → PENDING

mount llm provider
  └── provide("llm")
      └── Reflect.notify("llm")
          └── recompute consumer epoch
              └── LOADING → ACTIVE
```

所以 Inject 是响应式依赖声明，不是一次性的启动排序提示。

## Provider 替换

```text
old llm removed
  └── consumer epoch → INACTIVE
      └── ACTIVE → UNLOADING
          └── remove consumer effects
              └── PENDING

new llm provided
  └── consumer epoch = new provider identity
      └── PENDING → LOADING → ACTIVE
```

如果新 provider 在 consumer 尚未卸载完时出现，只记录新 epoch。旧清理完成后再激活，不能同时运行两个 consumer epoch。

## 最终销毁

调用 `await fiber.dispose()`：

1. 标记永久 dispose，禁止新 Effect；
2. 发布安全的插件销毁通知；
3. 从 PluginRuntime 和 Parent Fiber 移除；
4. epoch 设为 INACTIVE；
5. 进入 UNLOADING；
6. 固化并执行全部 Effect disposer；
7. 注销服务触发依赖图下游级联卸载；
8. 移除监听器、关闭业务资源并销毁子插件；
9. 等待所有同步和异步清理结束；
10. 聚合清理错误并进入 DISPOSED。

最终 dispose 与依赖暂时消失不同：前者不可复活，后者在依赖恢复后重新激活。

## 失败路径

- 配置验证失败：进入 FAILED，不执行插件入口；
- 插件入口中途失败：回滚本次 setup 已产生的 Effect，再进入 FAILED；
- epoch 在 LOADING 中过期：启动结果不能发布，立即卸载；
- 单个 disposer 失败：继续其他清理，最后聚合异常；
- Loader 更新失败：保留上一有效 Entry/Fiber，记录候选配置错误；
- Parent 在 Child 创建期间销毁：Child 必须被 Parent 已发布的 Effect 捕获并清理。

## 调用链总结

```text
Context 提供作用域视图
  → Registry 把 Plugin 变成 Fiber
    → Reflect 解析服务并传播变化
      → Fiber 根据 epoch 加载或卸载
        → Effect 绑定和回收所有资源
          → Events 让插件在不依赖具体实现时协作
```
