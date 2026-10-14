# 21 — 关闭验证、整章回归与边界

整栈已经能启动、运行、更新、失败和恢复。最后一个问题是：

```text
关闭之后，刚才那些资源真的都释放了吗？失败会不会让某个计数器留下脏值？
```

这个问题只能靠"可数的快照"回答，不能靠感觉。

## Step 11：把"关闭"定义成一组必须归零的计数器

`MiniHarness.snapshot()` 是本章唯一的收尾断言：

```python
def snapshot(self) -> dict[str, int]:
    fibers = self.context._scope.fibers
    return {
        "services": len(self.context._scope.services),
        "fibers": len(fibers),
        "entries": len(self.loader.entries),
        "listeners": self.events.count(),
        "timer_handles": self.timer.pending,
        "watcher_subscriptions": self.source.subscription_count,
        "effects": len(self.context.fiber.effects)
        + sum(len(fiber.effects) for fiber in fibers),
        "pending_events": len(self.reloader.pending_events),
    }
```

每一项都对应一类所有权：

| 计数器 | 归谁所有 | 归零意味着 |
| --- | --- | --- |
| `services` | 各 fiber 的 `provide` disposer + 根 Context | 所有服务名都撤销，没有"幽灵 provider" |
| `fibers` | `Scope.fibers` 注册表 | 每个 Fiber 都走过 `dispose()` 并注销 |
| `entries` | `Loader.entries` | 声明树不再被运行时引用 |
| `listeners` | 各 fiber 的 `on()` disposer | 事件订阅全部撤销，没有重复 listener |
| `timer_handles` | 各 fiber 的 `EffectScope` | heartbeat、debounce 等时钟条目全部取消 |
| `watcher_subscriptions` | `ChangeSource` | 宿主 feed 上不再挂着运行时处理器 |
| `effects` | 根 fiber + 所有活动 fiber | 清理栈为空，没有遗留 cleanup |
| `pending_events` | `HotReloader` | 没有未处理的变更积压 |

关闭顺序必须是依赖的反向：

```python
async def close(self) -> None:
    if self._closed:
        return
    self._closed = True
    await self.reloader.close()   # 先退订宿主 feed、撤销 debounce
    await self.loader.close()     # 再按 fiber 所有权 dispose 整棵树
    await self.context.aclose()   # 最后释放根 Context 自己的服务
```

测试在看到稳态时的非零值之后，断言关闭后全部为 0，并且第二次 `close()` 幂等：

```python
live = harness.snapshot()
assert live["services"] == 8      # 4 个 harness 服务 + 4 个插件服务
assert live["fibers"] == 4 and live["entries"] == 4
assert live["listeners"] == 1 and live["timer_handles"] == 1
assert live["watcher_subscriptions"] == 1

await harness.close()
assert set(harness.snapshot().values()) == {0}
assert harness.request("ping") == "PENDING"
await harness.close()             # 幂等
```

`harness.request()` 在关闭后返回 `PENDING` 而不是抛异常：请求入口本来就要表达"现在没有可用路由"，关闭只是其中一种原因。

## Step 12：清理失败要聚合上报，而不是中断或吞掉

关闭过程中如果某个 cleanup 抛异常，正确行为既不是"立刻放弃其余清理"，也不是"悄悄忽略"：

```python
async def _dispose_forest(self) -> None:
    for entry in reversed(self.roots):
        if entry.fiber is None:
            continue
        try:
            await entry.fiber.dispose()
        except BaseException as error:
            self.cleanup_errors.append(error)    # 记录，继续释放其它根
```

Fiber 内部同样是"先跑完，再聚合"：

```text
plugin cleanup 抛错
  -> Context.effect 的 release 收集 -> BaseExceptionGroup("plugin(..) cleanup failed")
  -> EffectScope.close 继续逆序清理其余 disposer -> BaseExceptionGroup("effect cleanup failed")
  -> Fiber.dispose 聚合子树 -> BaseExceptionGroup("fiber 'gateway' cleanup failed")
  -> Loader._dispose_forest 记录到 cleanup_errors
```

测试用一个真实会失败的插件（配置里加 `cleanup_error: true`）验证三件事：

```python
assert len(failing.loader.cleanup_errors) == 1
assert isinstance(failing.loader.cleanup_errors[0], BaseExceptionGroup)
assert "gateway cleanup failed" in repr(failing.loader.cleanup_errors[0])
assert failing.trace.count("cleanup:heartbeat") == 1     # 其它 cleanup 仍然执行
assert set(failing.snapshot().values()) == {0}           # 仍然归零
```

### Checkpoint F：关闭是幂等的，失败也不留脏值

```bash
cd Tutorial/21-mini-harness/src
PYTHONDONTWRITEBYTECODE=1 /tmp/cordis-tutorial-venv/bin/python -m pytest -p no:cacheprovider -q -k shutdown
```

> **教学契约**：本章选择"清理错误写入 `loader.cleanup_errors`，不向 `close()` 抛出"。理由是抛出无法表达"部分清理成功"，也会中断后续的关闭步骤；正式实现把清理错误交给各自的 owner 与 Logger，而不是让一个聚合异常代表整个关闭过程。这是一个明确的简化，不影响"必须归零"这一硬约束。

## Step 13：整章一起跑

```bash
cd Tutorial/21-mini-harness/src
PYTHONDONTWRITEBYTECODE=1 /tmp/cordis-tutorial-venv/bin/python -m pytest -p no:cacheprovider -q
```

还可以直接跑一次完整剧本（启动 → 心跳 → 模块替换 → 关闭）：

```bash
PYTHONDONTWRITEBYTECODE=1 /tmp/cordis-tutorial-venv/bin/python run.py
```

输出形如：

```text
cold start request: v1:alpha:hello
active entries: ['gateway', 'heartbeat', 'consumer', 'provider']
heartbeats after 1.0 virtual seconds: 2 2
after module replacement: v2:demo:HELLO
replacements: 1
shutdown snapshot: {'services': 0, 'fibers': 0, 'entries': 0, 'listeners': 0,
                    'timer_handles': 0, 'watcher_subscriptions': 0, 'effects': 0,
                    'pending_events': 0}
```

八条整栈场景合起来覆盖了完成标准里的六阶段：**启动、稳态、更新、故障、恢复、关闭**。

## 正式源码对应

本章是整栈教学，每个机制都能在正式 [`src/cordis`](../../cordis-py/src/cordis) 中找到对应物：

| 教学概念 | 正式实现 | 差异 |
| --- | --- | --- |
| `Context` 派生与作用域 | [`context.py`](../../cordis-py/src/cordis/context.py) | 正式版有 filter、accessor、Service 绑定；本章只保留 get/has/provide/effect/on/plugin |
| `EffectScope` / `Disposer` | [`effect.py`](../../cordis-py/src/cordis/effect.py) | 本章保留逆序、幂等、`BaseExceptionGroup`；省略元数据与 interceptor |
| `Fiber` / epoch | [`fiber.py`](../../cordis-py/src/cordis/fiber.py) | 正式版并发安全、有 LOADING/FAILED 细节与 `await fiber`；本章为顺序 await |
| 服务注册与依赖通知 | [`registry.py`](../../cordis-py/src/cordis/registry.py)、[`reflect.py`](../../cordis-py/src/cordis/reflect.py) | 本章用 `Scope.request()/stabilize()` 表达同一件事，没有隔离标签与 prototype |
| 事件 | [`events.py`](../../cordis-py/src/cordis/events.py) | 本章只有 on/emit/count，没有五种分发模式 |
| 配置组合 | [`loader/composer.py`](../../cordis-py/src/cordis/loader/composer.py)、[`loader/parser.py`](../../cordis-py/src/cordis/loader/parser.py) | 正式版多格式 parser + composer；本章只保留 YAML adapter 与同一套 include/overlay 语义 |
| 模块解析 | [`loader/resolver.py`](../../cordis-py/src/cordis/loader/resolver.py) | 正式版用 `importlib` 与安全策略；本章是 `name:attribute` → 本地文件的受限 grammar |
| mount / update / replace 事务 | [`loader/runtime.py`](../../cordis-py/src/cordis/loader/runtime.py) | 正式版还有 disabled、version、错误位置等；本章保留 preflight、config-only 与回滚 |
| host-driven reload | [`loader/reloader.py`](../../cordis-py/src/cordis/loader/reloader.py) | 正式的 `ConfigReloader.start()/reload()` 只重新 compose 再交给 `Loader.update()`；本章额外的 `ChangeSource`/debounce/候选替换是**教学扩展** |
| HMR 的职责边界 | [`docs/architecture/07-configuration-loader.md`](../../cordis-py/docs/architecture/07-configuration-loader.md) | 正式文档明确：宿主检测变化、宿主刷新模块缓存，核心不建 watcher/线程/轮询 |
| Timer | 无对应资产 | 正式 `cordis-py` 目前**零 Timer 资产**；Timer 被列为可选插件、不进入最小核心（[`docs/architecture/01-overview.md`](../../cordis-py/docs/architecture/01-overview.md)） |

## DSH 与扩展边界

必须把结论写在正确的位置：

- **Core 对等证据**已有；Loader、Include、HMR、Timer 必须各自完成 API inventory 与双语言行为场景，才能声称对应组件等价（见 [`docs/cordis-core-compatibility.md`](../../cordis-py/docs/cordis-core-compatibility.md) 的范围声明）。
- 本章的 `ManualClock`/`TimerService`、`HotReloader`、`ChangeSource`、debounce 都是**教程定义的候选契约**，不是 `@deepseek-ai/cordis-plugin-timer` 1.1.2 或 `@deepseek-ai/cordis-plugin-hmr` 1.0.15 的等价证据，也没有与任何 pinned TypeScript build 对照过。
- 正式 Python suite 的既有结论不受本章影响：`cordis-py` 的 Python 测试 76 passed、lint/format/pyright/build 通过；pinned TypeScript reference oracle **仍未验证**；Tutorial 各章测试是**独立的教学测试**，不进入 `cordis-py` 的验证结论。
- 本章不更新 `cordis-py/docs/roadmap.md`、`docs/plans/` 或 [`.agents/STATE.md`](../../cordis-py/.agents/STATE.md)：Timer/HMR 尚未进入正式实现，Phase 0–5 的验证结论保持原样。

## 刻意简化

| 简化 | 影响 | 正式实现的差距 |
| --- | --- | --- |
| 模块变化是整树 replacement | 窗口内所有请求 `PENDING` | DSH HMR 的逐 Fiber/逐模块替换未复现 |
| 不清理 `sys.modules` | 旧模块对象仍被进程级缓存引用 | 正式设计把模块缓存刷新交给宿主 |
| 无 OS watcher / 无 mtime | `ChangeSource` 由宿主显式喂入 | 与正式"核心不建 watcher"一致，但也没有真实文件监视 |
| Fiber 无并发锁 | 顺序 await 下行为确定 | 正式 Fiber 处理并发 refresh/update 竞态 |
| 服务查找无隔离标签 | `get` 只有一个命名空间 | 正式 `reflect` 用 name + isolation label |
| 清理错误不抛出 | 由 `loader.cleanup_errors` 读取 | 正式实现经 Logger/observer 上报 |
| 回调必须同步（除插件入口） | Timer 触发点不引入任务 | 正式 Timer 插件同样以同步回调为主 |

## 本章不解决什么

- 不提供生产级的并发与隔离语义：并发 refresh、跨进程配置源、隔离标签都不在范围内。
- 不声称任何扩展组件与 DSH 插件等价：本章只证明"这些契约可以同时成立、可以更新、可以失败、可以恢复、可以关闭"。
- 不实现真实文件监视与真实模块热替换：宿主边界的形状是对的，机制是教学替身。
- 不把 Timer/HMR 提升为正式能力：那需要各自的 API inventory、语言差异表与双语言行为场景。

## 检查理解

1. 把 `config/cordis.yaml` 里的 include 顺序改成 `provider, consumer, infra`，预测 `activate:*` 的 trace 顺序，然后实测。为什么 consumer 仍然会等到 provider？（提示：`Consumer` 的 `inject` 与 `stabilize()`。）
2. 让 `gateway` 也 `inject` provider，重跑 `test_request_is_pending_while_the_candidate_activates`。窗口内 `harness.request()` 会返回什么？为什么这个改动破坏了"稳定请求入口"？
3. 在 `Fiber.update()` 里把 `await self.context.stabilize()` 移到最后（`load()` 之后），重跑 `test_config_change_updates_the_epoch_in_place`，解释 `agent.provider.preamble` 为什么变回 `alpha`。
4. 给 `provider_v2` 加一个在 `provide` 之前抛出的分支，重跑坏候选测试。确认 `loader.replacements == 0`、旧 generation 仍然服务请求、且 `timer.pending` 没有增长。
5. 把 `snapshot()` 里的 `effects` 换成只统计 `len(self.context.fiber.effects)`，关闭测试会漏掉哪一类泄漏？构造一个能触发该泄漏的插件。
6. 让 debounce 的 `delay` 变成 0，`test_burst_of_changes_is_merged_into_one_batch` 会怎样？这说明 `ManualClock.advance` 的哪一条不变量是必要的？
7. 用一句话向别人解释：为什么"配置变化优先 config update"不能只比较 YAML 文本 diff？（提示：identity 包含 `module`、`inject`、子条目 id。）
