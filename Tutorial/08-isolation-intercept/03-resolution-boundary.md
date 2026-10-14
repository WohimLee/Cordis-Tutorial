# 两条机制在哪里汇合

## Step 7：先解析 Service，再解析配置

一个 Consumer 使用 injected Service 时，完整过程可以拆成两步：

```text
Consumer Context
    │
    ├── name + isolation label ──→ Reflect 选择 Impl
    │
    └── base + intercepts + head ─→ Service 合并 config
```

Isolation 与 Intercept 都保存在 derived Context 上，但不要把它们混成一个概念：

- 改变 isolation label，可能换成另一个 Service 实例；
- 改变 intercept，只会改变当前 Service 看到的配置。

本章教学代码用 `Service.resolve_config(context, ...)` 明确传入 Consumer Context。正式 Runtime 会通过 caller Context tracking 自动找到调用者；该机制要等第 13 章的完整 Service 抽象再加入。

## Step 8：保持 Context 派生不可变

`isolate()` 和 `intercept()` 都采用“复制元数据并返回 child”的做法：

```python
root = Context()
private = root.isolate("database")
regional = private.intercept("database", {"region": "cn"})
```

`root` 没被修改，`private` 只带 isolation，`regional` 同时带 isolation 和 intercept。这样 sibling Plugin 不会因为另一个 Plugin 修改 Context 而发生暗中变化。

## 与正式源码对应

- Context 派生、isolate 与 intercept：[`src/cordis/context.py`](../../cordis-py/src/cordis/context.py)
- name + label 的服务解析：[`src/cordis/reflect.py`](../../cordis-py/src/cordis/reflect.py)
- Service config merge：[`src/cordis/service.py`](../../cordis-py/src/cordis/service.py)
- Inject head 进入 activation：[`src/cordis/fiber.py`](../../cordis-py/src/cordis/fiber.py)

正式实现还会验证配置必须是 string-keyed mapping，并在自定义 merge 不存在时采用浅合并。本章保留相同的可观察顺序。

## 与 DSH Cordis 对照

本章复现的核心语义是：

- Service resolution 同时使用 name 与 isolation label；
- 未指定 label 的 `isolate()` 创建 fresh token；
- 显式使用同一个 label 的 Context 共享 Service scope；
- derived Context 继承 isolation 和 intercept chain；
- 默认配置优先级为 base、外层 intercept、内层 intercept、Inject head；
- Service 可以用 `Config.merge` 替换默认 shallow merge。

JavaScript 使用 prototype chain 和 symbol 表达继承与唯一 label。Python 使用复制的 mapping 和 object identity，语言机制不同，但以上可观察行为一致。

## 本章没有重复实现 Epoch

第 07 章已经证明依赖变化会产生新 Epoch。本章只改变 `resolve_impl()` 的条件：从 name 扩展为 name + label。

把完整 scheduler 再复制一遍不会增加知识，反而会遮住本章的新机制。正式 Runtime 中，两者自然组合：Reflect 只通知同 name、同 label 的 Consumer，Fiber 仍使用唯一的 Epoch runner。

## 检查理解

1. 为什么两个 Service 可以同名，却不会互相覆盖？
2. 为什么 `extend()` 应继承同一个 label，而 `isolate()` 默认应创建新 label？
3. Isolation 与 Intercept 分别改变解析过程的哪一部分？
4. base、两层 intercept 和 Inject head 的覆盖顺序是什么？
5. 为什么 custom `Config.merge` 不应改变配置来源的顺序？
6. 为什么本章无需重写 Dependency Epoch scheduler？
