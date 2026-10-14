# 21 — 更新、替换、空窗与恢复

上一节的整栈已经在稳态工作。接下来问一个更苛刻的问题：

```text
运行中的 provider 被换掉时，请求会看到什么？
候选坏了会怎样？previous generation 还在吗？
恢复合法更新后，旧 epoch 的资源是否只清理一次？
```

这一节按"config 变化 → 模块变化 → 空窗 → 失败 → 恢复"的顺序推进，每一步都只增加一个判定。

## Step 7：先分类，再决定事务

HMR 收到的批次只有两种走向：**仅配置变化**走原地 update，**模块或结构变化**走 replacement。判定标准不是文本 diff，而是 entry identity：

```python
@staticmethod
def _identity(entry: ParsedEntry) -> tuple[object, ...]:
    return (
        entry.module,                        # 换了实现文件 -> 结构变化
        entry.disabled,
        dict(entry.inject),
        tuple(child.id for child in entry.children),
    )
```

`Loader.update()` 先比较 id 集合与 identity；只要有一个不同就升级为 `replace()`。否则只对 `config` 不同的条目调用 `entry.fiber.update(config)`：

```text
provider.yaml: preamble alpha -> alpha-2
  id 集合相同、identity 相同、只有 config 不同
  -> fiber.update() 原地换 epoch，不新建 Fiber，不重建整棵树
```

这里有一个必须显式验证的语义：provider 的新 epoch 会产生**新的服务对象**，因此 consumer 必须在服务消失与重新出现之间卸载重载，否则它会继续持有旧对象。`Fiber.update()` 通过"先 `_release()`，再 `stabilize()`，最后 `load()`"保证这一点（见 [01](01-boot-the-stack.md) 的 Step 1）。测试同时断言：

```python
assert entry.fiber.uid == uid            # 还是同一个 Fiber
assert entry.fiber.version == 1          # 但进入新 epoch
assert harness.trace.count("cleanup:provider") == 1
assert harness.context.get("agent").provider.preamble == "alpha-2"   # consumer 看到新服务
```

### Checkpoint C：config-only 更新

```bash
cd Tutorial/21-mini-harness/src
PYTHONDONTWRITEBYTECODE=1 /tmp/cordis-tutorial-venv/bin/python -m pytest -p no:cacheprovider -q -k config_change
```

预期：`reloader.config_updates == 1`、`reloader.replacements == 0`、`loader.replacements == 0`，请求返回 `v1:alpha-2:ping`，heartbeat 句柄数量仍然是 1。

## Step 8：模块变化走 candidate replacement

模块变化不能靠修改对象，它必须换一整个候选声明树。`replace_module()` 只做一件事：按 entry id 把某个条目的 `module`（以及可选的 `config`）替换掉，其它条目原样保留：

```python
def replace_module(entries, entry_id, specifier, config=None, *, has_config=False):
    def visit(entry):
        if entry.id == entry_id:
            return replace(entry, module=specifier,
                           config=config if has_config else entry.config)
        return replace(entry, children=tuple(visit(child) for child in entry.children))
    ...
```

然后交给 `Loader.replace()` 的事务：

```python
self._prepared = self._preflight(entries)     # 先把候选全部解析，失败就完全不动现树
previous = tuple(entry.parsed for entry in self.roots)
try:
    await self._dispose_forest()              # 撤销旧 generation
    self._reset_forest()
    try:
        await self._mount_forest(entries)     # 挂载候选
    except BaseException as error:
        self._prepared.clear()
        await self._dispose_forest()          # 候选自己的部分资源
        self._reset_forest()
        await self._mount_forest(previous)    # 恢复旧 generation
        raise HarnessError(f"replacement rejected: {error}", cause=error) from error
finally:
    self._prepared.clear()
```

两点顺序很重要：

1. **preflight 在撤销旧树之前**。specifier 无法解析（文件不存在、入口不是 callable）时，候选根本不会碰运行中的树。
2. **候选失败后重新挂载 previous**。回滚不是"什么都不做"，而是把上一棵声明树重新挂载成新 generation；服务的旧实例已经随着 dispose 消失，因此 consumer 会走一次完整的卸载/重载。

测试把"旧 epoch 失效"写成可数事实：

```python
assert harness.trace.count("cleanup:provider") == 1     # v1 只清理一次
assert harness.timer.pending == 1                       # 旧 interval 撤销，新 interval 建立
assert harness.events.count() == 1                      # listener 没有翻倍
harness.clock.advance(0.5)
assert new_agent.ticks == 1 and old_agent.ticks == 1    # 旧实例冻结，新实例在跳
```

`old_agent.ticks` 停在上一次的值，是因为旧 consumer 的 listener、interval 与整个 EffectScope 都已经撤销。

## Step 9：替换窗口内的 `PENDING` 是契约的一部分

整树 replacement 天然存在一个空窗：旧 provider 已 dispose、新 provider 还没 provide。gateway 的 `request()` 在 provider 缺席时返回 `PENDING`，所以这个空窗是可观察、可等待的，而不是一个未定义状态。

为了确定性地停在窗口正中，本章提供一个测试可控的 latch：

```python
class ActivationGate:
    async def wait(self) -> None:
        if self._open:
            return
        self.arrived.set()          # 告诉测试"候选已经走到这里"
        await self._released.wait() # 直到测试放行
```

provider 插件在 provide 之前 `await gate.wait()`。测试流程完全由 Event 控制，没有 sleep：

```python
harness.gate.close()
harness.source.publish(module_event("provider_v2:plugin", preamble="beta"))
harness.clock.advance(harness.reloader.debounce)

applying = asyncio.create_task(harness.reloader.flush())
await harness.gate.arrived.wait()          # 候选已经进入激活阶段
assert harness.request("ping") == "PENDING" # 空窗内的真实回答

harness.gate.release()
assert await applying == 1
assert harness.request("ping") == "v2:beta:PING"
```

### Checkpoint D：空窗语义

```bash
PYTHONDONTWRITEBYTECODE=1 /tmp/cordis-tutorial-venv/bin/python -m pytest -p no:cacheprovider -q -k pending
```

预期：窗口内 `PENDING`，放行后 `v2:beta:PING`，且 `harness.context.get("agent")` 已经用新 provider 重新激活。

> **教学契约的诚实说明**：本章对模块变化采用**整树 replacement**（与第 17 章一致），所以窗口内所有请求都会短暂 `PENDING`。这不是"无损热切换"。DSH HMR 的逐 Fiber / 逐模块替换不在本章范围内，也没有被这里的行为证明。

## Step 10：坏候选与恢复

坏候选有两种，必须分别处理：

| 失败点 | 例子 | 现树是否被触碰 | 结果 |
| --- | --- | --- | --- |
| 解析阶段 | `provider_missing:plugin`（文件不存在） | 否（preflight 先执行） | `reloader.rejected += 1`，请求仍是 v1 |
| 激活阶段 | `provider_v2:plugin` + `preamble: 42`（`Config` 校验失败） | 是，但随后回滚 | 旧树重新挂载，请求仍是 v1 |

激活阶段的失败会沿着 `Fiber.load → Context.plugin → Loader.mount_entry → _mount_forest` 一路抛出，最终被 `replace()` 捕获并进入恢复分支。测试同时检查"错误被上报"和"系统状态正确"：

```python
assert harness.reloader.rejected == 2
assert harness.loader.replacements == 0        # 没有一次替换被算作成功
assert harness.request("ping") == "v1:alpha:ping"
assert harness.timer.pending == 1              # 回滚后不留重复句柄

# 合法恢复
harness.source.publish(module_event("provider_v2:plugin", preamble="beta"))
harness.clock.advance(harness.reloader.debounce)
assert await harness.reloader.flush() == 1
assert harness.loader.replacements == 1
assert harness.request("ping") == "v2:beta:PING"
```

`reloader.rejected` 与 `loader.replacements` 分开计数，是为了区分"宿主提交了一次变化"和"运行树真的换了一代"。

### Checkpoint E：故障与恢复

```bash
PYTHONDONTWRITEBYTECODE=1 /tmp/cordis-tutorial-venv/bin/python -m pytest -p no:cacheprovider -q -k "invalid_candidate or module_change"
```

随后运行整章：

```bash
PYTHONDONTWRITEBYTECODE=1 /tmp/cordis-tutorial-venv/bin/python -m pytest -p no:cacheprovider -q
```

八条整栈测试覆盖六个阶段：

| 阶段 | 测试 | 证明什么 |
| --- | --- | --- |
| 启动 | `test_cold_start_mounts_the_tree_without_manual_ordering` | 无序声明、依赖激活、specifier 指向真实文件 |
| 稳态 | `test_manual_clock_drives_the_heartbeat` | 虚拟时间驱动 heartbeat，无真实时间 |
| 更新 | `test_config_change_updates_the_epoch_in_place` | config-only 原地换 epoch，consumer 看到新服务 |
| 更新 | `test_burst_of_changes_is_merged_into_one_batch` | debounce 合并 burst，只处理一次 |
| 更新 | `test_module_change_replaces_the_generation` | 结构替换、旧 epoch 资源只清理一次 |
| 更新 | `test_request_is_pending_while_the_candidate_activates` | 空窗内 `PENDING`，放行后恢复 |
| 故障/恢复 | `test_invalid_candidate_keeps_the_previous_generation` | 解析失败与激活失败都保留旧 generation，随后合法恢复 |
| 关闭 | `test_shutdown_zeroes_every_counter_and_reports_cleanup_errors` | 关闭归零、清理错误聚合、幂等 |

下一节 [03-shutdown-and-boundaries.md](03-shutdown-and-boundaries.md) 收尾：关闭后到底哪些计数器必须为 0、清理失败如何上报，以及整章与正式实现和 DSH 的边界。
