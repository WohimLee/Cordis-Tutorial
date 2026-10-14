# 更新与替换：两类变更、事务与回滚

挂载只处理"第一次"。配置会变：一个 URL 改了，或者整个模块换了。这两件事的代价完全不同，把它们混在一个路径里，就会出现"改一个 URL 导致整棵树重建"或"换了模块却什么都没发生"。

## Step 9：先判定变更类型

`update()` 的第一步不是执行，而是分类。判定依据是**身份元组**：

```python
@staticmethod
def _identity(entry: ParsedEntry) -> tuple[object, ...]:
    return (entry.module, entry.disabled, dict(entry.inject), tuple(child.id for child in entry.children))
```

| 比较结果 | 变更类型 | 走哪条路 |
| --- | --- | --- |
| id 集合相同，且每条身份相同 | config-only | 逐条 `fiber.update(config)` |
| 任意一条 id 或身份不同 | structural | 整棵树 `replace()` |

`config` 被**故意排除**在身份之外：它正是 config-only 路径要处理的东西。反过来，`inject` 参与身份判定，因为依赖变化会改变 Fiber 的激活条件，不能靠原地改配置解决。

还有一条容易被忽略的规则：

```python
if any(candidate.children for _, _, candidate in changes):
    raise LoaderError("config update for an entry with children requires replacement")
```

带子条目的条目即使只是 config 变化也走 replacement。理由是所有权：子 Fiber 的创建发生在父激活过程中，原地刷新父配置而不重建子 Fiber，会让"父声明的子集合"和"实际挂载的子 Fiber"失去对应关系。正式实现给出同样的约束。

## Step 10：config-only 更新是一个事务

先看单条 Fiber 上的语义：

```python
async def update(self, config: object) -> None:
    async with self._lock:
        if self.state is FiberState.DISPOSED:
            raise InactiveEffectError("cannot update a disposed fiber")
        validated = self._validated(config)      # ① 校验
        if not self._dependencies_ready():
            self.config = validated
            return
        if self.state is not FiberState.PENDING:
            await self._release()                # ② 卸载旧 epoch
        self.config = validated
        await self._load(validated)              # ③ 激活新 epoch
        if self.state is FiberState.FAILED:
            assert self.error is not None
            raise self.error
        self.version += 1
```

顺序很关键：**校验在下发之前**。合法配置失败不可能发生；非法配置则在旧 epoch 还没被卸载时就抛出，所以消费者看到的仍是原来那棵树。这正是归档场景 [`015-fiber-invalid-update`](../../cordis-py/tests/compat/scenarios/015-fiber-invalid-update.json) 观察到的时序。

Loader 在这一层之上加事务：

```python
for entry, previous, candidate in changes:
    await entry.fiber.update(candidate.config)
    entry.parsed = candidate
    entry.version += 1
    applied.append((entry, previous))
```

只要有一条失败，已经应用的部分要按相反顺序回滚：

```python
for entry, previous in reversed(applied):
    await entry.fiber.update(previous.config)
    entry.parsed = previous
    entry.version += 1
```

于是失败留下的轨迹是可以逐项断言的：

```text
a:1, b:1                    初始
a:stop:1, a:2               a 更新成功
[b 的非法配置在下发前被拒绝]
a:stop:2, a:1               a 回滚
```

```python
assert trace == ["a:1", "b:1", "a:stop:1", "a:2", "a:stop:2", "a:1"]
assert loader.entries["a"].version == 2      # 一次应用 + 一次回滚
assert loader.entries["b"].error is not None # 失败的条目被记录
```

注意 `b` 从头到尾都是 `b:1`：非法配置没有让它卸载。如果回滚实现成"重新挂载整棵树"，`b` 也会经历一次 stop/start，trace 立刻不同。

```bash
uv run pytest Tutorial/17-loader/src/test_chapter17.py -k roll
```

### Checkpoint D

config-only 更新满足：

```text
成功：fiber 身份不变，config 变，version + 1
失败：所有已更新条目回到旧 config，未更新条目完全没被触碰
```

## Step 11：structural 变更与预检

replacement 的难点是"中间态"：旧树已经销毁，新树还没建好。教学实现把顺序设计成：

```python
self._prepared = self._preflight(parsed_entries)   # ① 只做解析，不碰运行态
previous = tuple(entry.parsed for entry in self.roots)
await self._dispose_forest()                       # ② 销毁旧树
self._reset_forest()
try:
    await self._mount_forest(parsed_entries)       # ③ 挂载新树
except BaseException as error:
    ...
    await self._mount_forest(previous)             # ④ 失败则还原旧声明
```

① 的预检很关键。如果 module 根本解析不了（拼错、被策略拒绝），这一步就抛错，**旧树连动都没动**：

```python
with pytest.raises(LoaderError, match="cannot resolve plugin"):
    await loader.update((parsed("a", "test:missing", config=1),))
assert loader.entries["a"].fiber is original
assert trace == ["a:1"]        # 没有任何 cleanup 发生
```

预检通过后如果**激活**失败，就必须走 ④ 还原。教学 Core 里一个激活即失败的插件足以触发：

```python
with pytest.raises(LoaderError, match="failed to replace entry tree"):
    await loader.update((parsed("a", "test:broken"),))

assert old_fiber.state is FiberState.DISPOSED      # 失败的那棵树被清掉
assert restored.parsed.module == "test:cfg_a"
assert restored.fiber.config == 1                  # 上一棵有效树重新激活
```

两种失败的区别要在文档和测试里都说清楚：

| 失败位置 | 旧树 | 结果 |
| --- | --- | --- |
| 预检（解析/策略） | 完全未触碰 | 原样抛 `LoaderError` |
| 挂载（激活/清理） | 先销毁，再还原 | `LoaderError`，`previous` 声明重新挂载 |

还原本身也可能失败。这时不能吞掉任何一个错误，教学实现用 `BaseExceptionGroup` 聚合：

```python
cause = BaseExceptionGroup("loader replacement and rollback failed", [error, rollback_error])
raise LoaderError("failed to replace entry tree", cause=cause) from cause
```

```bash
uv run pytest Tutorial/17-loader/src/test_chapter17.py -k replacement
```

### Checkpoint E

到这里，两类变更都有明确契约：

```text
config-only：原地更新 + 部分失败回滚 + 非法配置不下发
structural ：预检 → 销毁 → 挂载 → 失败还原
```
