# DeepSeek Harness Compatibility

## Selected seams

Phase 04 validates runtime composition, not a wholesale port of Harness business logic. The representative profile follows these current DSH contracts:

| DSH package | Stable service | Selected observable contract |
| --- | --- | --- |
| `packages/llm/llm` | `llm` | Provider plugins inject the runtime and register lifecycle-owned adapter routes. |
| `packages/core/tools` | `tools` | Tool plugins register lifecycle-owned named definitions. |
| `packages/core/session` | `sessions` | Agent turns append ordered user/assistant events to a session. |
| `packages/core/agent` | `agents` | The registry remains a stable dependency of the loop. |
| `packages/core/agent-loop` | `agentLoop` | The loop injects `agents`, `sessions`, `llm`, `tools` and `systemPrompt`. |

The runnable profile is in [`examples/harness_profile`](../examples/harness_profile). It uses a deterministic mock LLM and uppercase tool, so it requires no key or network access.

## Provider replacement semantics

DSH separates the stable `llm` service from provider adapter plugins. Reconfiguring one adapter restarts that provider Fiber and reverses its adapter-registration Effect, but does not unload Agent Loop because the `llm` service never disappeared. Replacing the `llm` service itself does remove an injected dependency and therefore unloads/reloads Agent Loop through the normal Fiber cascade.

## Intentional limits

- Python module specifiers replace JavaScript package specifiers.
- The example models service boundaries and lifecycle, not token streaming or provider wire formats.
- No real API, persistence backend, MCP process or sandbox is started.
- TypeScript private fields, Symbols, Proxy behavior and byte-identical logs are not compatibility targets.

## Compatibility matrix

| Pattern | Status | Evidence |
| --- | --- | --- |
| Service definition/provider/consumer | Supported | Harness profile mounts six independent services. |
| Dependency-independent config order | Supported | `agent-loop` precedes all five injected services. |
| Fiber-owned adapter/tool registration | Supported | Provider disposal removes its route and tool. |
| Adapter HMR without Agent Loop restart | Supported | Scenario `002-harness-adapter-reload`. |
| Service-loss dependency cascade | Supported | Scenario `003-harness-llm-service-loss`. |
| Session/tool/LLM turn composition | Supported seam | Keyless profile records an ordered transcript. |
| TypeScript specifiers unchanged | Intentionally different | Python uses `module:attribute`. |
| Full DSH streaming/wire protocols | Not implemented here | They belong to Harness capability packages. |
| Automatic Python module-cache reload | Host responsibility | Refresh imports, then call `Loader.replace()`. |

## Adding a provider plugin

A provider injects the stable service and registers a Fiber-owned implementation:

```python
from typing import cast

from cordis import Context
from examples.harness_profile.plugins import LlmRuntime


def my_provider(ctx: Context, config: object) -> None:
    llm = cast(LlmRuntime, ctx.llm)
    llm.register_adapter("my-route", lambda prompt: f"answer:{prompt}")


my_provider.inject = ["llm"]
```

Configure it independently of its consumers:

```yaml
plugins:
  - id: my-provider
    module: my_package.providers:my_provider
  - id: agent-loop
    module: examples.harness_profile.plugins:AgentLoop
    config:
      provider: my-route
```

Registration APIs use `Service.caller_context` so the calling provider Fiber owns cleanup. Do not keep global registrations or manually sort plugins. Test a provider with one keyless turn, dispose or update its Fiber, and assert the registration disappears on shutdown.
