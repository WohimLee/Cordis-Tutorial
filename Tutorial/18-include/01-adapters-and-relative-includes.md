# 18 — 配置组合：adapter 边界与相对路径 include

第 17 章让 Loader 能挂载一棵声明树，但声明树只有一个来源：一个文件。真实项目不会这样配置：

```text
cordis.toml          基础设施（数据库、日志）
config/agent.toml    Agent 与工具
environments/dev.toml 开发环境覆盖
```

把这几份拼起来，会立刻遇到三个问题：

```text
include 的相对路径以谁为基准？从别的目录启动还能找到吗？
某个条目来自哪个文件？出错时能指出文件吗？
两个文件都定义了 id = "database"，应该合并还是报错？
```

这一章只引入一个锚点：`ConfigComposer`。它会回答上面三个问题，并且**不会**被做成第二个 Loader。

## 本章目标

- 用 adapter 边界把"文本格式"和"组合语义"彻底分开；
- 实现相对路径的递归 `include` 与规范化路径的循环检测；
- 让每个条目保留来源文件，使错误可定位；
- 按 entry id 应用 `overlay`/patch，且组合失败不扰动运行中的树。

教学实现自包含在 [`src/chapter18.py`](src/chapter18.py)，只导入标准库，不导入 [`cordis`](../../cordis-py/src/cordis)。

## Step 0：先纠正两个常见的错误设计

**错误一：以为正式实现里有一个独立的 `Include` 类。**

正式 [`src/cordis/loader`](../../cordis-py/src/cordis/loader/__init__.py) 的导出里没有 `Include`。`include` 是 [`ConfigComposer`](../../cordis-py/src/cordis/loader/composer.py) 接受的一个 **配置键**；组合逻辑是 composer 的职责。本章不创建 `Include` 类，因为那样会凭空造出一个正式实现不存在的锚点。

**错误二：相对路径以当前工作目录为基准。**

```python
include = (Path.cwd() / value).resolve()   # ❌ 换一个启动目录就找不到
```

正确的基准是**声明它的那个文件**所在目录：

```python
return tuple((source.parent / item).resolve() for item in values)
```

这两条设计决定合起来定义了本章的边界：composer 是唯一的组合入口，而组合的位置语义来自声明点，不来自进程状态。

## Step 1：格式适配器与 Composer 的边界

Composer 不解析文本，它只接收 mapping：

```python
class MappingAdapter(Protocol):
    def read(self, source: Path) -> Mapping[str, object]: ...

class TomlMappingAdapter:
    def read(self, source: Path) -> Mapping[str, object]:
        return tomllib.loads(source.read_text())

class JsonMappingAdapter:
    def read(self, source: Path) -> Mapping[str, object]:
        value = json.loads(source.read_text())
        if not isinstance(value, Mapping):
            raise ValueError("top-level JSON document must be an object")
        return cast(Mapping[str, object], value)
```

于是"TOML 还是 JSON"对组合逻辑完全不可见：两个 adapter 喂同一个 composer，产出同一组 `ComposedEntry`。

YAML 也一样，只是解析器换成项目已锁定的 PyYAML。它属于 adapter 一侧，而不是 composer 一侧：

```python
class YamlMappingAdapter:  # adapter 的职责：文本 → mapping
    def read(self, source: Path) -> Mapping[str, object]:
        document = yaml.safe_load(source.read_text())
        if not isinstance(document, Mapping):
            raise ValueError("top-level YAML document must be an object")
        return document
```

```bash
uv run pytest Tutorial/18-include/src/test_chapter18.py -k adapters
```

`ConfigComposer` 只看到 `read()` 的返回值。如果哪天 YAML 解析器要在条目上标注位置，那是 adapter 附加 `_cordis_line` 的事情（第 3 步会用到），composer 的代码不需要改。

读取失败也被包装成带来源的错误：

| 情况 | 结果 |
| --- | --- |
| 文件不存在 | `ComposerError("cannot read config …")`，`location.path` 是被 include 的路径，消息里带 include 链 |
| 格式非法 | `ComposerError("invalid configuration: …")`，`location.path` 是该文件 |
| 顶层不是 mapping | `ComposerError("configuration document must be a mapping")` |
| `include` 不是字符串或字符串数组 | `ComposerError("'include' must be a path string or an array of path strings")` |

## Step 2：include 是递归的，顺序是确定的

```python
def _compose(self, source: Path, stack: tuple[Path, ...]) -> list[dict[str, object]]:
    document = self._read(source, stack)
    plugins: list[dict[str, object]] = []
    for included in self._include_paths(document, source):
        plugins.extend(self._compose(included, (*stack, source)))
    plugins.extend(self._annotate(document.get("plugins", []), source))
    ...
```

三条语义：

1. **基准**：`source.parent / item`，即"声明点所在目录"；
2. **顺序**：先按声明顺序展开全部 include，再接本文档自己的条目；
3. **规范化**：`Path.resolve()` 让同一文件的不同写法（`./a.toml`、`../x/a.toml`、符号链接）得到同一个键——循环检测依赖这一点。

嵌套相对路径因此是自然结果。下面三份文件互相引用两跳：

```toml
# environments/dev.toml
include = ["../config/agent.toml"]
[[plugins]]
id = "agent"
module = "app.agent:plugin"
```

```toml
# config/agent.toml
include = ["../shared/base.toml"]
[[plugins]]
id = "cache"
module = "app.cache:plugin"
```

```toml
# shared/base.toml
[[plugins]]
id = "database"
module = "app.database:plugin"
```

组合结果：

```python
assert [entry.id for entry in entries] == ["database", "cache", "agent"]
```

```bash
uv run pytest Tutorial/18-include/src/test_chapter18.py -k relative
```

### Checkpoint A

`include` 的顺序由"先展开子文件、再接本文件"唯一确定。没有"按 id 排序"这类隐式规则，所以两次运行、两台机器的结果一致。

## Step 3：每个条目都要记住自己来自哪个文件

组合会丢失来源，这是最常见的退化。教学实现保留两个注解：

```python
_SOURCE = "_cordis_source"
_LINE = "_cordis_line"
```

它们沿用正式 [`parser.py`](../../cordis-py/src/cordis/loader/parser.py) 使用的键名，含义是"这条声明由哪个文件的哪一行产生"。组合结束后转成 `ComposedEntry`：

```python
@dataclass(frozen=True, slots=True)
class ComposedEntry:
    id: str
    module: str
    config: object
    disabled: bool
    inject: Mapping[str, object]
    location: SourceLocation
    children: tuple[ComposedEntry, ...] = ()
```

`SourceLocation.line` 是 `int | None`，这是本章与正式实现的一处**有意差异**：

| 情况 | 正式 parser | 教学 Composer |
| --- | --- | --- |
| adapter 提供了位置 | 使用真实行号 | 使用真实行号 |
| adapter 未提供位置 | 回退到数组下标（伪行号） | `line = None` |

教学版选择 `None`，因为数组下标不是行号。把序号伪装成行号，会让"错误定位"这个卖点在第一次误报时崩掉。第 7 步会演示 adapter 提供位置时，错误消息里出现真实的 `文件:行号`。

来源归属在嵌套结构里同样成立：`shared/base.toml` 里声明的条目，即使被两跳 include 进来，`location.path` 仍然是 `shared/base.toml`。

```bash
uv run pytest Tutorial/18-include/src/test_chapter18.py -k positions
```

### Checkpoint B

到这里可以回答"这个条目是谁声明的"：

```text
entries[i].location.path  → 声明文件
entries[i].location.line  → 行号（adapter 提供时）或 None
children[j].location      → 同样保留，嵌套不丢失
```

## Step 4：循环检测用规范化路径

有了规范化路径，循环检测就是"当前路径是否已经在展开栈里"：

```python
if source in stack:
    chain = " -> ".join(str(path) for path in (*stack, source))
    raise ComposerError(f"include cycle detected: {chain}", SourceLocation(source))
```

一个两跳循环只会给出两条路径，很难排查：

```toml
# first.toml
include = "second.toml"
# second.toml
include = "first.toml"
```

三跳以上才能暴露真正的错误形状，所以测试用 `first → second → nested/third → first`，并断言链的顺序：

```python
assert "include cycle detected" in message
assert caught.value.location == SourceLocation(first.resolve())
assert message.index(str(first.resolve())) < message.index(str(second.resolve()))
assert message.index(str(second.resolve())) < message.index(str(third.resolve()))
```

`location` 指向**闭合循环的那次 include 的目标**（重复出现的 `first.toml`），链里则保留完整路径，读者既能知道错在哪个文件，也能看到自己是怎么绕回来的。

```bash
uv run pytest Tutorial/18-include/src/test_chapter18.py -k cycle
```

### Checkpoint C

三条与"来源"有关的契约都已经成立：

```text
相对路径：以声明文件为基准，递归展开
规范化  ：不同写法得到同一个键，循环检测可靠
来源    ：每个条目（含嵌套子条目）保留声明文件
```
