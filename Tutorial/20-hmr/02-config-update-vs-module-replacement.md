# 20 — 配置更新与模块替换：两条路径、一个事务边界

上一节结束时输入侧已经闭环：宿主喂事件，一次 burst 变成一次可分类的批处理。这一节处理"批处理要做什么"。

两类变化的代价差异是整节的组织原则：

```text
config 变化：  一个 URL 改了。期望：同一个 Fiber，同一个 generation，只换 epoch。
module 变化：  provider 换成 v2。期望：新代码生效，旧代码的资源全部释放。
```

混在一条路径上只有两种坏结果：改一个 URL 却重建整棵树（不必要的 stop/start），或者换了模块却什么都没发生。

## 本章目标（承接上一节）

- 定义"一代"的三个对象：`ParsedEntry`（声明）、`GenerationEntry`（运行态）、`HotReloadTarget`（跨代标签）；
- 让 config-only 变化原地换 epoch，不新建 generation，也不新建 Fiber；
- 让 module/结构变化走 candidate replacement：先在旧树完全未被触碰时完成解析，再换代；
- 用 `MappingModuleSource` 把"模块版本"变成可以显式注入的输入；
- 明确**分类规则**与**预检规则**分别解决什么问题。

## Step 6：先定义"一代"的三个对象

声明与运行态分开，第 17 章已经建立了这个习惯：

```python
@dataclass(slots=True)
class ParsedEntry:
    """The declarative half of one entry."""

    id: str
    module: str
    version: int | None = None
    config: object = None
    disabled: bool = False


@dataclass(slots=True)
class GenerationEntry:
    """The runtime half of one entry in one generation."""

    parsed: ParsedEntry
    target: HotReloadTarget
    callback: Callback
    fiber: Fiber | None
    context: Context | None
    scope: EffectScope
    disposers: list[Disposer] = field(default_factory=list)
    version: int = 0
```

`GenerationEntry.version` 只统计 **config update 次数**。replacement 生成全新记录，`version` 从 0 开始——"更新了几次配置"对新一代没有意义。

第三个对象是本章的核心记账单位，它**每 entry 一个、跨 generation 存活**：

```python
class HotReloadTarget:
    """The single transactional owner of one entry's generations.

    A target outlives the code that activates it: one ``HotReloader`` keeps one
    target per entry and re-labels it for every new generation.  Three
    ledgers make "released exactly once" observable:

    * ``cleanup_counts`` is the running ledger of the resources installed by
      the current epoch;
    * ``retired[generation]`` freezes that ledger when the epoch ends;
    * ``history`` records the mode of every finished generation, so a test can
      still ask what generation N released after N+1 is already running.
    """

    def __init__(self) -> None:
        self.generation = 0
        self.entry = ""
        self.registry: dict[str, object] = {}
        self.cleanup_counts: dict[str, int] = {}
        self.retired: dict[int, dict[str, int]] = {}
        self.history: list[GenerationSnapshot] = []
```

测试需要在换代之后仍然能问"第 N 代释放了什么"。如果 target 每代新建，旧代账本随对象消失，就只能靠遍历运行树来猜；`history` 与 `retired` 就是保留这份证据的地方：

```python
@dataclass(frozen=True, slots=True)
class GenerationSnapshot:
    """What one finished generation released, kept after the target moves on."""

    generation: int
    entry: str
    cleanup_counts: Mapping[str, int]
    effects: bool
    listeners: bool
    tasks: bool

    @property
    def clean(self) -> bool:
        """True when the generation released effects, listeners and tasks."""

        return not (self.effects or self.listeners or self.tasks)

    def released(self, label: str) -> int:
        """How many times one cleanup label ran for this generation."""

        return self.cleanup_counts.get(label, 0)
```

### Checkpoint D

```text
ParsedEntry        声明：模块、版本、配置、是否禁用
GenerationEntry    运行态：本代的 Fiber、Context、scope、config 版本
HotReloadTarget    标签：generation 号、当前账本、已归档账本与历史
GenerationSnapshot 证据：一代结束时释放了什么、是不是干净
```

## Step 7：MappingModuleSource——把"版本"变成输入

正式实现把模块缓存交给宿主：宿主决定 `importlib` 何时刷新，然后调用 `Loader.replace()`。本章不触碰 `importlib`，所以需要一个替身，让"v2 是坏的，保留 v1"可以在测试里被构造出来。

```python
class MappingModuleSource:
    """A mapping-driven fake module source with explicit candidate versions."""

    def deployed(self, name: str) -> int | None:
        """The version the host currently claims is loaded for this module."""

        if name in self._pinned:
            return self._pinned[name]
        versions = self.versions(name)
        return versions[0] if versions else None

    def pin(self, name: str, version: int | None) -> None:
        """The host says "this is the code currently on disk for this module"."""

        if version not in self._table.get(name, {}):
            raise KeyError(f"{name!r} has no version {version}")
        self._pinned[name] = version

    def resolve(self, name: str, version: int | None = None) -> Callback:
        versions = self._table.get(name)
        if not versions:
            raise ExecutionError(f"cannot resolve module {name!r}", phase="resolve")
        if version is None:
            version = self.deployed(name)
        module = versions.get(cast(int, version))
        if module is None:
            raise ExecutionError(
                f"module {name!r} has no version {version}", phase="resolve"
            )
        callback = getattr(module, "apply", module)
        if not callable(callback):
            raise ExecutionError(
                f"module {name!r} v{version} is not callable", phase="resolve"
            )
        return cast(Callback, callback)
```

三条语义值得单独说明：

| 情况 | 结果 |
| --- | --- |
| `version=None` | 使用宿主的 `deployed()`（pin 过就用 pin，否则用最小已发布版本） |
| `version=2`（事件显式携带） | 使用 v2，即使 pin 仍指向 v1——这是"候选"的表达方式 |
| 版本不存在 / 模块不可调用 | `ExecutionError(phase="resolve")`，且**没有触碰任何运行态** |

测试 fixture 同时放了"好版本"和"坏版本"，让两条路都能被构造：

```python
def make_source() -> MappingModuleSource:
    return MappingModuleSource(
        {
            "app:provider": {1: provider_v1, 2: provider_v2},
            "app:listener": {1: listener_v1, 2: listener_v2},
            "app:broken": {1: provider_v1, 2: broken_activation},
            "app:cleanup": {1: cleanup_failure, 2: cleanup_failure},
        }
    )
```

```bash
cd Tutorial/20-hmr/src
PYTHONDONTWRITEBYTECODE=1 /tmp/cordis-tutorial-venv/bin/python -m pytest -p no:cacheprovider -q -k boundary
```

### Checkpoint E

```text
模块版本不再来自 importlib，而是来自 mapping：
  "当前部署的是哪个版本" → deployed()
  "这次候选是哪个版本"   → ChangeEvent(version=...)
  解析失败               → ExecutionError(phase="resolve")，运行态未动
```

## Step 8：config-only 更新——同一个 generation，新的 epoch

路由规则先写清楚：

```python
def handle(self, batch: ChangeBatch) -> None:
    """Route one merged burst; failures are recorded instead of leaking."""

    try:
        if batch.structural:
            self.replace(batch)
        else:
            self.update_config(batch)
    except BaseException as error:  # noqa: BLE001 - reload failures must not kill the host
        self.errors.append(error)
```

`handle` **不向上抛**：宿主是唯一持续运行的一方，一次失败的更新不该杀掉它的循环。错误记录在 `reloader.errors`，由宿主决定怎么处理。这一条在第 03 节的失败测试里被反复验证。

config 更新的实现：

```python
def update_config(self, batch: ChangeBatch) -> GenerationEntry:
    current = self._current(batch.entry)
    payload = batch.payload
    if payload is _MISSING:
        raise InvalidChangeError(
            f"config change for {batch.entry!r} has no payload"
        )
    validated = validate_config(getattr(current.callback, "Config", None), payload)
    fiber = current.fiber
    previous_scope = current.scope
    cleanup_error = close_scope(previous_scope)
    try:
        scope, disposers = fiber.activate(
            validated, name=current.parsed.id, scope=EffectScope()
        )
    except BaseException as error:
        ...  # 见第 03 节：用 previous_config 重新激活
    current.scope, current.disposers = scope, disposers
    current.parsed.config = validated
    current.version += 1
    return current
```

顺序是这一节的第一个关键决定：

```text
1. 校验 payload   ← 失败时运行态完全没被触碰（抛原始校验错误）
2. 释放旧 epoch   ← close_scope(previous_scope)
3. 激活新 epoch   ← 全新 scope，同一个 Fiber，generation 不变
```

把校验放在最前面，对应第 17 章已经证明过的契约："非法配置在旧 epoch 被卸载之前就被拒绝"：

```python
def test_invalid_update_keeps_the_current_epoch_installed() -> None:
    reloader.on_change(ChangeEvent(kind=ChangeKind.CONFIG, entry="provider", payload=0))
    reloader.drain()

    assert isinstance(reloader.errors[0], ValueError)  # rejected before any side effect
    assert reloader.generation("provider") == generation
    assert reloader.entry("provider").fiber is fiber
    assert fiber is not None and fiber.config == 1 and fiber.epoch == 1
    assert reloader.context("provider").get("value") == "v1:1"
    assert reloader.entry("provider").version == 0  # nothing was applied
    assert trace == ["v1:start:1"]  # the live epoch was never released
```

合法 payload 的观察结果是"同一个对象、新配置、epoch + 1"：

```python
def test_config_change_reuses_the_fiber_and_keeps_the_generation() -> None:
    reloader.on_change(ChangeEvent(kind=ChangeKind.CONFIG, entry="provider", payload=2))
    assert reloader.drain() == 1

    assert reloader.generation("provider") == generation  # no new generation
    assert reloader.live("provider") is provider_v1  # same code, new config
    assert reloader.entry("provider").fiber is fiber
    assert fiber is not None and fiber.config == 2
    assert fiber.epoch == 2
    # The old epoch really was released: stop-then-start, never two live epochs.
    assert trace == ["v1:start:1", "v1:stop:1", "v1:start:2"]
    assert mounted.target.retired == {}  # the generation never ended
    assert mounted.target.cleanup_counts["plugin: provider"] == 1
```

`trace` 的顺序是"先 stop 旧的，再 start 新的"。如果实现先激活再释放，`start:2` 会出现在 `stop:1` 之前——同一时刻存在两个活的 epoch，这是本章要避免的头号问题。

```bash
PYTHONDONTWRITEBYTECODE=1 /tmp/cordis-tutorial-venv/bin/python -m pytest -p no:cacheprovider -q -k config_change
```

### Checkpoint F

```text
config-only update
  校验        → 失败则零副作用，抛原始校验错误
  释放旧 epoch → same Fiber, scope 换新
  激活新 epoch → fiber.epoch += 1, entry.version += 1
  generation  → 不变
```

## Step 9：module 替换——三步事务

replacement 的顺序比 config update 复杂，因为它有"中间态"风险。本章把它拆成三步：

```python
    # ① 解析候选：旧一代完全未被触碰
    callback = self.source.resolve(candidate.module, candidate.version)
    # ② 释放旧一代
    cleanup_error = self._dispose_generation(current)
    # ③ 激活候选
    built = self._build(candidate, callback, target, current)
    self.generations[candidate.id] = built
```

| 失败位置 | 旧一代 | 结果 |
| --- | --- | --- |
| ① 解析（module 不存在、版本不存在、不可调用） | 完全未触碰 | `ExecutionError(phase="resolve")`，旧一代继续服务 |
| ② 清理（旧一代的 disposer 抛错） | 已释放，错误被记录 | 替换成功 + `ExecutionError(phase="cleanup")` |
| ③ 激活（新代码 setup 抛错） | 已释放 | 回滚：用旧声明重新挂载，然后报错 |

`_build` 是唯一创建新一代的地方，它只在**全部成功之后**才占用 generation 号：

```python
def _build(
    self,
    parsed: ParsedEntry,
    callback: Callback,
    target: HotReloadTarget | None = None,
    previous: GenerationEntry | None = None,
) -> GenerationEntry:
    number = self._generation_counter + 1
    owner = HotReloadTarget() if target is None else target
    previous_generation = (
        owner.generation if previous is None else previous.target.generation
    )
    owner.begin(parsed.id, number)
    try:
        pass  # 建 Context/Fiber 并激活（完整实现见 src/chapter20.py）
    except BaseException:
        owner.mark_disposed()
        # A failed candidate leaves no trace: no generation number, no
        # ledger entry, and the entry keeps the label it already had.
        owner.cleanup_counts.clear()
        owner.generation = previous_generation
        raise
    self._generation_counter = number
    return built
```

意图可以概括成一句：**失败的候选不消耗 generation 号，也不在 target 上留下痕迹。** 否则"失败一次"会让 entry 看起来比实际新，之后所有依赖 generation 的判断都会偏移。

成功的替换可以被逐项断言：

```python
def test_module_change_replaces_the_generation_and_uses_the_new_version() -> None:
    reloader.on_change(replace_event("provider", "app:provider", 2))
    assert reloader.drain() == 1

    # A replacement is a new generation: new fiber, new context, preserved target.
    assert reloader.generation("provider") == first_generation + 1
    current = reloader.entry("provider")
    assert current.target is target
    assert reloader.live("provider") is provider_v2
    assert current.fiber is not first_fiber
    assert current.context is not first_context
    assert current.context.get("value") == "v2:1"  # config survived the replacement
    assert trace == ["v1:start:1", "v1:stop:1", "v2:start:1"]
    # The replaced generation is archived and provably clean, exactly once.
    archived = target.history[0]
    assert archived.clean is True
    assert archived.released("plugin: provider") == 1
    assert target.cleanup_counts == {}  # the new generation has its own ledger
    assert first_context.get("value") is None  # the service was withdrawn
```

三个"变了"和两个"没变"，值得对照：

| 对象 | config update | module replacement |
| --- | --- | --- |
| `GenerationEntry.fiber` | 不变 | **变** |
| `GenerationEntry.context` | 不变 | **变** |
| `GenerationEntry.scope` | 变（新 epoch） | 变（新一代） |
| `GenerationEntry.version` | +1 | 归 0 |
| `HotReloadTarget` / `generation` | target 不变，号不变 | target 不变，**号 +1** |

```bash
PYTHONDONTWRITEBYTECODE=1 /tmp/cordis-tutorial-venv/bin/python -m pytest -p no:cacheprovider -q -k module_change
```

### Checkpoint G

```text
module replacement
  ① 解析候选 → 失败则旧一代零扰动
  ② 释放旧一代 → 账本归档到 target.history / target.retired
  ③ 激活候选 → 失败则回滚到 previous 声明
  成功 → generation + 1，Fiber/Context 全新，config 沿用或被同批 payload 覆盖
```

## Step 10：预检为什么必须存在

"加载一个坏模块不该打断正在运行的服务"不是靠"捕获异常然后忽略"实现的，而是靠**顺序**：任何可能抛错的解析都必须在释放旧一代之前发生。release 之后旧树已经不存在，再补救也来不及。

本章的预检比第 17 章弱（只有一次 `resolve`，不是整棵树的预检），但足以覆盖测试里的两类失败：

| 注入的坏候选 | 观察结果 |
| --- | --- |
| `app:absent`（模块不存在） | `ExecutionError(phase="resolve")`，`generation` 不变，`trace` 不变 |
| `version=2` 指向 `broken_activation` | 解析成功、激活失败 → 走回滚，`generation` 恢复为原值 |

```bash
PYTHONDONTWRITEBYTECODE=1 /tmp/cordis-tutorial-venv/bin/python -m pytest -p no:cacheprovider -q -k failed_candidate
```

### Checkpoint H

```text
预检（解析）在事务之外
  失败 → 零可观察影响
  成功 → 才允许进入"释放 + 激活"的事务段
```

## 与正式源码对应

- Loader 事务的完整形态（config-only 优先、结构变化替换、失败回滚）：[`src/cordis/loader/runtime.py`](../../cordis-py/src/cordis/loader/runtime.py)
- 正式 ConfigReloader 如何把变化交给 Loader：[`src/cordis/loader/reloader.py`](../../cordis-py/src/cordis/loader/reloader.py)
- include 组合后再重载：[`src/cordis/loader/composer.py`](../../cordis-py/src/cordis/loader/composer.py)
- 设计说明：[`docs/architecture/07-configuration-loader.md`](../../cordis-py/docs/architecture/07-configuration-loader.md) 的 "Loader 流程" 与 "HMR"

| 本章教学概念 | 正式实现中的位置 | 说明 |
| --- | --- | --- |
| `update_config` | `Loader.update()` 的 config-only 分支 | 结构对应：同一个 Fiber 换配置 |
| `replace` | `Loader.replace()` | 结构对应：整棵树替换 + 失败还原 |
| `_build` 的"成功后编号" | 正式实现没有 generation 号 | 教学扩展：为可观察性引入 |
| `MappingModuleSource.resolve` | `ModuleResolver.resolve` + 宿主刷新 import | 教学替身：解析在哪里发生 |
| `validate_config` | `ConfigValidator` 协议 | 简化：只支持 `validate`/callable |

第 17 章的 `Loader.update()` 已经在做"分类 + 事务"，本章没有发明新的分类机制：**事件种类**替代了**声明身份比较**，因为事件来自核心之外，没有 `ParsedEntry` 可以比较。除此之外，两条路径的失败语义完全沿用第 17 章。

## 与 DSH 及扩展边界对照

| DSH Loader / HMR 概念 | 本章表达 | 状态 |
| --- | --- | --- |
| 仅配置变化调用 `fiber.update()` | `update_config()` | 概念对应：本章测试覆盖该行为契约 |
| callback 身份变化时挂载新 Fiber | `replace()` 的 `_build` | 概念对应：这里由 module 事件触发 |
| HMR 不绕过 Effect 清理 | `_dispose_generation` + 账本 | 概念对应：所有资源走同一个 scope |
| 模块删除 dispose 对应 Entry | **本章不实现** | 缺口：没有 delete 事件类型 |
| 依赖仍由 Fiber 状态机协调 | **本章不实现** | 简化：Core 没有 Inject/依赖 epoch |
| 与 DSH HMR 1.0.15 等价 | **不宣称** | 需要独立 API inventory 与双语言场景 |

必须重复的边界：本章的 `HotReloader` 是**教学候选契约**。正式 `cordis-py` 只有 host-driven reload 基础（`ConfigReloader.start()`/`reload()`），没有 watcher、没有 mtime、没有模块缓存失效；DSH `@deepseek-ai/cordis-plugin-hmr` 1.0.15 也不在本章的证据范围内。

## 本章刻意简化

- 一个 burst 只处理最后一个 entry 的事件（`ChangeBatch.entry` 取 `events[-1]`），不支持一次处理多个 entry 的差异集合；
- 没有"模块删除"事件，也没有 entry 的增删（增删属于 Loader 的声明层，第 17/18 章）；
- `MappingModuleSource` 用整数版本，没有时间戳、hash 或 semver；
- 预检只解析被替换的那一个 entry，不做整棵树的预检；
- `disabled` 的 entry 不激活，`update_config` 对它直接报 `ExecutionError`，没有实现"重新启用"。

## 检查理解

1. 为什么 `update_config()` 必须先 `validate_config` 再 `close_scope`？把两步换序后，`test_invalid_update_keeps_the_current_epoch_installed` 里哪几条断言会失败？
2. `_build()` 在失败时把 `owner.generation` 恢复为 `previous_generation`。如果不恢复，`test_failed_candidate_keeps_the_previous_generation_running` 的哪一条断言会暴露它？
3. config update 后 `target.retired == {}`，而 replacement 后 `target.retired[1]` 有内容。这两个事实分别对应"什么没有发生"和"什么发生了"？
4. `GenerationEntry.version` 在 replacement 之后归 0，而 `HotReloadTarget.generation` 递增。如果两个计数器合并成一个，会丢失哪一种诊断信息？
