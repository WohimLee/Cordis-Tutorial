# 失败隔离、整章回归与边界

组合是纯函数，但它的结果要被装进正在运行的进程。这一步解决最后一个问题：**组合失败时，已经在跑的那棵树怎么办？**

## Step 8：先组合，再安装，最后更新快照

顺序就是答案。`ConfigSession` 把重载拆成三段：

```python
def reload(self) -> bool:
    if self.snapshot is None:
        raise RuntimeError("config session has not been loaded")
    candidate = self.composer.load(self.source)     # ① 纯组合，可能失败
    if candidate == self.snapshot:
        return False                                # 内容未变
    self.runtime.install(candidate)                 # ② 原子换树
    self.snapshot = candidate                       # ③ 成功后记录
    return True
```

三段的分工是刻意的：

| 阶段 | 会不会失败 | 失败后旧树 |
| --- | --- | --- |
| ① 组合 | 会（语法、重复 id、循环、缺文件） | 完全未触碰 |
| ② 安装 | 不会（新树先建好再替换引用） | 被替换为新树 |
| ③ 快照 | 不会 | 保持旧快照，直到 ② 成功 |

关键在于 ① 是纯的。如果实现成"边组合边往运行树里塞条目"，那么一个在第三个文件才发现的重复 id 会留下半棵新树——这正是最糟糕的状态：新旧混合、无法描述。

安装本身也遵循"先建后换"：

```python
def install(self, entries: tuple[ComposedEntry, ...]) -> None:
    nodes = self._build(entries)   # 先完整构建
    self.roots = nodes             # 再一次性替换引用
```

于是失败路径可以被逐项断言：

```python
session.load()
previous_snapshot = session.snapshot
previous_activations = list(runtime.activations)

write(second, '[[plugins]]\nid = "same"\nmodule = "b:plugin"\n')
with pytest.raises(ComposerError, match="duplicate plugin entry id 'same'"):
    session.reload()

assert runtime.ids == ("same", "other")          # 运行树没变
assert runtime.activations == previous_activations  # 没有多余挂载动作
assert session.snapshot is previous_snapshot     # 快照没变
```

注意第三条断言用了 `is`：新旧快照必须是**同一个对象**，而不只是"内容相等"。内容相等可能掩盖"重新组合了一份等价配置"这种实现。

```bash
uv run pytest Tutorial/18-include/src/test_chapter18.py -k composition_failure
```

### Checkpoint F

失败隔离的完整契约：

```text
组合失败 → 抛出 ComposerError（带来源）
         → 运行树不变、挂载动作不变、快照不变
成功     → 原子换树 + 新快照
内容未变 → 不换树、不更新快照、返回 False
```

## Step 9：整章一起跑

```bash
uv run pytest Tutorial/18-include/src/test_chapter18.py
```

十条测试覆盖的契约：

| 测试 | 覆盖的契约 |
| --- | --- |
| `relative` | 两跳嵌套相对路径、include 顺序、来源路径 |
| `cycle` | 三跳循环、链顺序、闭合位置 |
| `duplicate` | 跨 include 的重复 id、第二次声明的位置 |
| `overlay` | 深合并、字段替换、module 重新归属 |
| `overlay_rejects` | 缺失目标、未知字段、非法 overlays 结构 |
| `adapters` | TOML/JSON 同源同结果（composer 只吃 mapping） |
| `positions` | adapter 提供的行号进入 `location` 与错误消息 |
| `reload` | 增删条目、内容未变返回 `False` |
| `composition_failure` | 失败不扰动运行树与快照 |
| `missing_include` | 缺文件时的 include 链与来源路径 |

### Checkpoint G

这一章建立的能力可以一句话概括：

```text
多个配置文件 → （adapter → mapping） → 递归 include + overlay
            → 带来源的不可变条目 → 原子安装 / 失败不改动
```

## 与正式源码对应

- 组合器与 overlay 规则：[`src/cordis/loader/composer.py`](../../cordis-py/src/cordis/loader/composer.py)
- 来源注解键与 parser 的结合：[`src/cordis/loader/parser.py`](../../cordis-py/src/cordis/loader/parser.py)
- 来源与错误类型：[`src/cordis/loader/errors.py`](../../cordis-py/src/cordis/loader/errors.py)
- 宿主驱动重载与快照：[`src/cordis/loader/reloader.py`](../../cordis-py/src/cordis/loader/reloader.py)
- 组合测试与 YAML 适配测试：[`tests/test_loader_composer.py`](../../cordis-py/tests/test_loader_composer.py)、[`tests/test_loader_yaml.py`](../../cordis-py/tests/test_loader_yaml.py)
- 设计说明：[`docs/architecture/07-configuration-loader.md`](../../cordis-py/docs/architecture/07-configuration-loader.md) 的 "Include 与 Overlay"
- 上一章：Loader 的挂载与事务 [../17-loader/03-update-replace-and-rollback.md](../17-loader/03-update-replace-and-rollback.md)

## 与 DSH Cordis 对照与扩展边界

| DSH Include 概念 | 本教程表达 | 状态 |
| --- | --- | --- |
| 展开配置文件来源 | `ConfigComposer` + `MappingAdapter` | 部分对应：只实现相对路径 include 与 overlay |
| 相对路径解析 | 以声明文件为基准 | Equivalent |
| 循环检测 | 规范化路径 + 展开栈 | Equivalent |
| 来源信息 | `ComposedEntry.location` | Equivalent（行号依赖 adapter） |
| overlay / patch | `[overlays.<id>]`，白名单字段 + 深合并 | 部分对应 |
| Index patch 等其余 patch 形态 | **未实现、也不宣称** | Missing by design |
| watcher / 自动重载 | 宿主职责 | Missing by design |

必须写明的边界有三条：

1. **没有独立的 `Include` 类**：`include` 是 [`ConfigComposer`](../../cordis-py/src/cordis/loader/composer.py) 的配置键，正式实现也没有这样一个类；
2. **`ConfigComposer` 是 Cordis-py 扩展**：[`docs/cordis-core-compatibility.md`](../../cordis-py/docs/cordis-core-compatibility.md) 明确把 Loader API 列为 Python 扩展，而 Include/Loader/HMR/Timer 包整体不在 Core 对照范围内，见 [`docs/compatibility.md`](../../cordis-py/docs/compatibility.md)；
3. **不虚构 DSH 等价**：本章的 overlay 只覆盖 id 定位 + 四字段白名单，比 DSH Include 的 patch 能力窄，这一点在文档和测试里都按"部分"记录。

## 本章刻意简化

- 只实现相对路径 include，不支持通配符、目录展开、URL 或环境变量选择文件；
- overlay 只有 `module`/`config`/`disabled`/`inject` 四个字段，没有 index patch、删除语义或条件表达式；
- `line` 在 adapter 未提供时为 `None`，与正式 parser 的"回退到数组下标"有意不同；
- `CompositeRuntime` 只是一个字典树，用来观察"树有没有被扰动"，不参与真实的 Fiber 生命周期（那是第 17 章与后续章节的职责）；
- 不实现 watcher：`ConfigSession.reload()` 由宿主显式调用。

## 检查理解

1. 如果 `ConfigSession.reload()` 先 `install` 再组合，哪一条断言会失败？为什么这种实现更危险？
2. 为什么快照断言用 `is` 而不是 `==`？两种写法分别能发现什么问题？
3. 本章只做了相对路径 include 与 id overlay。哪些 DSH Include 能力被明确排除，为什么不能默认宣称等价？
4. `CompositeRuntime.install()` 里"先 `_build` 再赋值 `self.roots`"保护了什么？如果直接在 `self.roots` 上追加会怎样？
