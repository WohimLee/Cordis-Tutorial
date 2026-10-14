# 查询和删除 Plugin

Registry 不只是创建 Fiber，还让 Runtime 能按 Plugin identity 检查当前挂载情况。

## Step 5：查询 Registry

本章实现这些公开操作：

- `get(plugin)`：返回 PluginRuntime；
- `has(plugin)`：判断 Plugin 是否注册；
- `size`：PluginRuntime 数量；
- `keys()`：规范化 callback；
- `values()`：PluginRuntime；
- `entries()`：callback 和 PluginRuntime；
- `forEach(callback)`：遍历每条记录。

运行：

```bash
uv run pytest Tutorial/04-plugin-registry/src/test_chapter04.py -k inspection
```

这些 API 是只读观察入口。它们不应该触发 Plugin callback 或改变 Fiber state。

## Step 6：删除一个 PluginRuntime

`delete(plugin)` 按 identity 找到 PluginRuntime，将它从 Registry 移除，并对它当前拥有的所有 Fiber 发出 dispose 请求：

```python
runtime = self._runtimes.pop(callback, None)
if runtime is None:
    return None
for fiber in tuple(runtime.fibers):
    fiber.dispose()
return runtime
```

`delete()` 立即返回被删除的 PluginRuntime。Fiber Cleanup 可能是异步的，因此调用者若要确认完成，需要等待对应 Fiber。

运行：

```bash
uv run pytest Tutorial/04-plugin-registry/src/test_chapter04.py -k delete
```

测试验证两个 Fiber 都被清理，而不是只删除第一条挂载记录。

## Step 7：关闭 root Context

`await root.aclose()` 先通过 root Fiber 清理整棵 Fiber 树，随后清空 Registry 的共享记录。最终：

```python
assert root.registry.size == 0
```

### Checkpoint C

```text
Context.plugin()
    ↓
Registry
├── 规范化 Plugin shape
├── 保持 Plugin identity
├── 保存 PluginRuntime
├── 为每次挂载创建 Fiber
└── 按 identity 查询和删除全部 Fiber
```

## 与正式源码对应

- Plugin metadata 与 Inject 类型：[`src/cordis/model.py`](../../cordis-py/src/cordis/model.py)
- Registry 与 PluginRuntime：[`src/cordis/registry.py`](../../cordis-py/src/cordis/registry.py)
- Fiber 使用 Runtime callback：[`src/cordis/fiber.py`](../../cordis-py/src/cordis/fiber.py)

正式实现还要读取 Plugin 的 `name`、`Config`、`inject`、`provide` 和 `intercept` metadata，并通过内部事件允许 Loader 关联 Entry。本章只处理 shape、identity 和多 Fiber 记录。

## 与 DSH Cordis 对照

DSH Cordis 同样把 function、constructor 和 `{ apply }` Plugin 规范化为内部 callback，并用 `Plugin.Runtime` 保存共享的 `name`、`callback`、`Config` 和 `fibers`。

Python 不能像 TypeScript 一样在 union type 下声明 `Plugin.Runtime` namespace，因此正式 Cordis-py 导出独立的 `PluginRuntime` class。名称结构不同，能力相同。

## 本章没有解决什么

下一章实现 **Reflect 与 Service implementation**。Registry 知道“哪些 Plugin 被挂载”，但仍不知道“它们提供了哪些 Service”。

Inject 会在 Reflect 建立后再加入，否则 Consumer 即使声明依赖，也没有地方查询 Service。

## 检查理解

1. 为什么一个 PluginRuntime 可以对应多个 Fiber？
2. 为什么 `resolve()` 必须缓存 callback identity？
3. 为什么 Context 不应该自己复制 Plugin normalization？
4. `delete()` 为什么要处理 Runtime 中的全部 Fiber？
