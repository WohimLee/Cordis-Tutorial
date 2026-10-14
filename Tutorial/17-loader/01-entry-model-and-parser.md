# 17 — 从配置到插件树：Entry 模型与 Parser

到目前为止，挂载一个插件都要手写代码：

```python
context.plugin(probe, {"url": "sqlite:///data.db"})
```

这在演示里没问题，一旦插件来自配置文件就会立刻出问题：

```text
配置里的插件怎么带上稳定身份？改配置要重启进程吗？
解析失败时用户怎么知道是哪个文件的哪一行？
依赖顺序能不能靠"把 provider 写在前面"来保证？
```

Loader 就是回答这四个问题的组件。这一章把它拆成四个可以分别检查的职责，并明确纠正一处常见误解：**Loader 不排序依赖**。

## 本章目标

- 用 `ParsedEntry` / `Entry` 表达"声明"和"运行时状态"两层模型；
- 用 format-neutral parser 把 mapping 变成不可变声明，TOML 只是薄前端；
- 明确 parser / resolver / runtime / reloader 的职责边界；
- 说清正式 [`cordis.loader`](../../cordis-py/src/cordis/loader/__init__.py) 的真实类名。

教学实现自包含在 [`src/chapter17.py`](src/chapter17.py)，只导入标准库，不导入 [`cordis`](../../cordis-py/src/cordis)。

## Step 0：先固定正式实现的真实名字

课程规划里曾提到 `EntryGroup`、`EntryTree`、`ModuleLoader`。这些名字在正式 [`src/cordis/loader`](../../cordis-py/src/cordis/loader/__init__.py) 里**不存在**，所以本章不能照着它们教学。实际导出是：

| 正式契约 | 文件 | 职责 |
| --- | --- | --- |
| `ParsedEntry` | [`model.py`](../../cordis-py/src/cordis/loader/model.py) | 不可变声明 |
| `Entry` | [`model.py`](../../cordis-py/src/cordis/loader/model.py) | 可变运行时状态 |
| `ConfigParser` / `TomlConfigParser` / `YamlConfigParser` | [`parser.py`](../../cordis-py/src/cordis/loader/parser.py) | mapping → 声明 |
| `interpolate_env` | [`parser.py`](../../cordis-py/src/cordis/loader/parser.py) | `${NAME}` 替换 |
| `ModuleResolver` / `ModulePolicy` | [`resolver.py`](../../cordis-py/src/cordis/loader/resolver.py) | specifier → plugin |
| `Loader` | [`runtime.py`](../../cordis-py/src/cordis/loader/runtime.py) | 挂载与事务 |
| `ConfigReloader` | [`reloader.py`](../../cordis-py/src/cordis/loader/reloader.py) | 宿主驱动的重载 |
| `ConfigComposer` | [`composer.py`](../../cordis-py/src/cordis/loader/composer.py) | include/overlay（第 18 章） |
| `LoaderError` / `LoaderSecurityError` / `SourceLocation` | [`errors.py`](../../cordis-py/src/cordis/loader/errors.py) | 带来源的错误 |

"树"没有被建模成独立类：它就是 `ParsedEntry.children` 的嵌套，加上 `Loader.entries` 的扁平索引。分组同样没有独立类，17 章也不引入。

## Step 1：声明层必须不可变，而且要有稳定 id

```python
@dataclass(frozen=True, slots=True)
class ParsedEntry:
    id: str
    module: str
    config: object
    location: SourceLocation
    disabled: bool = False
    inject: Mapping[str, object | None] = field(default_factory=lambda: MappingProxyType({}))
    children: tuple[ParsedEntry, ...] = ()
```

三件事是刻意的：

1. `frozen=True`：一次解析结果不能在运行时被就地改写，否则"这次更新到底改了什么"无法回答；
2. `id` 是**稳定身份**：更新的比较、overlay 的定位、错误的归属都靠它，不靠数组下标；
3. `location` 跟着条目走：错误可以指出是哪个文件、哪一行。

`inject` 用 `MappingProxyType`，因为它是**声明**而不是可写配置：消费者声明的依赖不应该在运行期被偷偷改掉。

## Step 2：运行时状态是另一层

```python
@dataclass(slots=True)
class Entry:
    parsed: ParsedEntry              # 当前生效的声明
    plugin: Plugin | None = None
    fiber: Fiber | None = None       # 当前挂载的 Fiber
    version: int = 0                 # 配置版本
    error: BaseException | None = None
    parent: Entry | None = None
    context: Context | None = None
    children: list[Entry] = field(default_factory=list)
```

`Entry` 是 `parsed` 的**当前值**加上"挂载到什么"。同一个 id 在配置更新前后始终是同一个 `Entry` 对象，所以 `entry.version` 才能表达"这个条目被更新过几次"。

于是"更新"有了精确定义：

```text
config-only update：parsed.config 变，fiber 不变，version + 1
structural change ：module/disabled/inject/children 变，Fiber 被替换
```

## Step 3：Parser 只认 mapping，不认格式

```python
class MappingConfigParser:
    def parse(self, document, source, *, env=None, source_text=None) -> tuple[ParsedEntry, ...]:
        raw_plugins = document.get("plugins")
        if not isinstance(raw_plugins, list):
            raise LoaderError("top-level 'plugins' must be an array", SourceLocation(source))
        ...
```

TOML 只是一个读文本、产出 mapping 的前端：

```python
class TomlConfigParser(MappingConfigParser):
    def loads(self, text, source, *, env=None):
        document = tomllib.loads(text)
        return self.parse(document, source, env=env, source_text=text)
```

这个分层让第 18 章的 JSON 适配器和（项目已锁定依赖的）YAML 适配器可以复用同一个 parser。教学版只带 TOML，因为它是标准库。

解析时逐项校验，每条错误都带位置：

| 配置错误 | 结果 |
| --- | --- |
| 顶层 `plugins` 不是数组 | `top-level 'plugins' must be an array` |
| 条目缺 `id` | `plugin entry requires a non-empty string 'id'` |
| 同一 id 出现两次 | `duplicate plugin entry id 'same'` |
| `disabled` 不是布尔 | `plugin 'disabled' must be boolean` |
| `inject` 不是字符串键 | `plugin 'inject' must be a string-keyed object` |
| `config` 里 `${NAME}` 未设置 | `environment variable 'DATABASE_URL' is not set` |

注意 `${NAME}` 只做字符串替换，不执行 Python 表达式——这一点和第 01–16 章的效果所有权一样，属于"声明数据不得变成代码"。

```bash
uv run pytest Tutorial/17-loader/src/test_chapter17.py -k toml_parser
```

### Checkpoint A

到这里，一个 TOML 文档会被变成不可变的 `ParsedEntry` 森林，每个条目都有稳定 id、来源路径和可选行号。运行时还没有被触碰——这正是下一步的前提。

## Step 4：四个职责，一次分清

| 职责 | 输入 → 输出 | 不知道什么 |
| --- | --- | --- |
| Parser | 文本/mapping → `ParsedEntry` | 插件对象、Fiber、依赖 |
| Resolver | `module:attribute` → plugin 对象 | 配置内容、生命周期 |
| Runtime（`Loader`） | `ParsedEntry` → `Entry` + Fiber | 文本格式、import 机制 |
| Reloader | 文件变化 → 再次组合 + `Loader.update()` | 是否该重载（宿主决定） |

这张表最重要的用途是排除一种错误实现：

```text
❌ Loader 按 inject 拓扑排序，再依次挂载
✅ Loader 按配置顺序创建 Fiber，由 Fiber 自己等待依赖
```

第 02 篇会用两条 trace 证明教学实现走的是第二条路。

## 与正式源码对应

- Loader 导出清单：[`src/cordis/loader/__init__.py`](../../cordis-py/src/cordis/loader/__init__.py)
- 声明与运行时模型：[`src/cordis/loader/model.py`](../../cordis-py/src/cordis/loader/model.py)
- format-neutral parser 与 TOML/YAML 前端：[`src/cordis/loader/parser.py`](../../cordis-py/src/cordis/loader/parser.py)
- 来源与错误：[`src/cordis/loader/errors.py`](../../cordis-py/src/cordis/loader/errors.py)
- 设计说明（Loader 分层与流程）：[`docs/architecture/07-configuration-loader.md`](../../cordis-py/docs/architecture/07-configuration-loader.md)
- 解析层测试：[`tests/test_loader_parser.py`](../../cordis-py/tests/test_loader_parser.py)

## 与 DSH Cordis 对照

下表只说"当前 Python 实现里长什么样"，**不是**兼容性分类。`Exact`/`Equivalent`/`Language-specific` 这些词属于 [`docs/cordis-core-compatibility.md`](../../cordis-py/docs/cordis-core-compatibility.md) 的 Core 矩阵，本节不使用它们。

| Cordis 侧的命名/概念 | 当前 Python Loader 的实际契约 | 分类词（非兼容结论） |
| --- | --- | --- |
| Loader 包的 Entry/EntryTree 概念 | `ParsedEntry.children` + `Loader.entries` | 教学映射：Cordis-py 扩展内部的树表示 |
| `ModuleLoader` 之类的命名 | 实际导出 `ModuleResolver` + `ModulePolicy` | Python 设计选择：正式导出的名字，不是 Core 分类 |
| 模块 specifier | `module:attribute` | Python 设计选择：Python 没有等价的包路径约定 |
| 配置来源位置 | `SourceLocation` | 结构对应：字段与用途一致，未做逐项等价验证 |

相邻解释同样重要：`ParsedEntry.children` 加 `Loader.entries` 只是**当前 Cordis-py 扩展内部**如何表示一棵树，它没有和 DSH Loader 的 Entry/EntryTree 做过逐项对照，因此不构成 DSH Loader 等价的证据。`ModuleResolver` 与 `ModulePolicy` 确实是 [`src/cordis/loader/__init__.py`](../../cordis-py/src/cordis/loader/__init__.py) 实际导出的名字（予以保留），但它们同样是 Python 扩展的设计选择，而不是 Core 矩阵里的 `Language-specific` 分类。

范围必须完整说出：Loader **不在** [`docs/cordis-core-compatibility.md`](../../cordis-py/docs/cordis-core-compatibility.md) 的对照范围内，它被列为 Cordis-py 扩展（"explicit Loader APIs under `cordis.loader`"）。所以本章不宣称 DSH Loader 1.0.0-rc.5 等价，只对照当前 Python 实现；上面这张表也就不能当成兼容矩阵的结论来引用。

## 本章刻意简化

- Parser 只带 TOML（标准库）；YAML 由第 18 章的 adapter 边界解释，不与正式 `YamlConfigParser` 逐行对齐；
- `MappingProxyType` 只保护 `inject`，`config` 仍是普通嵌套结构，因为教学实现要演示 overlay 深合并；
- `SourceLocation.line` 在 parser 能找到 module 字符串时给出真实行号，找不到时回退到数组下标，与正式解析器的 `_locate`/fallback 策略一致；
- 不实现 include、watcher、模块缓存 HMR 和 `disabled` 的条件表达式，`disabled` 只是布尔。

## 检查理解

1. 为什么 `ParsedEntry` 必须 `frozen`，而 `Entry` 必须可变？两者交换会坏掉什么？
2. `location` 放在声明层而不是运行时层，能给哪些错误带来好处？
3. 如果 parser 直接返回 `Context.plugin()` 的调用结果，第 04 步的职责表会退化成什么？
4. `inject` 用 `MappingProxyType`、`config` 用普通 dict，这个不对称是随意决定的吗？
