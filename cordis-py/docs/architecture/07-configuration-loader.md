# 07. 配置与 Loader

## 配置验证协议

核心定义最小协议，不绑定 Pydantic：

```python
class ConfigValidator(Protocol):
    def validate(self, value: object) -> object: ...
    def merge(self, *values: object) -> object: ...
```

可为 dataclass、Pydantic、attrs 和 callable 提供适配器。验证在每次激活前发生，因为配置解析可能依赖当前注入服务。

验证失败抛出 `ValidationError`，包含 message、path 和可选 code。失败不得留下部分 Effect。

## 配置文件示例

```yaml
plugins:
  - module: myapp.database:DatabasePlugin
    config:
      url: ${DATABASE_URL}
  - module: myapp.agent:AgentPlugin
    inject:
      database: {}
```

完整实现可支持 YAML 和 TOML。Loader 模型不应绑定某种文本格式。

## Loader 流程

1. 读取配置并保存来源位置；
2. 展开 include 和 overlay；
3. 解析环境变量或受限表达式；
4. 通过 `importlib` 解析 `module:attribute`；
5. 规范化 PluginSpec；
6. 按配置树创建 Loader Entry；
7. 由 Cordis 依赖系统决定实际激活顺序；
8. 变更时对比 entry identity，执行 update、restart、mount 或 dispose。

Loader 不做服务依赖拓扑排序。依赖等待属于 Fiber；Loader 只维护配置树、插件身份和持久化更新。

分层职责必须清晰：Core 运行插件，Loader 把声明式配置变成插件树，HMR 检测代码或配置变化，Include/Group 组合配置树。

## Loader 数据模型

每个 Entry 至少包含：

- 稳定 id；
- module specifier；
- 原始 config；
- disabled 条件；
- 父子关系；
- source 文件和位置；
- 当前 Fiber；
- 配置版本和加载错误。

## Include 与 Overlay

- include 使用规范化绝对路径识别循环；
- 相对模块和 include 路径相对于来源文件解析；
- overlay 以 entry id 或显式 key 定位，不能依赖数组下标；
- 合并规则必须确定且可解释；
- 更新失败保留上一个有效运行态，同时暴露新配置错误。

## HMR

HMR 分为配置更新和代码更新：

- 仅配置变化优先调用 `fiber.update()`；
- callback 身份变化时挂载新 Fiber，再安全销毁旧 Fiber；
- 模块删除必须 dispose 对应 Entry；
- reload 期间服务依赖仍由 Fiber 状态机协调；
- HMR 不得绕过 Effect 清理。

Python 实现提供宿主驱动的 `ConfigReloader`，不在核心内创建 watcher、线程或轮询任务。宿主检测到配置文件变化后调用 `reload()`；适配器重新执行 compose，并通过 `Loader.update()` 的事务与回滚路径应用结果。模块代码重载由宿主负责刷新 import 状态，再显式调用 `Loader.replace()`，避免 Cordis 猜测进程级模块缓存策略。

## 安全边界

- YAML/TOML 不执行任意 Python；
- 插件 import 是代码执行，应用必须授权模块来源；
- Loader 支持允许目录或允许包列表；
- secret 配置默认不得进入日志和诊断快照；
- 沙箱不是 Cordis 核心职责，应由进程、容器或系统权限层实现。
