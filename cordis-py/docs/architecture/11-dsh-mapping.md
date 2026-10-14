# 11. DeepSeek Harness 映射与源码依据

## DSH 中的能力图

DeepSeek Harness 将能力拆为 Cordis 插件和服务：

```text
LLM provider
      │ provides ctx.llm
      ▼
Agent loop ─── requires ctx.tools
      │
      ├── requires ctx.sessions
      ├── emits agent/* events
      └── calls ctx.llm.stream()

Tool plugins ───── register into ctx.tools
Persistence ────── observes session events
Web UI ─────────── drives ctx.agents
```

替换 LLM provider 时，旧服务撤销使依赖者卸载；新服务注册后依赖者重新激活。Agent loop 不需要知道 provider 是 DeepSeek、OpenAI 还是本地模型。

## 同一 Plugin 的多个 Fiber

DSH 的 `@deepseek-ai/dsh-tool-subagent` 展示了多次挂载同一 Plugin 的实际需求。基础 Bundle 用同一实现注册两个模型工具：

```text
tool-subagent PluginRuntime
├── Fiber: spawn route
│   ├── provider = spawn
│   ├── toolName = subagent
│   ├── backgroundMode = continuable
│   └── Effect: register tools["subagent"]
└── Fiber: fork route
    ├── provider = fork
    ├── toolName = subagent_fork
    ├── backgroundMode = one-shot
    └── Effect: register tools["subagent_fork"]
```

`subagent` 创建可继续交互的新子代理，`subagent_fork` 从父代理已有历史分叉。两者共享 `tool-subagent` callback 和 PluginRuntime，但各自拥有配置、Plugin Context、dependency snapshot 和 Effect；卸载其中一个 Fiber 只撤销它注册的工具。Agent Preset 可以独立调整路由策略，例如把 fork 配置为 continuable，而不复制 Plugin 实现。参考 DSH `packages/bundle/base/cordis.patch.yml` 和 `packages/preset/agent-presets/presets/standard/agent.cordis.yml`。

命名的外部子代理 Provider 是另一类多实例需求。DSH 的组合测试会多次挂载 `@deepseek-ai/dsh-subagent-codex` 或 `@deepseek-ai/dsh-subagent-claude-code`，为同一种 Provider 实现分配不同名称和模型：

```text
subagent-codex PluginRuntime
├── Fiber
│   ├── providerName = codex-primary
│   └── model = codex-primary-model
└── Fiber
    ├── providerName = codex-secondary
    └── model = codex-secondary-model

subagent-claude-code PluginRuntime
├── Fiber: claude-primary
└── Fiber: claude-secondary
```

独立的 `tool-subagent` Fiber 再把这些 Provider 暴露成 `subagent_codex_primary`、`subagent_codex_secondary`、`subagent_claude_primary` 和 `subagent_claude_secondary`。这种组合让应用复用 Provider 和工具实现，同时按名称选择不同模型、外部进程或策略。参考 DSH `packages/subagent/subagent-codex/tests/fixtures/loader/codex.patch.yml`、`packages/subagent/subagent-claude-code/tests/fixtures/loader/claude-code.patch.yml` 和 `snapshots/session/product-subagent-both/cordis.yml`。

配置中多次出现同一模块不保证运行时存在多个 Fiber。DSH 的 minimal profile 为 `@deepseek-ai/dsh-terminal-bash` 写入 Bash 和 PowerShell 两个条目，但用互斥的 `disabled` 表达式按平台选择：

```text
非 Windows  ──▶ Bash 条目 active，PowerShell 条目 disabled
Windows     ──▶ Bash 条目 disabled，PowerShell 条目 active
```

因此需要区分三个数量：

```text
配置条目数       Loader 读到的声明数量
存活 Fiber 数    当前实际挂载且尚未 dispose 的实例数量
PluginRuntime 数 Registry 按 Plugin callback 建立的共享记录数量
```

再次执行 `ctx.plugin(same_plugin, config)` 创建新 Fiber；`restart()`、`update()` 或 Inject 丢失后恢复只让原 Fiber 进入新的 activation epoch。多个 Fiber 若在同一 isolation scope 注册同名 Service 或同名 Tool，仍会触发对应 Registry 的重复注册规则；可共存的配置必须使用不同名称或不同 isolation scope。

## Python 复现层级

```text
Cordis-py Core
  Context / Registry / Reflect / Fiber / Effect / Events
                │
                ▼
Configuration Runtime
  Loader / Include / Group / HMR
                │
                ▼
Harness Capability Plugins
  llm / tools / sessions / agent-loop / shell / fs / web ...
                │
                ▼
Application Profiles
  headless / CLI / API / ACP / Web
```

第一阶段只复现 Core。Loader 完成不等于 Harness 已复现；Harness 还需要逐项实现其服务定义、provider 和 consumer。

## 源码阅读顺序

原始实现位于 DeepSeek Harness 的 `vendor/cordis/src`。建议顺序：

1. `context.ts`：根 Context、Proxy、extend、isolate 和 intercept；
2. `registry.ts`：插件形态、Inject 和 `ctx.plugin()`；
3. `fiber.ts`：`_refresh()`、epoch、reload、unload 和 effect；
4. `reflect.ts`：服务解析、provide 和 notify；
5. `events.ts`：五种分发模式和 listener ownership；
6. `service.ts`：Service 对 provide、check 和 intercept 的封装；
7. `logger.ts`：Logger 层级、消息和 Exporter；
8. Loader 源码：配置 Entry 如何变成插件树。

## 对照原则

Python 实现应按行为场景对照，而不是逐行翻译：

- Context 动态访问是否解析到同一作用域实现；
- 同一加载顺序是否产生相同 Fiber 状态序列；
- provider 替换是否导致相同 unload/reload；
- Effect 是否在相同所有权边界被清理；
- dispatch 是否产生相同监听顺序和结果；
- update、failure 和 disposal 是否达到相同最终状态。

JavaScript Proxy、Symbol、prototype 和 thenable 等机制不要求按实现方式复制；它们承载的公开 API 和可观察能力仍属于 Core 对等目标，无法直接表达的部分必须登记 Python 等价形式并通过行为对照。

## 架构事实来源

| 架构主题 | 原始源码 |
| --- | --- |
| Context 派生与隔离 | `context.ts` |
| PluginSpec 与 Runtime | `registry.ts` |
| Fiber 状态、epoch、Effect | `fiber.ts` |
| Service implementation 与 notify | `reflect.ts` |
| 分发模式与 Hook | `events.ts` |
| Service 便利封装 | `service.ts` |
| 日志模型 | `logger.ts` |

实现期间如果文档与当前 vendored 源码冲突，以 vendored 源码的可观察行为为准，并同步修正文档及跨语言测试。
