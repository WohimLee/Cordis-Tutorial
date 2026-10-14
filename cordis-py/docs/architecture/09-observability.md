# 09. 日志与可观测性

## LoggerService

LoggerService 提供命名 logger 和层级继承：

```python
log = ctx.logger("loader")
log.info("loaded %s", name)
```

日志消息包含时间、级别、logger 名称、参数、异常和可选 Fiber 信息。关闭级别时不得提前格式化消息。

Exporter 是生命周期注册项，必须通过 Effect 安装和撤销。Console、JSON、文件或远程日志均是独立 Exporter，不写死在核心 Logger 中。

`cordis` 只保留上游 Cordis 定义的 Logger、Message、格式化和 Exporter 生命周期契约。用于本项目开发者观察调用链的内存采集器不属于 Cordis API，放在独立的 `cordis_observer` 包中：

```python
from cordis_observer import MemoryExporter

exporter = MemoryExporter()
ctx.exporter(exporter)
```

这种拆分避免把本地学习和诊断用途误认为上游 Cordis 能力，也不复制日志分发或格式化机制。

## 生命周期诊断

核心提供只读诊断：

- `registry.runtimes()`；
- `runtime.fibers`；
- `fiber.state`、`fiber.error` 和依赖快照；
- `fiber.getEffects()` 的 label/children 树；
- `reflect.implementations()`；
- 生命周期、服务变化和事件 dispatch trace。

诊断读取不得触发 getter、插件加载或其他业务副作用。敏感配置只展示字段名和遮蔽值。

## 状态事件

每次 Fiber 状态变化发出包含新旧状态的事件。只有 ACTIVE 与非 ACTIVE 之间的变化才触发服务可用性传播，普通 LOADING 到 UNLOADING 的中间状态不得制造重复通知。

## 指标建议

- 各状态 Fiber 数量；
- plugin load/unload 时长；
- 配置验证失败次数；
- Effect 清理失败和超时；
- event listener 数与 dispatch 时长；
- service dependency wait 时长；
- HMR 成功、回滚和失败次数。

指标接口作为可选插件，不让核心绑定 OpenTelemetry。

## 性能目标

- 服务读取接近字典查找复杂度；
- 依赖通知通过反向索引只扫描相关 Fiber；
- 事件只复制单个名称的 Hook 列表；
- Context 派生使用持久化映射或小型 copy-on-write 数据；
- 生产环境默认关闭昂贵的调用栈捕获；
- 正确性优先于微优化，尤其不能牺牲生命周期原子性。
