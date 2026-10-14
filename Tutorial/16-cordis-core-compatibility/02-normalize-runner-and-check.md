# 归一化、Runner Registry 与 expected check

上一步确定了"什么算证据"：两侧输出都必须等于同一个 `expected`。但在比较之前，两侧的输出根本长不成一个样子。这一步解决"怎么比"。

## Step 4：原始输出不能直接比较

Python 和 TypeScript 对同一件事的表示不同：

| 事实 | Python | TypeScript |
| --- | --- | --- |
| 状态枚举 | `FiberState.ACTIVE`（`StrEnum`） | `FiberState[fiber.state]` 后是字符串 |
| 序列 | `tuple` / `list` | `Array` |
| 映射顺序 | `dict` 插入序 | 对象键序 |
| 缺失值 | `None` | `undefined` |
| 失败 | 异常对象 | `Error` 实例 |

如果直接比较，得到的差异大部分是语言差异，而不是行为差异。`normalize()` 把这些表示压成一个规范形：

```python
def normalize(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): normalize(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [normalize(item) for item in value]
    if isinstance(value, BaseException):
        return normalize_failure(value)
    return {"unrepresentable": type(value).__qualname__}
```

最后一行是刻意设计：如果一个对象没有可移植表示，就得到一个**稳定的类型标记**，而不是 `repr()` 里可能出现的对象地址。两个进程对同一个 `object()` 得到同一个 `{"unrepresentable": "object"}`，比较结果才是确定的。

于是 `004` 场景里 Python 的 `FiberState.DISPOSED` 和 TypeScript 的 `"DISPOSED"` 归一化后相等：

```python
assert normalize(FiberState.ACTIVE) == "ACTIVE"
assert normalize((1, 2)) == [1, 2]
assert normalize({"b": 1, "a": 2}) == {"a": 2, "b": 1}
```

## Step 5：失败输出也是证据

runner 可能直接抛异常。管线不能因此崩溃——抛异常本身就是一条可比对的结果：

```python
def normalize_failure(error: BaseException) -> dict[str, str]:
    return {"error": type(error).__name__, "message": str(error)}
```

规则只有两条，但都很重要：

1. **异常变成数据**：`ValueError("boom")` → `{"error": "ValueError", "message": "boom"}`；
2. **不携带 traceback**：栈字符串是运行时特有的，把它写进报告会让同一行为在两边产生"差异"。

```bash
uv run pytest Tutorial/16-cordis-core-compatibility/src/test_chapter16.py -k failure_output
```

测试同时断言报告里出现 `ValueError`、不出现 `Traceback`：规范化是可读的，而不是把崩溃藏起来。

## Step 6：每一侧一个可注入的 Runner Registry

真实世界两侧的实现完全不同（Python `oracle.py`、TypeScript `oracle.mjs`），但对管线来说都只是"一个把场景变成输出的函数"：

```python
Runner = Callable[[Scenario], object]

class RunnerRegistry:
    def register(self, scenario_id: str, runner: Runner) -> Runner: ...
    def run(self, scenario: Scenario) -> object:
        runner = self._runners.get(scenario.id)
        if runner is None:
            raise UnknownRunnerError(f"no runner registered for {scenario.id!r}")
        return runner(scenario)
```

这个设计带来三件事：

- 教学测试可以注入两个假 runner，**不依赖缺失的 TypeScript build**，也不依赖 Cordis-py 的实现细节；
- "某侧没有 runner"是一种明确的 `available=False`，与"runner 跑失败"区分开；
- 未来的真实 runner 只是把 `oracle.py`/`oracle.mjs` 包一层，管线本身不变。

```python
comparison = EvidencePipeline(python=registry).evaluate({"id": "005-plugin-shapes", "expected": {}})
assert comparison.python.available is False
assert "no runner registered" in comparison.python.note
```

## Step 7：expected check 与可注入输出

"通过"的定义只有一个：归一化后的输出与归一化后的 `expected` 之间没有任何差异。

```python
normalized = normalize(raw)
diff = mismatch_diff(scenario.expected, normalized)
return SideResult(..., matches_expected=not diff, diff=diff)
```

`expected` 的比较不引入容差、不忽略额外字段、不"大致相等"。任何放宽都会让下一节的结论失去意义。

报告写到可注入的 `OutputSink`：

```python
class OutputSink(Protocol):
    def write(self, text: str) -> None: ...
```

- `NullSink`：程序内使用时的默认值；
- `RecordingSink`：测试与 CLI 用，保留每一行 JSON。

报告是一行稳定的 JSON（`sort_keys=True`）：场景 id、形状、verdict、两侧的 `available`/`matches_expected`/`normalized`/`diff`/`note` 以及版本门禁问题列表。

```bash
uv run pytest Tutorial/16-cordis-core-compatibility/src/test_chapter16.py -k report_is_written
```

### Checkpoint C

到这一步，管线可以回答"这一侧跑出来什么、是否等于 expected"，但还**没有**回答"这算不算跨语言证据"。把两件事分开，是为了让下一步的判定表可以被单独审查。

## Step 8：差异必须逐路径列出

`mismatch_diff()` 返回的是路径列表，而不是布尔值：

```python
mismatch_diff(
    {"trace": ["activate", "cleanup"], "state": "ACTIVE"},
    {"trace": ["activate"], "state": "DISPOSED", "extra": 1},
)
```

```text
$.trace[1]: missing, expected "cleanup"
$.state: expected "ACTIVE", got "DISPOSED"
$.extra: unexpected 1
```

路径语法沿用 JSON 习惯：`$.trace[1]` 表示 `trace` 的第 2 个元素。三种差异都有专门措辞：

| 情况 | 输出 |
| --- | --- |
| 键缺失 | `$.key: missing, expected …` |
| 键多余 | `$.key: unexpected …` |
| 值不同 | `$.key: expected …, got …` |

顺序也确定：映射按排序后的键、列表按下标，所以 CI 直接 diff 两次报告不会出现顺序噪声。

## Step 9：判定表只有五个结论

```python
class Verdict(StrEnum):
    CROSS_LANGUAGE = "cross-language"
    PYTHON_ONLY = "python-only"
    TYPESCRIPT_ONLY = "typescript-only"
    NEITHER = "neither"
    NOT_APPLICABLE = "not-applicable"
```

| Python 侧 | TypeScript 侧 | verdict |
| --- | --- | --- |
| 等于 expected | 没有运行 / 无 runner | `python-only` |
| 等于 expected | 等于 expected | `cross-language` |
| 不等于 expected | 等于 expected | `typescript-only` |
| 不等于 expected | 不等于 expected | `neither` |
| 任意 | 任意（host 场景） | `not-applicable` |

注意第二行与第三行的区别：`python-only` 和 `typescript-only` 都是**单边**证据，都不能支撑"等价"。`is_equivalence_evidence` 只有一种返回 `True` 的情况。

```bash
uv run pytest Tutorial/16-cordis-core-compatibility/src/test_chapter16.py -k not_cross_language
```

## Step 10：版本门禁在运行之前

TypeScript 侧的输出只有在来自 pinned build 时才算证据。门禁必须在**运行之前**检查，否则一个错误版本的结果会进入报告：

```python
@dataclass(frozen=True, slots=True)
class ReferencePin:
    version: str
    commit: str
    entry_env: str

    def issues(self, metadata) -> tuple[str, ...]: ...
```

```python
REFERENCE_PIN = ReferencePin(
    version="4.0.2",
    commit="dd6322d604e00eec1ba5e0c8541159906a21094a",
    entry_env="CORDIS_REFERENCE_ENTRY",
)
```

| 元数据情况 | `issues()` 输出 |
| --- | --- |
| `None` | 缺少 `version` 与 `commit` 的整体提示 |
| 缺 `version` | `reference metadata is missing 'version'` |
| 版本不符 | `reference version '4.0.1' does not match pinned '4.0.2'` |
| 缺 `commit` | `reference metadata is missing 'commit'` |
| commit 不符 | `reference commit '…' does not match pinned '…'` |

测试用计数器证明未通过门禁的 runner **一次都不会被调用**：

```python
assert calls == 0  # an unpinned build is never executed as evidence
assert wrong_version.verdict is Verdict.PYTHON_ONLY
```

```bash
uv run pytest Tutorial/16-cordis-core-compatibility/src/test_chapter16.py -k pinned_version
```

### Checkpoint D

现在可以把整条管线跑一遍：

```python
sink = RecordingSink()
pipeline = EvidencePipeline(
    python=python_registry,
    typescript=typescript_registry,          # 指向 pinned build 的 runner
    typescript_metadata={"version": "4.0.2", "commit": "dd6322d6…"},
    sink=sink,
)
comparison = pipeline.evaluate(scenario_document)
assert comparison.verdict is Verdict.CROSS_LANGUAGE
```

如果 `typescript` 不传或元数据不符，同一个场景会得到 `python-only`——这正是当前仓库的真实状态。
