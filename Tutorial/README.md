# Step by Step：从零构建 Cordis Python Runtime

本教程通过一步步实现一个可运行的 Python Runtime，彻底理解 Cordis 的核心机制，并最终复现 DeepSeek Harness vendored Cordis 运行栈的可移植概念、生命周期语义、公共接口和功能能力。目标范围不仅包括 `@deepseek-ai/cordis` 4.0.2 Core，还包括 Loader、Include、HMR 和 Timer 插件，并以一个最小 Harness 验证整套组件可以协同运行、稳定更新和完整退出。

教程不是正式源码的逐行注释，也不是只展示 Cordis-py 的使用方法。我们会从一个只能挂载函数插件的最小 Runtime 出发，不断加入资源清理、服务反射、响应式依赖、生命周期状态机、事件系统和并发保护。每一步先面对一个真实问题，再把实现演化到 [`src/cordis`](../cordis-py/src/cordis) 当前采用的设计。

## 当前进度

本仓库正在重组：原 root 的 Python 项目已整体移动到 [`cordis-py/`](../cordis-py)，教程正文中的“正式实现”链接均指向该目录。

- 00–21 章已编写：每章包含若干编号 Markdown 正文，以及自包含的 `src/chapterNN.py` 和 `src/test_chapterNN.py`；第 00–20 章的代码只依赖标准库和 pytest，第 21 章额外使用项目已锁定的 PyYAML（仅 `safe_load`）；所有章节都不跨章 import、不 import `cordis`。章节级 `README.md` 占位文件已全部删除；
- 16 章建立跨语言证据管线（场景 schema、归一化、可注入 runner、expected check、双侧比较与 diff、版本门禁），并把真实状态写成台账：Python oracle 的 `004`–`024` 为单边 21/21 通过，pinned TypeScript 4.0.2 build 未运行，`001`–`003` 是 host 形状、不参与 Core 判定；
- 17 章实现最小 Loader：`ParsedEntry`/`Entry`、format-neutral parser、映射式 `ModuleResolver`、mount/update/replace 事务与 `ConfigReloader`，并说明为什么依赖顺序不属于 Loader；
- 18 章以 `ConfigComposer` 实现 mapping adapter 边界、相对路径递归 include、循环检测、来源归属与按 entry id 的 overlay，且组合失败不扰动运行中的树；
- 19 章把 Timer 写成**教程定义的候选契约**（`cordis-py` 目前没有 Timer 资产）：`ManualClock` 可控时钟、`timeout`/`interval`/可取消 `sleep`/`throttle`/`debounce`，句柄全部归调用方 Fiber 的 Effect，并明确 callback 失败、取消与重启/新 epoch 的语义；
- 20 章把 host-driven HMR 写成**教学候选契约**（正式实现只有 `ConfigReloader.start()/reload()`，没有 watcher、没有模块缓存刷新）：宿主喂入 `ChangeEvent`、Timer debounce 合并 burst、配置变化优先 config update、结构或模块变化走 candidate replacement，候选失败时保留或恢复 previous generation；
- 21 章是最小 Harness 整栈毕业项目：自包含 Context/Effect/Fiber、YAML `include` 配置组合、Loader 挂载整棵树（不手工排序）、Timer 驱动 heartbeat、host-driven HMR 替换 provider，并验证请求结果、短暂 `PENDING`、旧资源只清理一次、故障回滚与恢复、根 Context 关闭后所有计数器归零；
- 教程章节测试需要 Python 3.11+ 和 pytest；第 00–21 章的 `src/test_chapterNN.py` 已**逐章**运行（Python 3.12.13，`PYTHONDONTWRITEBYTECODE=1`，`pytest -p no:cacheprovider`，每章从自己的 `src/` 目录运行）：共 184 项教学测试通过，其中 00–15 为 126 项、16–18 为 30 项、19 为 10 项、20 为 10 项、21 为 8 项。第 16–18 章的 6 个 Python 文件，以及第 19–21 章的全部 12 个 Python 文件（含 `src/plugins/*.py` 与 `src/run.py`），另外通过 `ruff check --target-version py311 --no-cache` 与 `ruff format --check --no-cache`（**在仓库根目录**对文件列表运行，与 16–18 章已记录的口径一致）。提示：ruff 的 isort 分组会按 cwd 推断 project root，因此在章的 `src/` 目录内单独运行时，16–21 章的测试文件都会报 I001（`chapterNN` 被当作 first-party）；这是仓库既有的口径差异，不是代码缺陷。
- 教程测试是**独立的教学测试**，不计入正式实现的验证结论。[`cordis-py/`](../cordis-py) 的验证事实仍以原有记录为准（2026-10-14：正式 Python suite 76 项测试通过，lint/format/类型检查/打包通过，场景 004–024 的 Python oracle 全部通过；pinned TypeScript reference oracle **未验证**；Timer 与 HMR 是教学候选契约，第 19–21 章不更新其 Phase 0–5 结论）。

## 教程目标

完成教程后，读者应能够独立回答以下问题：

- Context 为什么不是普通的依赖字典？
- Plugin、PluginRuntime 和 Fiber 分别代表什么？
- 为什么 consumer 可以先于 provider 挂载？
- 服务消失后，为什么 consumer 会卸载，而不是直接失败？
- 服务恢复后，Cordis 如何开始一个新的 activation epoch？
- Effect 如何统一管理监听器、服务、子 Fiber、任务和用户资源？
- 为什么 dispose 必须幂等、完整等待，并在部分清理失败后继续执行？
- 如何避免旧 epoch 的异步 setup 在依赖变化后重新发布过期结果？
- Context isolation、intercept 和 caller Context 如何协作？
- 五种事件分发模式的同步、异步和 bail 语义有什么区别？
- Cordis 的哪些 TypeScript 接口可以直接复现，哪些必须采用 Python 等价表达？

最终目标不是写出“看起来像 Cordis”的代码，而是构建一个在可观察行为上与参考 Cordis 对等的 Python Runtime，并证明配置装载、文件组合、热更新和定时任务确实建立在同一套 Fiber/Effect 生命周期之上。

## 兼容边界

教程首先复现 DSH 仓库 `vendor/cordis/src` 中的 Cordis Core：

- Context；
- Registry 与 Inject；
- Fiber 与 Effect；
- Reflect 与 Service；
- Events；
- Logger；
- 具有可移植运行时能力的公共 utilities。

在 Core 完成后，继续复现 DSH vendored 的四个官方组件：

| 组件 | DSH 参考版本 | 教程目标 |
| --- | --- | --- |
| Loader | `@deepseek-ai/cordis-plugin-loader` 1.0.0-rc.5 | 将声明式 Entry 树解析、挂载、更新和回滚到 Core 生命周期。 |
| Include | `@deepseek-ai/cordis-plugin-include` 1.0.4 | 组合文件配置、处理相对路径和 patch，并保留来源信息。 |
| Timer | `@deepseek-ai/cordis-plugin-timer` 1.1.2 | 提供随 Fiber 自动释放的 timeout、interval、sleep、throttle 和 debounce。 |
| HMR | `@deepseek-ai/cordis-plugin-hmr` 1.0.15 | 监视配置和模块变化，经过 Loader 事务安全替换运行中的插件。 |

第 17–21 章按这张表推进，但**教程完成不等于组件等价**：正式 `cordis-py` 只有 Loader、include 展开与 host-driven `ConfigReloader`，Timer 目前**零资产**，HMR 也没有 watcher 与模块缓存刷新。因此第 19、20 章以及第 21 章的对应部分是教程定义的**候选契约（candidate contract）**，用来固定"若进入正式实现应当满足哪些可观察行为"，不是 DSH Timer 1.1.2 或 HMR 1.0.15 的等价证据。

DeepSeek Harness 的完整 LLM、Agent、Tools、Session 等业务实现仍不属于复现范围。教程最后只构建一个小型、无网络、无密钥、无真实 LLM 的 Harness，用来验证 Cordis 运行栈，而不是复制 DSH 产品功能。

Core 已有逐项对等证据；Loader、Include、HMR 和 Timer 必须分别完成新的 API inventory、语言差异表和双语言行为场景后，才能宣称对应组件等价。当前 Python Loader、include 展开和 host-driven reload 只能作为实现基础，不能直接视为四个 DSH 插件已经完整复现；第 19–21 章也不更新 `cordis-py` 的 Phase 0–5 验证结论。

对照版本、逐项接口分类和语言差异见 [`docs/cordis-core-compatibility.md`](../cordis-py/docs/cordis-core-compatibility.md)。

## 教学方法

### 1. 从问题推动实现

每章先实现当前问题所需的最小代码。只有当测试或新需求暴露缺陷时，才引入下一层机制。例如，我们不会一开始就写完整 Fiber 状态机，而是先展示“直接执行插件”为什么无法正确处理依赖丢失和异步清理。

### 2. 教学实现可以演进，正式实现保持唯一

每个阶段保留独立、可运行的教学代码，使读者能看到设计如何变化。阶段之间允许少量代码重复，但这些重复只存在于 `Tutorial`。正式 Runtime 始终以 [`src/cordis`](../cordis-py/src/cordis) 为唯一实现，不为了迁就教程增加兼容层或重复机制。

### 3. 先建立语义，再对照最终源码

章节不会要求读者先理解最终实现。每章完成后才链接到正式源码，解释教学版本中哪些结构被保留、哪些因并发、类型或公共 API 要求而被强化。

### 4. 测试是教程的一部分

每一步都通过测试表达行为契约，而不只是验证示例可以运行。生命周期和竞态测试使用可控制的 `asyncio.Event` 或 `Future`，不依赖时间 sleep。

### 5. 同时对照 TypeScript 和 Python

涉及兼容性的章节会指出：

- DSH Cordis 的原始概念和可观察行为；
- Cordis-py 的 Python 表达；
- 两者是否完全一致、能力等价或属于语言特有机制；
- 对应的双语言场景或测试证据。

## 核心教学主线

整个教程围绕下面的闭环逐步展开：

```text
Context 定义作用域并提供插件 API
    ↓
Registry 规范化 Plugin，创建并记录 Fiber
    ↓
Reflect 按服务名称与 isolation label 解析 Inject
    ↓
Fiber 根据依赖快照决定是否激活
    ↓
Effect 拥有本次激活产生的所有长期资源
    ↓
服务变化产生新的 dependency epoch
    ↓
旧 epoch 完整卸载后，新 epoch 才能激活
```

Context 是入口，Fiber 是生命周期主体，Effect 是资源所有权机制，Reflect 是服务解析层，Registry 是插件身份与挂载记录，Events 和 Logger 则建立在相同的 Context/Fiber 所有权之上。

## 章节规划

### 第一阶段：最小生命周期闭环

#### 00 — 为什么需要 Cordis

- 从普通函数调用和依赖字典开始；
- 展示插件顺序、资源泄漏和动态替换问题；
- 定义 Context、Plugin、Fiber、Effect、Service 和 Epoch 的术语；
- 明确整个教程要维持的生命周期不变量。

#### 01 — 最小 Context

- 构建根 Context；
- 实现 `extend()` 和父子元数据继承；
- 区分 root、parent 和派生作用域；
- 暂时不实现动态服务访问。

#### 02 — Effect 与可逆资源

- 从普通 disposer 开始；
- 统一同步和异步 setup；
- 接受单个、嵌套和 iterable cleanup；
- 实现逆序、幂等且完整等待的清理；
- 解释 callable + awaitable Effect 的 Python 表达。

#### 03 — 最小 Fiber

- 使用 Fiber 表示一次插件挂载；
- 引入最小生命周期状态；
- 收集插件执行期间创建的 Effect；
- 实现父子 Fiber 级联销毁；
- 让 `await fiber` 等待稳定状态。

阶段成果：可以挂载函数插件、注册资源并安全销毁，但还没有响应式依赖。

### 第二阶段：响应式依赖系统

#### 04 — Plugin 与 Registry

- 规范化函数、类和 `{ apply }` 风格插件；
- 区分 Plugin、PluginRuntime 和 Fiber；
- 实现插件 identity、`get()`、`has()`、`delete()` 和遍历；
- 支持同一 PluginRuntime 的多个 Fiber。

#### 05 — Reflect 与服务实现

- 按名称注册和查询服务；
- 将服务实现绑定到提供者 Fiber；
- 区分 strict 和 loose lookup；
- 服务 disposer 如何触发依赖通知；
- 为什么 Reflect 不是第二个依赖调度器。

#### 06 — Inject 与延迟激活

- 函数插件的静态 `.inject` 元数据；
- 类和方法的 `@Inject`；
- consumer 先注册、provider 后到达；
- 依赖不完整时保持 PENDING；
- 依赖完整时进入第一次 activation。

#### 07 — Dependency Epoch

- 用服务 implementation identity 构造依赖快照；
- 服务丢失触发 consumer 卸载；
- 服务恢复触发新的 epoch；
- 防止同一 Fiber 同时运行两个 activation；
- 区分暂时 PENDING 和永久 DISPOSED。

#### 08 — Isolation 与 Intercept

- 服务名称之外为什么还需要 isolation label；
- `Context.isolate()` 的继承和共享标签；
- Inject mapping 中的 intercept 配置；
- 默认浅合并和 Service 自定义配置合并。

阶段成果：得到 Cordis 最核心的“依赖驱动生命周期”，插件挂载顺序不再决定激活顺序。

### 第三阶段：完整生命周期与并发正确性

#### 09 — Fiber 状态机

- PENDING、LOADING、ACTIVE、UNLOADING、FAILED、DISPOSED；
- 合法状态转换和稳定状态；
- `restart()`、`update()` 和永久 `dispose()`；
- 配置验证的时机和失败状态；
- 生命周期通知。

#### 10 — 所有资源统一为 Effect

- Context 注册监听器、服务和用户资源；
- 子 Fiber、Service 初始化结果和插件返回值；
- 一个 activation epoch 对应一棵 Effect 树；
- Effect metadata 如何服务于诊断而不成为第二套机制。

#### 11 — 异步竞态

- setup 期间 dispose；
- LOADING 时服务丢失；
- UNLOADING 时服务恢复；
- parent dispose 与 child creation 交错；
- cleanup 部分失败后的最终状态；
- 为什么用户 callback 不能在全局锁中执行。

阶段成果：Runtime 不只在正常路径工作，也能在依赖变化、异步 setup 和失败清理中保持生命周期不变量。

### 第四阶段：完整 Cordis Core 能力

#### 12 — Events

- listener 的 Effect 所有权；
- `emit`、`bail`、`serial`、`parallel` 和 `waterfall`；
- prepend、once 和 disposer；
- dispatch Context、filter 和 global listener；
- `internal/listener` 与 `internal/dispatch`。

#### 13 — Service 抽象

- Service 注册与 `provide`；
- init cleanup；
- caller Context tracking；
- callable Service 和 `extend()`；
- availability check；
- accessor 与 mixin。

#### 14 — Logger

- Logger、LoggerService、Message 和 Exporter；
- level 继承、格式化和缓冲；
- Logger 与 Fiber 的关联；
- 为什么学习调用链的 observer 位于 `cordis_observer`，而不是 Core Logger。

#### 15 — 公共 API 与 Python 语言差异

- 对齐 package-root exports；
- `Fiber.await()` 与 `await fiber`/`fiber.wait()`；
- `Context.is()` 与 `Context.is_context()`；
- `global` 与 `global_`；
- Proxy、Symbol、prototype、thenable 和 AggregateError 的 Python 等价表达；
- 删除教学过程中的临时 API 和重复抽象。

#### 16 — 与 DSH Cordis Core 行为对照

- 运行共享 JSON 场景；
- 对比 TypeScript 与 Python 的规范化结果；
- 检查正常路径、失败路径和竞态路径；
- 阅读完整兼容矩阵；
- 从教学 Runtime 过渡到正式 [`src/cordis`](../cordis-py/src/cordis)。

阶段成果：完成一个概念、语义、接口和能力上与 DSH Cordis Core 对等的 Python 实现，并能解释每一个重要设计选择的来源。

### 第五阶段：教程内的候选契约整栈闭环

这一阶段的目标是在教程内把运行栈闭环跑通，**不代表正式 `cordis-py` 已经实现 Timer 和 HMR**：正式实现只有 Loader、include 展开和 host-driven `ConfigReloader`，第 19–21 章的 Timer/HMR 是教学候选契约。

#### 17 — Loader：从配置到插件树

- 对照当前 Python Loader 的真实契约：`ParsedEntry`、`Entry`、`ConfigParser`/`TomlConfigParser`、`ModuleResolver`、`Loader`、`ConfigReloader`；`EntryGroup`、`EntryTree`、`ModuleLoader` 在正式实现中不存在，不作为对照目标；
- 把声明式配置解析为稳定 identity 的 Entry 树；
- 所有插件只通过 `Context.plugin()` 挂载，不建立第二套依赖排序；
- 区分 config-only update 和 structural replacement；
- 更新失败时回滚到上一棵有效运行树；
- 对照当前 [`src/cordis/loader`](../cordis-py/src/cordis/loader) 并补齐差异。

#### 18 — Include：组合配置来源

- 通过 mapping adapter 展开 TOML/JSON（YAML 由项目已锁定的 PyYAML adapter 负责），`ConfigComposer` 只接收 mapping；
- 正确解析嵌套 include 的相对路径；
- 保留每个 Entry 的源文件归属和错误位置；
- 支持按 entry id 的 overlay/patch，并明确不虚构 index patch 或 DSH Include 完整等价（`ConfigComposer` 是 Python 扩展，兼容矩阵排除 Include）；
- 检测 include cycle，失败时不扰动当前 Runtime；
- 使用跨文件场景验证组合顺序和更新传播。

#### 19 — Timer：生命周期安全的时间能力

- 实现 timeout、interval 和可取消 sleep；
- 实现 throttle 与 debounce；
- Timer 创建的 task 必须属于调用方 Fiber 的 Effect；
- Fiber unload/dispose 后保证旧 callback 不再执行；
- callback 失败、取消与重启具有明确契约；
- 测试使用可控时钟或确定性同步点，避免易抖动的真实时间 sleep。

#### 20 — HMR：安全替换运行中的插件

- 复现 DSH HMR 与 Loader、Include、Timer 的依赖关系；
- 监视配置文件和 Python 模块变化；
- 使用 Timer debounce 合并短时间内的重复变更；
- 配置变化优先走 Fiber update，结构或模块变化走 replacement；
- 候选模块导入、激活或清理失败时保留或恢复上一版本；
- 验证旧 epoch 的服务、监听器、task 和模块引用全部撤销。

#### 21 — 最小 Harness：整栈毕业项目

- 创建一个稳定的请求入口和可替换 provider；
- 使用 Include 组合基础设施、provider 和 consumer 配置；
- 由 Loader 挂载整棵插件树，不手工排序；
- consumer 通过 Inject 等待 provider；
- Timer 驱动周期性心跳或后台任务；
- HMR 修改 provider 实现或配置，并验证请求不中断或按预期短暂进入 PENDING；
- 注入无效更新，验证旧版本继续工作或事务回滚；
- 恢复合法更新，验证新 epoch 激活且旧资源只清理一次；
- 关闭根 Context，验证 Registry、服务、监听器、Timer task、watcher 和 Effect 全部归零。

阶段成果：证明 Core、Loader、Include、Timer 和 HMR 不是孤立功能，而是一套能够启动、运行、更新、恢复和关闭的完整 Cordis Python 运行栈。

## 目录约定

计划中的目录结构如下（00–21 现已全部编写完成）：

```text
Tutorial/
├── README.md
├── 00-why-cordis/
│   ├── 01-start-from-functions.md
│   ├── 02-build-naive-runtime.md
│   ├── 03-discover-lifecycle-problems.md
│   ├── 04-connect-to-cordis.md
│   └── src/
│       ├── chapter00.py
│       └── test_chapter00.py
├── ...
├── 16-cordis-core-compatibility/
├── 17-loader/
├── 18-include/
├── 19-timer/
├── 20-hmr/
└── 21-mini-harness/
    ├── 01-boot-the-stack.md
    ├── 02-update-pending-and-recovery.md
    ├── 03-shutdown-and-boundaries.md
    ├── config/
    │   ├── cordis.yaml
    │   ├── infra.yaml
    │   ├── consumer.yaml
    │   └── provider.yaml
    └── src/
        ├── plugins/
        ├── chapter21.py
        ├── run.py
        └── test_chapter21.py
```

所有章节都已完成，目录里不再保留章节级 `README.md` 占位文件；正文按阅读顺序拆成编号 Markdown 小节，Python 代码统一放入该章的 `src/`。

正式章节包含若干编号 Markdown 和一个 `src/`。Markdown 的数量不预设：简单章节可以只有一个文件，复杂章节可以有十个以上。只有当主题、实践目标或 checkpoint 能独立成立时才拆文件；两个连续步骤必须放在一起才能理解时就合并。

`src/` 保存截至本章可以独立运行的教学实现、行为测试和必要的示例。章节入口是编号最小的 Markdown，文件名直接说明内容，不使用章节级 `README.md`。

教学文件只依赖 Python 标准库和本项目已经使用的测试工具。除最终对照章节外，教学 Runtime 不直接导入 `src/cordis`，否则读者只能学习如何调用最终实现，而无法观察它如何建立。

## 每章应覆盖的内容

为了让教程真正可以 step by step 跟做，每章整体应覆盖以下内容，但不要求每项各占一个标题或文件。已经在相邻内容中说清楚的信息不重复：

1. **本章目标**：完成后能够运行什么；
2. **当前问题**：上一章的实现在哪里失效；
3. **先写测试**：用行为描述目标，而不是绑定内部结构；
4. **最小实现**：只添加解决当前问题所需的机制；
5. **运行示例**：展示外部可观察结果；
6. **为什么这样设计**：解释简单方案为何不足；
7. **正式源码对照**：链接到 `src/cordis` 的对应实现；
8. **DSH Cordis 对照**：说明 TypeScript 概念、Python 表达和兼容状态；
9. **本章不解决什么**：明确下一步，而不是提前塞入完整系统；
10. **检查理解**：提供可以自行推演或修改的练习。

### 章节内部也必须 from scratch

“Step by Step”不仅指章节之间有顺序，也约束每一章内部的写法。每章必须从上一章已经存在的代码，或本章明确展示的空白最小文件开始，逐步得到本章结果。不能先给出完整实现，再按模块解释它。

章节从上一章 checkpoint 或明确的空白起点开始，按照当前问题所需的实际步数推进。下面只是典型过程，不是必须凑齐的八步模板：

```text
Step 0  运行上一章，确认起点
Step 1  写一个会失败的新行为测试
Step 2  增加最小数据结构或接口
Step 3  实现最小成功路径
Step 4  运行测试并观察新的边界问题
Step 5  加入失败、清理或并发路径
Step N  重构并运行本章全部测试
最后    对照正式源码和 DSH Cordis
```

步骤可以增加、减少或合并。如果一章无法小步讲清楚，说明章节范围过大，应拆章；如果几个步骤没有独立信息，就应合并，而不是为了形式保留。

一个 Step 应在读者需要时说明下面的信息，而不是为了齐全重复同一句话：

1. 此时要解决的唯一问题；
2. 相对上一步修改了哪些代码；
3. 应该运行什么命令；
4. 预期看到什么结果，以及该结果为什么引出下一步。

代码展示优先使用短小的增量片段或 diff，并在关键 checkpoint 给出截至当前可以完整运行的文件。读者必须能够停在任意 checkpoint，运行代码并得到文档描述的结果。

### Checkpoint 也不固定数量

Checkpoint 只放在值得停下来运行和总结的位置，例如：

- 一组新能力已经形成可运行闭环；
- 一个失败测试明确暴露了下一层问题；
- 复杂状态转换完成，可以独立验证；
- 本章终点，需要列出已有能力和尚未解决的限制。

简单章节可能只需要终点 Checkpoint；复杂章节可以有多个。没有新的可验证状态，就不增加 Checkpoint。

后续章节只能依赖上一章终点已经公开并测试过的能力，不能依赖尚未讲解的最终 `src/cordis` 内部机制。

## 测试策略

教程测试分成四层：

- **最小行为测试**：证明本章新机制有效；
- **生命周期测试**：验证资源所有权、状态和最终清理；
- **确定性竞态测试**：使用 Event/Future 控制执行顺序；
- **跨语言场景**：最终与 DSH Cordis 对比规范化输出。

关键失败测试必须同时验证异常和最终资源状态。例如，cleanup 抛出异常时，不只检查异常类型，还要确认其他 disposer 已执行、Fiber 已进入终态、Registry 已移除记录。

正式项目已有的 Core 对照 runner 位于 [`tests/compat`](../cordis-py/tests/compat)，可以在后半程作为参考和验收工具。扩展组件不能只依赖 Core 场景，需要为 Loader、Include、Timer 和 HMR 增加各自的参考 runner 或可规范化行为场景。

## 与正式源码的关系

教程中的简化不是另一套 Cordis 设计。每个阶段都应明确指出与正式源码的关系：

| 教学概念 | 正式实现 |
| --- | --- |
| Context 与派生作用域 | [`src/cordis/context.py`](../cordis-py/src/cordis/context.py) |
| Effect 与资源收集 | [`src/cordis/effect.py`](../cordis-py/src/cordis/effect.py) |
| Fiber 状态机与 epoch | [`src/cordis/fiber.py`](../cordis-py/src/cordis/fiber.py) |
| PluginRuntime 与 Registry | [`src/cordis/registry.py`](../cordis-py/src/cordis/registry.py) |
| Inject 元数据 | [`src/cordis/model.py`](../cordis-py/src/cordis/model.py) |
| 服务解析、隔离和 accessor | [`src/cordis/reflect.py`](../cordis-py/src/cordis/reflect.py) |
| Service 调用方绑定 | [`src/cordis/service.py`](../cordis-py/src/cordis/service.py) |
| 五种事件分发模式 | [`src/cordis/events.py`](../cordis-py/src/cordis/events.py) |
| Logger 与 Exporter | [`src/cordis/logger.py`](../cordis-py/src/cordis/logger.py) |
| 公共错误与配置验证 | [`src/cordis/errors.py`](../cordis-py/src/cordis/errors.py)、[`src/cordis/config.py`](../cordis-py/src/cordis/config.py) |
| Loader、Include 与配置事务 | [`src/cordis/loader`](../cordis-py/src/cordis/loader) |
| Timer | 正式实现中**无对应资产**；第 19 章是教程定义的候选契约，见 [`19-timer/`](19-timer/) |
| HMR 文件监视与模块替换 | 正式实现只有 host-driven [`loader/reloader.py`](../cordis-py/src/cordis/loader/reloader.py)；第 20–21 章的 watcher/debounce/候选替换是教学候选契约，未进入正式实现 |

正式源码是最终答案，但教程负责展示这个答案如何从约束和失败场景中一步步推导出来。

## 编写原则

- 先列出本章不可缺少的知识点和它们的前置关系，再决定文件、标题和 Step；
- 文件数量、篇幅、Step 和 Checkpoint 数量全部由知识结构决定，不设统一模板数字；
- 同一个定义只在第一次需要时完整解释，后文通过链接或一句提醒引用；
- 不因追求简短删除失败路径、边界条件、设计理由和验证证据；
- 每章只引入少量新概念，确保代码仍可完整理解；
- 每章内部从零或从上一章 checkpoint 小步构建，禁止直接展示最终答案后倒序讲解；
- 不把后续章节的复杂度提前泄漏到当前教学实现；
- 不用伪代码代替关键生命周期逻辑；
- 不通过 sleep 模拟并发正确性；
- 不为了代码短而隐藏所有权、状态转换或错误传播；
- 不逐行翻译 TypeScript，只复现可观察语义；
- 公共命名尽可能沿用 Cordis；
- Python 差异必须明确说明，而不是悄悄改变契约；
- 每章结束时所有示例和测试都应可运行；
- 最终章节必须能够解释兼容矩阵中的每一类结论。

## 推荐学习方式

读者可以按以下方式完成每章：

1. 只阅读“当前问题”和测试，先自行设计；
2. 在空白或上一章代码上实现目标；
3. 运行测试并观察失败；
4. 对照本章 `src/chapterNN.py`；
5. 阅读正式源码中更完整的实现；
6. 最后查看 DSH Cordis 对照和语言差异。

不要只复制代码。教程真正要建立的是一套判断标准：任何新资源由谁拥有、什么时候发布、依赖变化时哪个 epoch 有效、失败后系统必须处于什么状态。

## 实施顺序

教程将按以下节奏推进：

1. 先完成 00–03，验证 Context → Effect → Fiber 的最小闭环和章节模板；
2. 再完成 04–08，把响应式依赖作为教程核心重点；
3. 完成 09–11，用确定性竞态测试强化生命周期正确性；
4. 完成 12–15，补齐 Cordis Core 的公共能力；
5. 完成 16，对照 Core 的全部兼容证据并回归正式源码；
6. 完成 17–20：Loader 与 include 对照当前 Python 扩展实现并验收；Timer 与 HMR 按教学候选契约实现并验收；
7. 最后完成 21，通过最小 Harness 执行整栈稳定性验收。

00–21 已全部完成。第 19–21 章补齐的是教程侧的候选契约与整栈证明；把 Timer 或 HMR 提升为正式能力仍需要各自的 API inventory、语言差异表和双语言行为场景。

第一阶段完成后应复审章节颗粒度。如果单章引入太多状态或超过一个核心问题，应拆分章节，而不是压缩解释。

## 完成标准

教程完成不以“所有目录都存在”为标准，而需要满足：

- 每一章都能从上一章的问题自然推导出新增机制；
- 每一章内部都有可执行的增量步骤、失败证据和至少三个 checkpoint；
- 每一章的教学源码和测试可以独立运行；
- 核心生命周期没有依赖未解释的魔法行为；
- 关键并发场景具有确定性测试；
- 所有正式 Core 模块都能从教程章节追溯其设计来源；
- 所有 Python 语言差异都有理由和能力对应；
- 最终教学 Runtime 的行为能够通过选定的 DSH Cordis Core 对照场景；
- Loader 与 include 有现有 Python 实现作为对照锚点；Timer 与 HMR 有来源固定的教学候选契约和可运行的章节行为证据；把任一扩展组件升级为“已等价复现”，仍需未来补齐 API inventory、语言差异表和双语言行为场景；
- 最小 Harness 能从多文件配置冷启动并处理真实的依赖无序挂载；
- provider 配置或代码更新后，consumer 按依赖 epoch 正确卸载和恢复；
- 无效配置、导入失败和激活失败不会破坏上一棵有效运行树；
- 连续 HMR 和 Timer 活动期间不产生重复 listener、遗留 task 或重复 cleanup；
- Harness 完整关闭后，Registry、Reflect implementations、Events hooks、Timer tasks、watchers 和根 Effect 全部清空；
- 整栈测试能够重复运行，并包含启动、稳态、更新、故障、恢复和关闭六个阶段；
- 读者完成教程后，可以不依赖现有实现重新设计一个语义正确、可配置、可热更新的 Cordis Python Runtime。

### 当前达成情况

- 00–21 章的正文、`src/chapterNN.py` 与 `src/test_chapterNN.py` 全部存在，并逐章通过各自的教学测试；
- 第 21 章的整栈测试覆盖启动、稳态、更新、故障、恢复、关闭六个阶段，并在关闭后断言所有计数器归零；
- Timer 与 HMR 的达成标准是“教学候选契约 + 可运行的章节行为证据”，而不是组件等价：正式实现中 Timer 零资产，HMR 只有 host-driven `ConfigReloader`；第 19–21 章提供的是候选契约与教学整栈证明，要升级为“已等价复现”仍需未来的双语言证据；
- `cordis-py` 的正式结论不因教程更新而改变：Python suite 76 passed，pinned TypeScript reference oracle 仍未验证，Phase 0–5 的验证记录保持原样。
