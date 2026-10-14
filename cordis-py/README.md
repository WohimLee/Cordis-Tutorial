# Cordis-py

Cordis-py is a behavioral Python reproduction of the Cordis plugin runtime vendored by DeepSeek Harness.

The runtime core, declarative Loader and representative DeepSeek Harness composition are implemented: scoped Contexts, dependency-driven Fibers, lifecycle-owned Effects, safe YAML/TOML composition, transactional updates and a keyless LLM/tools/sessions/agent-loop profile. See the [architecture index](docs/architecture/README.md), [compatibility matrix](docs/compatibility.md), and [current state](.agents/STATE.md).

> **Status.** Phase 0–5 implementation assets are present in this directory, and the Python suite has
> been re-verified in place after the move from the repository root (2026-10-14): `uv sync --dev
> --frozen` installed 16 packages, `uv run pytest` passed 76 tests with 0 failures (repeat run
> identical), `uv run ruff check .`, `uv run ruff format --check .` (82 files), `uv run pyright`
> (0 errors, 0 warnings), `uv build` and `git diff --check` all passed, and the 21 Cordis Core
> scenarios `004`–`024` passed their Python oracle `--check` run.
>
> Two boundaries remain: the pinned TypeScript reference oracle is unverified because it requires the
> built `lib/index.js` of `@deepseek-ai/cordis` 4.0.2 at commit
> `dd6322d604e00eec1ba5e0c8541159906a21094a`, which does not exist on this machine, and scenarios
> `001`–`003` are Harness-shaped host scenarios that the Python oracle does not implement. The
> per-chapter `Tutorial` tests were not run this round. See [.agents/STATE.md](.agents/STATE.md) for
> the exact commands and results.

## Core model

```text
Context receives a Plugin
    ↓
Registry normalizes it and creates a Fiber for this mount
    ↓
Reflect resolves the Fiber's injected Services in the current Context scope
    ↓
Fiber runs the Plugin when every dependency is available
    ↓
Effects own its Services, listeners, child Fibers and user resources
```

Plugin is reusable behavior, PluginRuntime is its shared Registry record, Fiber is one concrete mount, Context is that mount's scoped capability view, and Effect gives every long-lived resource a reversible owner. Fiber ownership forms a lifecycle tree; Context derivation forms a related scope structure; service dependencies and event subscriptions form graphs across those structures. See the [core object model](docs/architecture/02-core-model.md), [lifecycle design](docs/architecture/04-lifecycle.md), and [PluginRuntime tutorial](../Tutorial/04-plugin-registry/02-plugin-runtime.md).

## Development

Python 3.11 or newer is supported. The checked-in `.python-version` selects Python 3.12 for local development.

```bash
uv sync --dev --frozen
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
uv run python tests/compat/oracle.py tests/compat/scenarios/004-core-smoke.json --check
uv build
```

The compatibility scenarios `004`–`024` are exercised on the Python side by `tests/compat/oracle.py`;
the paired TypeScript side needs the pinned Cordis 4.0.2 build described in
[tests/compat/README.md](tests/compat/README.md) and has not been verified here.

Implementation work follows the numbered files in [docs/plans](docs/plans/README.md).

## Semantic-core example

```python
from cordis import Context, Service, inject


class Counter(Service):
    provide = "counter"

    def __init__(self, ctx, config=None):
        self.value = 0
        super().__init__(ctx)


@inject("counter")
def greeter(ctx, config):
    ctx.on("ready", lambda: print(ctx.counter.value))


async def main():
    ctx = Context()
    counter = ctx.plugin(Counter)
    greeter_fiber = ctx.plugin(greeter)
    await counter.wait()
    await greeter_fiber.wait()
    ctx.emit("ready")
    await ctx.aclose()
```
