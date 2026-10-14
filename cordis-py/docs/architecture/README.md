# Cordis-py 架构设计

本目录定义 Cordis-py 的目标架构。它是实现前的设计基线，不表示所有模块已经完成。

复现目标不是逐行翻译 TypeScript，而是保持 Cordis 的运行时语义：插件化扩展、作用域服务、依赖驱动激活、Fiber 生命周期、可逆 Effect，以及一致的同步和异步清理。

## 文档导航

1. [总体设计](01-overview.md)：核心定位、范围、运行时不变量、结构和代码布局；
2. [核心对象模型](02-core-model.md)：Context、Service、Plugin、PluginRuntime 和 Fiber；
3. [依赖注入与反射](03-dependency-reflection.md)：Inject、Epoch、服务解析、隔离和 Intercept；
4. [Fiber 与 Effect 生命周期](04-lifecycle.md)：状态机、激活、卸载、重启和资源所有权；
5. [完整运行流程](05-runtime-flow.md)：挂载、服务晚到、Provider 替换、销毁和失败时序；
6. [事件系统](06-events.md)：Hook、五种分发模式、Waterfall、过滤和内部事件；
7. [配置与 Loader](07-configuration-loader.md)：配置验证、插件解析、include、overlay 和 HMR；
8. [异步、并发与错误](08-async-errors.md)：asyncio、重入不变量、取消、错误聚合和错误码；
9. [日志与可观测性](09-observability.md)：Logger、Exporter、生命周期诊断和性能目标；
10. [公共 API](10-public-api.md)：Python API 草案、类型策略和兼容边界；
11. [DeepSeek Harness 映射](11-dsh-mapping.md)：DSH 能力图、复现层级、源码阅读和对照原则；
12. [测试与实施路线](12-testing-roadmap.md)：测试矩阵、阶段划分和架构验收标准。

## 阅读顺序

文件名前缀就是推荐阅读和实现顺序。首次阅读建议依次阅读 01–06；实现具体模块时，再阅读相应专题和第 12 章。

## 术语

- **Context**：插件访问运行时能力的作用域容器；
- **Service**：注册到稳定 Context 名称的能力对象；
- **Plugin**：接收 Context 和配置、产生 Effect 的扩展单元；
- **Fiber**：一次插件挂载及其生命周期实例；
- **Effect**：setup 和一个或多个 disposer 组成的可逆操作；
- **Epoch**：一次有效的依赖快照和插件激活周期；
- **Runtime**：同一插件 callback 的共享注册记录；
- **Implementation**：特定隔离作用域内的服务实现记录。
