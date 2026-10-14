import json
from pathlib import Path
from typing import cast

import pytest

from cordis import Context, Fiber, FiberState
from cordis.loader import ConfigReloader, Loader, ModuleResolver
from examples.harness_profile.plugins import (
    AgentLoop,
    AgentRegistry,
    LlmRuntime,
    SessionStore,
    SystemPrompt,
    ToolRuntime,
)

PROFILE = Path("examples/harness_profile/cordis.yaml").read_text()
SCENARIOS = Path("tests/compat/scenarios")


@pytest.mark.asyncio
async def test_keyless_harness_profile_and_provider_reload(tmp_path: Path) -> None:
    source = tmp_path / "cordis.yaml"
    source.write_text(PROFILE)
    context = Context()
    loader = Loader(context, ModuleResolver(allowed_packages=["examples"]))
    reloader = ConfigReloader(loader, source, env={})
    await reloader.start()
    agent_entry = loader.entries["agent-loop"]
    agent_fiber = agent_entry.fiber
    agent_loop = cast(AgentLoop, context.agentLoop)

    assert agent_loop.run("session-1", "hello") == "provider-a:HELLO"
    source.write_text(PROFILE.replace("prefix: provider-a", "prefix: provider-b"))
    assert await reloader.reload() is True
    assert loader.entries["agent-loop"].fiber is agent_fiber
    assert agent_loop.run("session-1", "again") == "provider-b:AGAIN"
    actual: dict[str, object] = {
        "answers": ["provider-a:HELLO", "provider-b:AGAIN"],
        "agent_fiber_reused": loader.entries["agent-loop"].fiber is agent_fiber,
    }

    sessions = cast(SessionStore, context.sessions)
    assert sessions.sessions["session-1"].messages == [
        ("user", "hello"),
        ("assistant", "provider-a:HELLO"),
        ("user", "again"),
        ("assistant", "provider-b:AGAIN"),
    ]

    await loader.close()
    await context.aclose()
    actual["shutdown"] = {
        "registry_size": context.registry.size,
        "services": len(context.reflect.implementations()),
        "root_effects": len(context.fiber.getEffects()),
    }
    scenario = json.loads((SCENARIOS / "002-harness-adapter-reload.json").read_text())
    assert actual == scenario["expected"]
    assert context.reflect.implementations() == ()
    assert context.registry.size == 0
    assert context.fiber.getEffects() == ()


@pytest.mark.asyncio
async def test_llm_service_loss_reloads_agent_loop_dependency_epoch() -> None:
    context = Context()
    states: list[str] = []

    def status(fiber: Fiber, old: FiberState) -> None:
        if fiber.name == "AgentLoop":
            states.append(fiber.state.value)

    context.on("internal/status", status)
    agent = context.plugin(AgentLoop, {"provider": "mock"})
    providers = [
        context.plugin(ToolRuntime),
        context.plugin(SessionStore),
        context.plugin(AgentRegistry),
        context.plugin(SystemPrompt),
    ]
    llm = context.plugin(LlmRuntime)
    await llm.wait()
    await agent.wait()
    assert agent.state is FiberState.ACTIVE

    await llm.dispose()
    assert agent.state is FiberState.PENDING
    replacement = context.plugin(LlmRuntime)
    await replacement.wait()
    await agent.wait()
    assert agent.state is FiberState.ACTIVE

    scenario = json.loads((SCENARIOS / "003-harness-llm-service-loss.json").read_text())
    assert states == scenario["expected_agent_states"]
    await context.aclose()
    assert all(fiber.state is FiberState.DISPOSED for fiber in [agent, *providers, replacement])
