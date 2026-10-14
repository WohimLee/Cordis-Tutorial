# Resolver 与挂载：依赖顺序为什么不属于 Loader

声明层已经就绪。这一步把声明变成正在运行的 Fiber，并证明 Loader 没有偷偷变成第二个依赖调度器。

## Step 5：Resolver 只做一件事

Resolver 的输入是一个 specifier 和一个来源位置，输出是一个 plugin 对象：

```python
class ModuleResolver:
    def resolve(self, specifier: str, location: SourceLocation) -> Plugin:
        module, separator, attribute = specifier.partition(":")
        if not separator or not module or not attribute:
            raise LoaderError("plugin reference must use 'module:attribute' syntax", location)
        if self.allowed is not None and module not in self.allowed:
            raise LoaderSecurityError(f"plugin module {module!r} is not allowed", location)
        plugin = self.table.get(specifier)
        if plugin is None:
            raise LoaderError(f"cannot resolve plugin {specifier!r}", location)
        return plugin
```

三条规则都带位置：

```text
plugin reference must use 'module:attribute' syntax   → cordis.toml:4:3 附近的位置
plugin module 'app' is not allowed                     → LoaderSecurityError
cannot resolve plugin 'test:missing'                   → 找不到
```

教学版用显式映射表，正式 [`ModuleResolver`](../../cordis-py/src/cordis/loader/resolver.py) 用 `importlib` 加上 `allowed_packages` / `allowed_paths`：

| 能力 | 教学版 | 正式版 |
| --- | --- | --- |
| specifier 解析 | `table[specifier]` | `importlib` + `module:attribute` 逐段 `getattr` |
| 允许策略 | `allowed` 模块名集合 | `allowed_packages` / `allowed_paths` / 自定义 `ModulePolicy` |
| 相对模块 | 不支持 | 相对于配置文件目录解析 `.py` |
| 失败类型 | `LoaderError` / `LoaderSecurityError` | 同上 |

策略放在 resolver 而不是 parser 不是随意安排：**import 就是代码执行**，它需要在挂载之前、在知道具体 plugin 之前就被拦下。这正是 [`docs/architecture/07-configuration-loader.md`](../../cordis-py/docs/architecture/07-configuration-loader.md) 里"插件 import 是代码执行，应用必须授权模块来源"的实现位置。

```bash
uv run pytest Tutorial/17-loader/src/test_chapter17.py -k resolver
```

## Step 6：Loader 把声明变成 Fiber

`mount_entry()` 是唯一的入口：

```python
async def mount_entry(self, parsed, context, parent) -> Entry:
    if parsed.id in self.entries:
        raise LoaderError(f"duplicate runtime entry id {parsed.id!r}", parsed.location)
    entry = Entry(parsed=parsed, parent=parent, context=context)
    self.entries[parsed.id] = entry
    if parent is None:
        self.roots.append(entry)
    else:
        parent.children.append(entry)
    if parsed.disabled:
        return entry
    plugin = self._prepared.pop(parsed.id, None)
    if plugin is None:
        plugin = self.resolver.resolve(parsed.module, parsed.location)
    entry.plugin = plugin
    wrapper = EntryPlugin(self, entry, plugin)
    entry.fiber = context.plugin(wrapper, parsed.config, name=parsed.id, inject=wrapper.inject)
    await entry.fiber.wait()
    return entry
```

三个决定值得解释：

1. **`disabled` 条目仍然建 `Entry`**，只是不挂 Fiber。这样它保留稳定 id 和来源，将来重新启用时身份不变；同时它下面的子条目也不会被挂载。
2. **每个条目包一层 `EntryPlugin`**：真实插件不认识 Loader，声明的 `inject`、子条目和返回值所有权都由包装层补齐。
3. **`await entry.fiber.wait()`** 让"挂载"这个词有确定含义：返回时该 Fiber 已经稳定，激活错误已经抛出。

包装层的 `apply()` 承担真正的工作：

```python
async def apply(self, context, config) -> None:
    result = _plugin_callback(self.plugin)(context, config)
    if inspect.isawaitable(result):
        result = await result
    if result is not None:
        await context.effect(lambda: result, f"loader.plugin({self.entry.id!r})")
    for parsed_child in self.entry.parsed.children:
        await self.loader.mount_entry(parsed_child, context, self.entry)
```

重点是最后两行：**子条目在父插件激活期间、用父插件的 Context 挂载**。因为教学 Core 的 `Context.plugin()` 把新 Fiber 挂到当前 Fiber 名下（与正式 Cordis 的 Effect 所有权一致），所以：

```text
父条目 dispose → 先 dispose 子 Fiber → 再跑父插件的 cleanup
```

级联清理不需要 Loader 写第二套逻辑，它只是复用了 Fiber 的所有权。

## Step 7：配置顺序不等于激活顺序

把 consumer 写在 provider 前面：

```toml
[[plugins]]
id = "consumer"
module = "test:consumer"
inject.value = {}

[[plugins]]
id = "provider"
module = "test:provider"
```

`mount()` 返回后：

```python
assert [entry.id for entry in roots] == ["consumer", "provider"]   # 配置顺序被保留
assert trace == ["provider:start:one", "consumer:start:value:one"]  # 激活顺序由依赖决定
```

第一条断言说明 Loader **没有重排** roots；第二条说明 consumer 先进入 `PENDING`，等 provider 通过 `provide()` 发布服务后，教学 Core 的 `Scope.request()` 唤醒所有 Fiber，consumer 的 `_dependencies_ready()` 变为真，才发生第一次激活。

教学 Core 只保留这一步需要的最小机制，其余留给对应章节：

| 机制 | 教学版（本章） | 完整章节 |
| --- | --- | --- |
| Fiber 状态 | `PENDING/LOADING/ACTIVE/UNLOADING/FAILED/DISPOSED` | 09 |
| 服务发布 | `Context.provide` + `Scope.services` | 05、13 |
| 依赖判定 | `inject` 里的名字是否都已提供 | 06 |
| epoch | 每次卸载/重载都走 `_release()` + `_load()` | 07 |
| 失败状态 | `FAILED` 稳定，只有 `update()` 会重新尝试 | 09 |

服务消失也会走同一条路：provider 被 dispose 时它的 `provide` Effect 移除服务并唤醒 consumer，consumer 卸载後回到 `PENDING`——不需要 Loader 参与。

```bash
uv run pytest Tutorial/17-loader/src/test_chapter17.py -k pending_consumer
```

### Checkpoint B

此时可以观察到的契约：

```text
配置顺序   = roots 顺序
激活顺序   = 依赖满足顺序
consumer   = PENDING → ACTIVE → PENDING（provider 消失后）
Loader     = 只创建 Fiber 与等待稳定，不排序、不调度
```

## Step 8：挂载失败必须留下干净的树

子条目激活失败时，已经激活的父条目不能留在运行态：

```python
tree = (parsed("parent", "test:parent", children=(parsed("broken", "test:broken"),)),)
with pytest.raises(RuntimeError, match="broken child"):
    await loader.mount(tree)
```

`mount()` 的处理是"先清理再抛出"：

```python
try:
    await self._mount_forest(parsed_entries)
except BaseException:
    await self._dispose_forest()
    raise
```

失败路径的断言必须同时检查异常和最终状态：

| 检查 | 期望 |
| --- | --- |
| 异常 | `RuntimeError("broken child")` 原样抛给调用者 |
| 父 Fiber | `DISPOSED` |
| 子 Fiber | `DISPOSED` |
| trace | `["parent:start", "parent:stop"]`（子插件从未成功启动） |
| 再次 `close()` | 不产生任何额外 cleanup |

只检查"抛了什么异常"是不够的：一个抛了异常但留下 ACTIVE 父 Fiber 的实现，会在下一次更新时表现出幽灵条目。

```bash
uv run pytest Tutorial/17-loader/src/test_chapter17.py -k mount_failure
```

### Checkpoint C

挂载现在是全有或全无的：成功时整棵树稳定，失败时没有任何残留。

## 与正式源码对应

- 解析策略与 `module:attribute`：[`src/cordis/loader/resolver.py`](../../cordis-py/src/cordis/loader/resolver.py)
- 每条目包装与子条目挂载：[`src/cordis/loader/runtime.py`](../../cordis-py/src/cordis/loader/runtime.py) 的 `_EntryPlugin`
- 挂载与依赖顺序（不排序的正式实现）：[`tests/test_loader_runtime.py`](../../cordis-py/tests/test_loader_runtime.py) 的 `test_loader_mounts_without_topological_sorting`
- 子条目所有权：[`tests/test_loader_runtime.py`](../../cordis-py/tests/test_loader_runtime.py) 的 `test_children_are_owned_by_parent_fiber`
- Fiber 与服务依赖的完整语义：[`src/cordis/fiber.py`](../../cordis-py/src/cordis/fiber.py)、[`src/cordis/context.py`](../../cordis-py/src/cordis/context.py)

## 检查理解

1. 如果 `EntryPlugin.apply()` 在激活返回**之后**才挂载子条目，父子所有权会怎么变？
2. 为什么 `mount()` 要在 `except` 里先 `_dispose_forest()` 再 `raise`，而不是让调用者自己清理？
3. 教学版把 resolver 的 `allowed` 检查放在查表之前，这个顺序为什么重要？
4. 若把 `inject.value = {}` 从 consumer 的配置里删掉，trace 会变成什么？这说明了 Loader 的哪条职责？
