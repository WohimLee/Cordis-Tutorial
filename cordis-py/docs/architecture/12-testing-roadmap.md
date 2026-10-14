# 12. 测试与实施路线

## 单元测试矩阵

- Context 派生不修改父 Context；
- 隔离标签解析；
- 服务重复注册和所有权检查；
- Inject 列表、映射和继承合并；
- 五种事件模式、prepend、once、global 和过滤；
- Effect 同步、异步、generator、失败回滚和幂等；
- Fiber 全部状态转换；
- 配置验证和 intercept 合并；
- Logger 层级和 Exporter 清理。

## 生命周期竞态测试

- async setup 未完成时 owner dispose；
- LOADING 时依赖消失；
- UNLOADING 时依赖恢复；
- disposer 内注册或删除其他资源；
- provider 和 consumer 同时关闭；
- listener 在 dispatch 中删除自身或新增 listener；
- 子插件创建时父插件同步销毁；
- 多个 disposer 同时失败。

测试使用可控 Future/Event 驱动时序，不使用脆弱的 `sleep()`。

## 跨语言行为测试

从 vendored Cordis 提取语言无关场景，由 TypeScript reference runner 和 Python runner 执行相同输入，再比较规范化输出：

- 插件激活顺序；
- 依赖消失与恢复；
- Effect 清理顺序；
- waterfall 嵌套结果；
- 隔离服务可见性；
- update/restart 行为；
- 失败后的最终状态。

除这些生命周期主干外，对照必须覆盖 Context、Registry、Effect、Events、Reflect、Service 和 Logger 的每个公开可移植契约。不能仅在 Python 测试中手写预期并将其称为跨语言兼容。

不比较对象布局，只比较公开结果、返回契约、异常、状态和事件序列。每个未对照项必须在兼容矩阵中保持未完成状态。

## 实施阶段

### 阶段 A：语义核心

实现 Context、Service、Registry、Fiber、Inject、Effect 和事件。完成依赖消失/恢复、同步/异步清理及隔离测试。

### 阶段 B：完整核心

补齐 Reflect accessor/mixin、配置验证、update/restart、Logger、内部事件、Effect 诊断和错误聚合。

### 阶段 C：配置运行时

实现 YAML/TOML Loader、include、overlay、插件解析、插件组和配置热更新。

### 阶段 D：Harness 兼容层

选择 DeepSeek Harness 的真实插件组合，以 Python 重写对应服务接口，并建立跨语言行为测试和示例应用。

### 阶段 E：Cordis Core 对等收敛

以 vendored `@deepseek-ai/cordis` 4.0.2 为固定参考，建立完整 API/行为矩阵和 TypeScript oracle，补齐公开契约，登记不可避免的 Python 差异，并删除早期实现中被取代的命名和抽象。

## 架构验收标准

1. 插件加载顺序不影响声明依赖后的最终结果；
2. provider 消失时 consumer 自动卸载，恢复时重新激活；
3. 所有注册项都有明确 owner 和幂等 disposer；
4. 同步与异步清理都能完整等待；
5. 隔离作用域中同名服务互不干扰；
6. waterfall 必须调用 `next()` 才继续；
7. 竞态下不会重复激活、泄漏 Effect 或从 DISPOSED 复活；
8. 配置验证失败不留下部分服务和监听器；
9. Loader 不替代核心依赖调度；
10. 关键行为具有由 TypeScript Cordis 实际执行的对照场景测试；
11. 每个 Cordis Core 公开项都有 exact、equivalent、language-specific、missing 或 extension 分类；
12. 不存在未解释的 API/能力差异或重复兼容实现。

达到阶段 A 是“小而可用的 Python Cordis”；阶段 B 表示功能完整的早期核心，不再表示与上游完全对等；只有阶段 E 的矩阵和双语言验收全部通过后，才能声明 Cordis Core 等价。阶段 C 和 D 不属于 Cordis Core 对等范围。
