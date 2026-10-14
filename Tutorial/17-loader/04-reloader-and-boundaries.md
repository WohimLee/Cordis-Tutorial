# Reloader、整章回归与边界

## Step 12：Reloader 只是宿主与 Loader 之间的适配器

重载逻辑里唯一"不知道"的东西是"什么时候该重载"。那是宿主的职责，所以 `ConfigReloader` 只做三件事：

```python
async def reload(self) -> bool:
    if self.current is None:
        raise RuntimeError("config reloader has not been started")
    candidate = self.read()                 # 重新读取并解析
    if candidate == self.current:
        return False                        # 内容未变，不触碰运行态
    await self.loader.update(candidate)     # 交给 Loader 的事务
    self.current = candidate
    return True
```

它不创建 watcher、不轮询、不导入模块：

| 职责 | 归属 |
| --- | --- |
| 检测文件变化 | 宿主 |
| 刷新 Python 模块缓存 | 宿主 |
| 重新读取 + 解析 | `ConfigReloader.read()` |
| 判定 config-only / structural | `Loader.update()` |
| 事务与回滚 | `Loader` |

这也是正式 [`ConfigReloader`](../../cordis-py/src/cordis/loader/reloader.py) 的位置：它接收一个 composer 产出声明，再把结果交给 `Loader`。教学版把 composer 换成一个可注入的 `reader`，第 18 章会用真正的组合器替换它。

快照 `self.current` 的更新发生在 `update()` **成功之后**，所以失败的重载不会污染快照：

```python
assert reloader.current is previous
assert loader.entries["a"].fiber.config == 1
```

```bash
uv run pytest Tutorial/17-loader/src/test_chapter17.py -k reloader
```

### Checkpoint F

完整的变更闭环：

```text
宿主检测 → reloader 读取 → Loader 分类
        → config-only 原地更新 / structural 替换
        → 成功更新快照；失败保留旧快照与旧运行树
```

## Step 13：整章一起跑

单条测试都通过之后，把这一章的十条测试连起来跑一遍：

```bash
uv run pytest Tutorial/17-loader/src/test_chapter17.py
```

它们覆盖的契约是：

| 测试 | 覆盖的契约 |
| --- | --- |
| `resolver` | specifier 语法、允许策略、带位置的错误 |
| `toml_parser` | 嵌套声明、不可变 `inject`、`${NAME}`、重复 id |
| `mount_order` | 配置顺序保留，激活顺序由依赖决定 |
| `pending_consumer` | `PENDING → ACTIVE → PENDING` |
| `children` | 子条目所有权、级联清理、`disabled` 条目不挂载 |
| `reloader`（含结构变更） | config-only 复用 Fiber，structural 替换整棵树 |
| `failed_config_update` | 部分失败回滚 + 快照不被污染 |
| `failed_replacement` | 失败后还原上一棵有效树 |
| `rejections` | 预检失败不动旧树、重复挂载被拒绝 |
| `mount_failure` | 失败挂载无残留、`close()` 幂等 |

### Checkpoint G

这一章的能力边界可以一句话概括：

```text
配置 → 声明 → Fiber 树 → 配置更新/结构替换 → 失败回滚
```

它仍然**不包含**：include 组合、文件监视、模块缓存刷新、Timer、HMR。下一章补上第一个。

## 与正式源码对应

- Loader 全部导出与测试：[`src/cordis/loader/__init__.py`](../../cordis-py/src/cordis/loader/__init__.py)、[`tests/test_loader_runtime.py`](../../cordis-py/tests/test_loader_runtime.py)
- 职责分层说明：[`docs/architecture/07-configuration-loader.md`](../../cordis-py/docs/architecture/07-configuration-loader.md)
- HMR 与模块缓存为什么属于宿主：[`docs/architecture/07-configuration-loader.md`](../../cordis-py/docs/architecture/07-configuration-loader.md) 的 "HMR" 一节
- 配置更新与重载测试：[`tests/test_loader_reloader.py`](../../cordis-py/tests/test_loader_reloader.py)
- Python Loader 的扩展定位：[`docs/cordis-core-compatibility.md`](../../cordis-py/docs/cordis-core-compatibility.md) 的 "Cordis-py extensions"

## 与 DSH Cordis 对照与扩展边界

下表只记录"当前 Python Loader 的行为契约对应到 DSH Loader 的哪个概念"，**不是**兼容矩阵，也不使用 `Exact`/`Equivalent`/`Missing` 等 Core 矩阵分类词。

| DSH Loader 概念 | 当前 Python 契约 | 非兼容性措辞 |
| --- | --- | --- |
| 声明式 Entry 树 | `ParsedEntry.children` | 结构对应：扩展内部的树表示 |
| 稳定 id 与更新判定 | `Entry.id` + `_identity()` | 结构对应：本节测试覆盖的行为契约 |
| config-only update | `Fiber.update(config)` | 结构对应：本节测试覆盖的行为契约 |
| structural replacement | `Loader.replace()` | 结构对应：本节测试覆盖的行为契约 |
| 失败回滚 | `previous` 声明重新挂载 | 结构对应：本节测试覆盖的行为契约 |
| watcher / 模块缓存 HMR | 不实现 | 未实现或宿主责任：由宿主刷新 import 后显式调用 |
| `EntryGroup` / `EntryTree` / `ModuleLoader` | 正式 Python 实现中不存在这些类 | 不作映射：不作为对照目标 |

必须重复第 01 步的边界：上表不是兼容矩阵。Loader **不在** [`docs/cordis-core-compatibility.md`](../../cordis-py/docs/cordis-core-compatibility.md) 的对照范围内，它是被明确记录的 Cordis-py 扩展（"explicit Loader APIs under `cordis.loader`"），`Exact`/`Equivalent`/`Partial`/`Missing`/`Language-specific`/`Extension` 这套分类只适用于该文档中的 Core 条目。本章证明的是"当前 Python Loader 的行为契约"，不是"DSH Loader 1.0.0-rc.5 已经等价"；逐条等价证据仍待独立的 API inventory 与双语言行为场景。

## 本章刻意简化

- 教学 Core 只保留 `PENDING/LOADING/ACTIVE/UNLOADING/FAILED/DISPOSED` 的状态与依赖唤醒，隔离（isolation）、intercept、Logger 都不参与 Loader；
- Resolver 用显式映射表替代 `importlib`，因此不能加载真实模块文件；
- `ConfigReloader` 接受可注入 `reader`，把组合职责留给第 18 章；
- 不实现 `Loader` 的 `closed` 之后重开、多棵树并存等未在正式契约中出现的形态。

## 检查理解

1. 为什么 Loader 必须"只创建 Fiber"，而不能自己按 `inject` 排序后依次挂载？
2. `EntryPlugin.apply()` 里先 `context.effect(...)` 再挂子条目，这个顺序与清理顺序是什么关系？
3. 预检（`_preflight`）只做解析。为什么它已经足以保证"拼错的 module 不会打断在跑的树"？
4. 如果某个条目的 cleanup 抛异常，`_dispose_forest` 与 `_release` 分别怎么保证"继续清理其余资源"？
