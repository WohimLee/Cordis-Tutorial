# 重复 id、overlay 与组合的确定性

include 解决了"配置分散在多个文件"，但立刻带来两个新问题：两个文件定义了同一个 id 怎么办？环境差异怎么表达而不复制整份配置？这两个问题的答案必须互不冲突。

## Step 5：重复 id 必须在组合期拒绝

一种常见的"方便"实现是后出现的覆盖先出现的：

```python
index[entry_id] = entry     # ❌ 静默覆盖：用户永远不知道哪份配置生效了
```

这样做的代价是：配置文件里最危险的事故（两个文件定义同一个插件）变成一个静默行为。教学实现选择报告：

```python
if entry_id in result:
    raise ComposerError(
        f"duplicate plugin entry id {entry_id!r}",
        cls._location(entry, source),
    )
```

`_index()` 会**展开嵌套子条目**，所以唯一性作用在整棵树上，而不只是顶层数组：

```text
root: [ {id: "same"}, {id: "parent", plugins: [ {id: "same"} ]} ]   → 拒绝
```

错误位置指向**第二次出现**的那个声明，因为第一次是合法声明，冲突是第二次造成的：

```python
with pytest.raises(ComposerError) as caught:
    composer.load(root)
assert caught.value.location == SourceLocation(second.resolve())
```

覆盖需求并没有被禁止，它只是被引导到唯一的显式机制：`overlay`。

```bash
uv run pytest Tutorial/18-include/src/test_chapter18.py -k duplicate
```

## Step 6：overlay 按 entry id 定位

```toml
# environments/dev.toml
include = ["../config/base.toml"]

[overlays.database]
disabled = true
module = "app.database:v2"
config.pool.size = 8
```

组合结果：

| 字段 | 规则 | 结果 |
| --- | --- | --- |
| `module` | 替换，并把来源改为 overlay 所在文件 | `app.database:v2` |
| `disabled` | 替换 | `true` |
| `config` | 与已有 mapping **深合并** | `{"url": "sqlite:///default.db", "pool": {"size": 8}}` |
| `inject` | 深合并 | 原有依赖保留，补上新增依赖 |

三条边界规则同样重要：

1. **只认 id，不认下标**：`overlays.database` 指向 id，不是"第 0 个插件"。数组顺序变化不会让 patch 打到别的条目上。
2. **缺失目标要报错**：`overlay target 'absent' does not exist`。静默忽略会让"改了配置没生效"变成最难查的一类问题。
3. **未知字段要报错**：`overlay for 'app' contains unsupported field 'unknown'`。允许任意字段会让拼写错误（`disable` vs `disabled`）静默通过。

支持字段被收紧成一个白名单：

```python
_OVERLAY_FIELDS = frozenset({"module", "config", "disabled", "inject"})
_MERGED_FIELDS = frozenset({"config", "inject"})
```

`config` 与 `inject` 深合并的规则是确定的递归定义：

```python
def _deep_merge(base, overlay):
    result = deepcopy(dict(base))
    for key, value in overlay.items():
        previous = result.get(key)
        if isinstance(previous, Mapping) and isinstance(value, Mapping):
            result[key] = _deep_merge(previous, value)
        else:
            result[key] = deepcopy(value)
    return result
```

只有"两边都是 mapping"才递归；此时 overlay 的标量/列表直接替换，未提及的键保留。`deepcopy` 不是为了性能，而是为了避免**别名污染**：如果 overlay 的嵌套 dict 被直接放进结果，修改其中一份会影响另一份。

`module` 变化时来源会被重新归属到 overlay 所在文件——这是"是谁改的"这一问题的答案：

```python
assert database.location == SourceLocation(root.resolve())
```

```bash
uv run pytest Tutorial/18-include/src/test_chapter18.py -k overlay
```

### Checkpoint D

组合的覆盖语义只有一条路径：

```text
新条目       → 写进对应文件
修改已有条目 → overlay.<id>
重复 id      → 组合期报错（不覆盖）
```

## Step 7：组合必须是纯函数

`ComposedEntry` 是 frozen dataclass，组合过程中没有 Fiber、没有 Context、没有文件系统写入——只有读。这条纪律带来两个可验证的性质。

**性质一：同一份内容，不同格式，结果相同。**

```python
from_toml = ConfigComposer(TomlMappingAdapter()).load(toml_source)
from_json = ConfigComposer(JsonMappingAdapter()).load(json_source)
assert shape(from_toml) == shape(from_json)
```

```bash
uv run pytest Tutorial/18-include/src/test_chapter18.py -k adapters
```

**性质二：组合失败不留半成品。** 组合器内部用列表和 `deepcopy` 组织中间结果，任何一步抛错都不会触碰调用方的状态；唯一的副作用是读取文件。

把"读"和"改"分开，是第 8 步能让"失败不扰动运行树"成立的前提。

### Checkpoint E

```text
输入：一组 mapping + 声明点路径
输出：不可变的 ComposedEntry 森林，或一个带来源的 ComposerError
副作用：只读文件
```
