# 16 — 场景 schema 与证据台账

00–15 章已经把 Cordis Core 的机制逐个实现完。到这里教程面对的不再是"还缺哪个机制"，而是一个更难回答的问题：

```text
我们凭什么说 Python 运行时与 TypeScript Cordis 是等价的？
```

这一章不重新实现 Cordis，也不重新运行 Cordis。它实现的是**证据管线**：一套能判定"什么才算跨语言证据"的机制。因为最危险的错误不是实现错，而是**把单边通过当成等价**。

## 本章目标

- 为共享场景建立 schema，并区分 Cordis Core oracle 场景与 Harness 形状的 host 场景；
- 把当前真实运行状态写成可核对的证据台账；
- 说明为什么"Python oracle 21/21 通过"不能推出"Core 等价完成"。

教学实现自包含在 [`src/chapter16.py`](src/chapter16.py)，只导入标准库，不导入 [`cordis`](../../cordis-py/src/cordis)。

## Step 0：一个会出错的说法

[`tests/compat/README.md`](../../cordis-py/tests/compat/README.md) 有两句话：

```text
the Python oracle has been run with --check over the 21 Cordis Core scenarios
004–024 and all of them passed.

The TypeScript side has not been run ... so no scenario currently counts as
cross-language equivalence evidence.
```

如果只读第一句就写下"Core 等价完成"，就漏掉了证据结构：

| 说法 | 实际含义 |
| --- | --- |
| Python oracle 21/21 通过 | **一侧**的运行结果与 `expected` 相等 |
| 跨语言等价 | **两侧**的输出都等于同一个 `expected` |

这两件事都不是"大概差不多"：它们是两个可以逐条检查的布尔值。证据管线要做的第一件事，就是让这个区别在代码里无法被绕过。

## Step 1：场景文档不是一种形状

Core oracle 场景 `expected` 是一个完整的结果对象：

```json
{
  "id": "004-core-smoke",
  "steps": [{"action": "mount", "plugin": "probe"}, {"action": "dispose-root"}],
  "expected": {"trace": ["activate", "event:value", "cleanup"], "fiber_state": "DISPOSED"}
}
```

完整文件见 [`004-core-smoke.json`](../../cordis-py/tests/compat/scenarios/004-core-smoke.json)。三个 host 场景描述的却是 Harness 里的外部行为，它们的 `expected` 各不相同，而且都不是"一个可比较的结果对象"：

| 场景 | 顶层 `expected` 形状 | 说明 |
| --- | --- | --- |
| [`001-provider-replacement`](../../cordis-py/tests/compat/scenarios/001-provider-replacement.json) | 列表 | 每一步的状态记录 `[{"plugin": "consumer", "state": "PENDING"}, …]` |
| [`002-harness-adapter-reload`](../../cordis-py/tests/compat/scenarios/002-harness-adapter-reload.json) | 对象 | `answers`、`agent_fiber_reused`、`shutdown` 等宿主产物 |
| [`003-harness-llm-service-loss`](../../cordis-py/tests/compat/scenarios/003-harness-llm-service-loss.json) | 无 `expected` | 只有 `expected_agent_states` 顺序表 |

关键事实是：[`oracle.py`](../../cordis-py/tests/compat/oracle.py) 的 `cases` 表里没有这三个 id，调用它们只会得到 `SystemExit: unsupported scenario`。所以它们既不是 pass 也不是 fail，而是"这套 Core 管线不适用"。

这个观察直接否定了两种偷懒的分类法：

```text
"有 expected 的就是 core"      001/002 会误判
"有 typescript_evidence 的就是 host"  001 没有该字段，会误判
```

schema 必须显式表达形状与分类，而不是让缺字段或多字段的文档静默通过：

```python
class EvidenceKind(StrEnum):
    CORE = "core-oracle"
    HOST = "host-shaped"

@dataclass(frozen=True, slots=True)
class Scenario:
    id: str
    kind: EvidenceKind
    expected: object
```

`Scenario.from_document()` 负责校验与分类；Core 场景缺 `expected` 时抛出带路径的 `ScenarioSchemaError`：

```python
raise ScenarioSchemaError("core oracle scenario requires 'expected'", "$.expected")
```

```bash
uv run pytest Tutorial/16-cordis-core-compatibility/src/test_chapter16.py -k scenario_schema
```

### Checkpoint A

schema 有三条拒绝路径，每条都指向具体字段而不是笼统的"格式错误"：

```text
$.id                  id 不是非空字符串
$.expected            Core 场景没有 expected
$.typescript_evidence typescript_evidence 不是字符串数组
```

host 场景被判定为 `kind = HOST` 后，管线不再解释它的任何 `expected` 形状——教学实现显式记录这一点，而不是猜。

## Step 2：分类来自台账，不能靠猜

一个自然的想法是"有 `typescript_evidence` 字段的就是 host 场景"。这条规则在第 2 步就会失效：

| 场景 | 是否有 `typescript_evidence` |
| --- | --- |
| `001-provider-replacement` | 没有 |
| [`002-harness-adapter-reload`](../../cordis-py/tests/compat/scenarios/002-harness-adapter-reload.json) | 有 |
| [`003-harness-llm-service-loss`](../../cordis-py/tests/compat/scenarios/003-harness-llm-service-loss.json) | 有 |

三个场景都是 host 形状，字段却不一样。所以分类必须是**输入数据**，不能从字段推断：

```python
HOST_SCENARIO_IDS: tuple[str, ...] = (
    "001-provider-replacement",
    "002-harness-adapter-reload",
    "003-harness-llm-service-loss",
)
CORE_SCENARIO_IDS: tuple[str, ...] = ("004-core-smoke", ..., "024-remaining-contracts")
```

`Scenario.from_document()` 只做一次 `id in HOST_SCENARIO_IDS` 判断。这样 host 场景永不会被送进 Core 运行器，也不会被算成 Python 失败。

## Step 3：把真实状态写成台账

台账是 24 条记录，每条有两个独立状态轴：

```python
@dataclass(frozen=True, slots=True)
class LedgerRecord:
    scenario: str
    kind: EvidenceKind
    python_runner: bool
    python_status: LedgerStatus
    typescript_status: LedgerStatus
```

当前值是：

| 场景区间 | 形状 | `python_status` | `typescript_status` |
| --- | --- | --- | --- |
| `001`–`003` | host | `not-applicable` | `not-run` |
| `004`–`024` | core | `pass` | `not-run` |

`not-applicable` 不是"失败"：oracle 不实现 host 场景，把它们记成 fail 会制造 3 个虚假缺陷；记成 pass 则是伪造证据。第三方状态存在的原因正是这两个选择都不可接受。

台账是一个**快照**，和 15 章的 `CORDIS_ROOT_EXPORTS` 一样，不在测试运行时去读 [`tests/compat/scenarios/`](../../cordis-py/tests/compat/scenarios/004-core-smoke.json)：教学测试必须自包含，不能因为参考仓库换目录而变红或变绿。

```python
assert counts["python:pass"] == 21
assert counts["python:not-applicable"] == 3
assert counts["typescript:not-run"] == 24
assert equivalence_claims() == ()
```

```bash
uv run pytest Tutorial/16-cordis-core-compatibility/src/test_chapter16.py -k host_scenarios
```

### Checkpoint B

"21/21 通过"依然成立，但只能写成：

```text
Python oracle 单边通过：004–024 共 21 条。
跨语言等价：0 条，因为 TypeScript 侧一次都没有运行。
```

这就是本章要守住的边界。后面的 Step 会把两侧的运行、归一化和比较真正实现出来。

## 与正式源码对应

- 共享场景目录与当前状态：[`tests/compat/README.md`](../../cordis-py/tests/compat/README.md)
- Python oracle 与它的场景表：[`tests/compat/oracle.py`](../../cordis-py/tests/compat/oracle.py)
- TypeScript runner 与 `CORDIS_REFERENCE_ENTRY` 要求：[`tests/compat/oracle.mjs`](../../cordis-py/tests/compat/oracle.mjs)
- Core 逐条证据表：[`docs/cordis-core-compatibility.md`](../../cordis-py/docs/cordis-core-compatibility.md)
- Harness 形状场景的出处：[`docs/compatibility.md`](../../cordis-py/docs/compatibility.md)

## 与 DSH Cordis 对照

| Cordis 侧机制 | Python 教学表达 | 关系 |
| --- | --- | --- |
| `vendor/cordis` 源码测试 | 共享 JSON 场景 | 场景描述外部可观察行为，不绑定对象布局 |
| TypeScript oracle | 可注入的 `RunnerRegistry`（下一节） | 只有指向 pinned build 的输出才计入证据 |
| `CORDIS_REFERENCE_ENTRY` | `ReferencePin.entry_env` | 环境变量名保持一致 |
| pinned 版本 `4.0.2` @ `dd6322d…` | `REFERENCE_PIN` | 版本与 commit 必须同时匹配 |

## 本章刻意简化

- 台账是 2026-10-14 的快照，不动态扫描 [`tests/compat/scenarios`](../../cordis-py/tests/compat/scenarios/004-core-smoke.json)，也不依赖已安装的 Cordis；
- `typescript_evidence` 只校验形状，不解析其中的 TypeScript 文件路径；
- schema 不校验 `expected` 的 JSON 可序列化性，`normalize()`（下一节）负责把无法表达的对象显式标记出来；
- 本章不实现 `oracle.py` 的任何一个 Core 场景 runner，runner 由测试注入。

## 检查理解

1. 为什么把 `001`–`003` 记成 `pass` 也不行？它和记成 `fail` 分别会造成什么后果？
2. `expected` 缺失时抛 `ScenarioSchemaError`，而不是让 `expected` 默认为空对象，这个选择保护了什么？
3. 如果有读者把 `REFERENCE_LEDGER` 改成"运行时读取真实场景目录"，会引入哪两类不稳定？
4. `equivalence_claims()` 在当前台账上返回空元组。要让它返回非空，需要哪个外部条件成立？
