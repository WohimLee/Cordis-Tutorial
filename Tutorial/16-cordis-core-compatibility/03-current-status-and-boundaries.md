# 当前结论、可以写下的边界

前面两步完成了管线本身。这一步把它和台账合起来，回答最初的问题，并明确哪些话现在可以说、哪些不可以。

## Step 11：把运行结果回填成结论

台账记录"已知事实"，管线产出"本次运行结论"。两者合起来才是一份可交付的结论：

```python
def conclusion(comparison: Comparison) -> str:
    if comparison.is_equivalence_evidence:
        return f"{comparison.scenario_id}: cross-language evidence"
    return f"{comparison.scenario_id}: {comparison.verdict.value} only"
```

当前仓库把 21 条 Core 场景逐条跑管线（TypeScript 侧缺 runner 或缺 pinned 元数据），得到的全部是 `python-only`；host 场景得到 `not-applicable`。`equivalence_claims()` 因此仍为空元组。

这不是"管线没用"，而是管线第一次运行就产出了正确且保守的结论：

```text
21 条 Python 单边通过   ← 与 tests/compat/README.md 一致
0  条跨语言等价          ← 因为 pinned TypeScript build 未运行
3  条 host 场景不参与    ← 不是失败，也不是通过
```

```bash
uv run pytest Tutorial/16-cordis-core-compatibility/src/test_chapter16.py
```

### Checkpoint E

到这里，本章的机制和结论都可以逐条核对：

| 问题 | 可以在哪里回答 |
| --- | --- |
| 这个场景是什么形状？ | `Scenario.from_document()` / `HOST_SCENARIO_IDS` |
| 这个场景现在什么状态？ | `REFERENCE_LEDGER` / `ledger_counts()` |
| 本次运行两侧跑出什么？ | `Comparison.report()` |
| 本次运行算不算等价证据？ | `Comparison.is_equivalence_evidence` |
| 还差什么条件才能算？ | `ReferencePin.issues()` |

## Step 12：明确写下不可以说的结论

有了可执行判定之后，"不可以说什么"终于能被写清楚：

| 说法 | 当前是否成立 | 依据 |
| --- | --- | --- |
| Python oracle 在 004–024 上 21/21 通过 | 成立 | `python:pass == 21` |
| 001–003 是 Cordis Core oracle 的失败 | **不成立** | 它们是 host 形状，`python_status == not-applicable` |
| Cordis Core 已完成跨语言等价 | **不成立** | `equivalence_claims() == ()` |
| 教学管线已经替代真实 oracle | **不成立** | 教学 runner 是注入的假实现，真实证据仍来自 [`tests/compat`](../../cordis-py/tests/compat/README.md) |

第 2 行和第 3 行是本章要防止的两种误读：把"不适用"读成"失败"，把"单边通过"读成"等价"。

这也解释了为什么第 15 章的兼容矩阵只把结论写成 `Exact`/`Equivalent`，并且注明"An entry is accepted as Exact or Equivalent only with paired TypeScript/Python evidence"（见 [`docs/cordis-core-compatibility.md`](../../cordis-py/docs/cordis-core-compatibility.md)）。本章的 `Verdict` 是那条规则的可执行版本。

## 与正式源码对应

- 共享场景目录与当前状态：[`tests/compat/README.md`](../../cordis-py/tests/compat/README.md)
- Python oracle 与 `cases` 表：[`tests/compat/oracle.py`](../../cordis-py/tests/compat/oracle.py)
- TypeScript runner 与 pinned 要求：[`tests/compat/oracle.mjs`](../../cordis-py/tests/compat/oracle.mjs)
- Core 逐条证据表：[`docs/cordis-core-compatibility.md`](../../cordis-py/docs/cordis-core-compatibility.md)
- 从教学到正式实现的过渡：[`../15-python-api/03-matrix-and-cleanup.md`](../15-python-api/03-matrix-and-cleanup.md)
- 未运行的外部条件说明：[`cordis-py/AGENTS.md`](../../cordis-py/AGENTS.md)、[`cordis-py/.agents/STATE.md`](../../cordis-py/.agents/STATE.md)

## 与 DSH Cordis 对照与扩展边界

| Cordis 侧 | 本教程表达 | 兼容状态 |
| --- | --- | --- |
| `vendor/cordis/src` Core | 00–15 章教学实现 + 正式 [`src/cordis`](../../cordis-py/src/cordis/__init__.py) | Core 已有逐条单边证据；跨语言证据待 TypeScript build |
| 共享 JSON 场景 | 本章 schema + 台账 + 管线 | Python 单边 21/21；TypeScript 侧未运行 |
| `@deepseek-ai/cordis` 4.0.2 | `REFERENCE_PIN` | 版本与 commit 显式固定 |
| Loader / Include / Timer / HMR | 17 章及以后 | 不参与 Core 等价判定，见 [`docs/cordis-core-compatibility.md`](../../cordis-py/docs/cordis-core-compatibility.md) 的范围声明 |

扩展边界同样要说清：`REFERENCE_LEDGER`、`normalize()` 的 `unrepresentable` 标记和差异路径都是本教程的**验证工具**，不是 Core 的公共 API，也不改变 [`cordis-py`](../../cordis-py/README.md) 的任何验证事实。

## 本章刻意简化

- 只比较"等于 / 不等于"，不实现场景重放、时序校验和 `invariants` 检查；
- 台账是固定快照，不提供更新脚本；真实状态变更时以 [`tests/compat/README.md`](../../cordis-py/tests/compat/README.md) 为准；
- 未实现 `001`–`003` 的 host 场景 runner，它们只作为"不参与 Core 判定"的分类样本；
- 不解析 TypeScript 侧的场景文件，不检查 `typescript_evidence` 里引用的源码是否存在。

## 检查理解

1. 如果有人把 `REFERENCE_LEDGER` 中 `004`–`024` 的 `typescript_status` 改成 `pass`，会产生什么错误结论？哪些测试会拦住它？
2. 为什么 `Verdict` 需要 `NOT_APPLICABLE` 这个值，而不是让 host 场景落到 `neither`？
3. 本章的教学 runner 是注入的假实现。它证明了什么，又没有证明什么？
4. 要让 `is_equivalence_evidence` 对 `004-core-smoke` 变为 `True`，需要外部环境提供哪些具体条件？
