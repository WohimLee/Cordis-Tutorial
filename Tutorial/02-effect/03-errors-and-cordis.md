# 错误处理与 Cordis 对照

## Step 6：一个 Cleanup 失败后继续

假设 Effect 有三个 Cleanup，中间一个抛出异常。错误不能让剩下的 Cleanup 永远不执行。

Effect 会先记录错误，再继续清理：

```python
errors = []

while self._cleanups:
    try:
        await run_cleanup(self._cleanups.pop())
    except BaseException as error:
        errors.append(error)
```

全部结束后，再用 Python 的 `ExceptionGroup` 一次报告所有错误。

运行：

```bash
uv run pytest Tutorial/02-effect/src/test_chapter02.py -k cleanup_failure
```

测试不仅检查异常，也检查两个 Cleanup 都执行过。Lifecycle 测试必须同时关心“报了什么错”和“资源最后是什么状态”。

## Step 7：运行全部检查

```bash
uv run pytest Tutorial/02-effect/src/test_chapter02.py -q
```

本章最终得到：

```text
Effect
├── setup 立即开始
├── await effect 等待 setup
├── effect() 开始 dispose
├── await effect() 等待 dispose
├── 多个 Cleanup 逆序执行
├── 重复 dispose 共用一个 Task
└── Cleanup 错误最后聚合
```

## 与正式源码对应

正式实现位于 [`src/cordis/effect.py`](../../cordis-py/src/cordis/effect.py)。它在本章能力之上还包含：

- `EffectMeta` 诊断树；
- nested Effect；
- 同步 setup 的立即错误语义；
- async iterable Cleanup；
- setup 失败后的完整 rollback；
- owner 关闭和 Effect 安装之间的竞态保护。

这些能力不会在本章一次塞进来。Effect tree 会在它有 Fiber owner 后出现；复杂竞态会在异步竞态章节集中处理。

## 与 DSH Cordis 对照

DSH Cordis 的 Effect 是一组 TypeScript 返回值类型，不是一个名为 `Effect` 的运行时 class。`fiber.effect()` 返回带内部 metadata 的 disposer function。

Python 没有 JavaScript function object 和 PromiseLike 的完全相同组合，因此 Cordis-py 使用一个同时实现 `__call__` 和 `__await__` 的 Effect 对象。对象形状不同，但保留了可观察能力：

- setup 立即执行；
- disposer 可调用；
- 异步 setup 可以等待；
- Cleanup 逆序且只执行一次；
- dispose 等待仍在进行的 setup；
- 无效返回值抛出 `TypeError("Invalid effect")`。

## 本章刻意简化的地方

- Effect 还没有 owner；
- Effect 不会自动随 Context 关闭；
- setup 中途失败时的 rollback 只覆盖已经收集到的普通 Cleanup；
- 没有 nested metadata；
- 没有处理 owner 与 setup 同时变化的全部竞态。

下一章实现最小 **Fiber**，让一次 Plugin 挂载拥有自己的 Effect。到那时我们才能删除第 01 章的全局 Cleanup 列表。

## 检查理解

1. 为什么 Cleanup 要逆序执行？
2. 为什么重复调用 dispose 必须返回同一个 Task？
3. `await effect` 与 `await effect()` 分别等待什么？
4. 异步 setup 进行中收到 dispose 请求时，为什么不能直接取消全部工作？
5. 一个 Cleanup 失败后，为什么还要执行剩下的 Cleanup？
