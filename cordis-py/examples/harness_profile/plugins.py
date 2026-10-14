"""Keyless Harness-shaped service definitions and plugins."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import cast

from cordis import Context, Service


@dataclass(slots=True)
class Session:
    """A minimal ordered conversation log."""

    id: str
    messages: list[tuple[str, str]] = field(default_factory=lambda: list[tuple[str, str]]())


class LlmRuntime(Service):
    """Provider-neutral route registry matching the DSH `llm` seam."""

    provide = "llm"

    def __init__(self, context: Context, config: object = None) -> None:
        self.adapters: dict[str, Callable[[str], str]] = {}
        super().__init__(context)

    def register_adapter(self, route: str, adapter: Callable[[str], str]) -> None:
        owner = self.caller_context

        def setup() -> Callable[[], None]:
            if route in self.adapters:
                raise ValueError(f"duplicate LLM route {route!r}")
            self.adapters[route] = adapter

            def cleanup() -> None:
                self.adapters.pop(route, None)

            return cleanup

        owner.effect(setup, f"llm.register_adapter({route!r})")

    def complete(self, route: str, prompt: str) -> str:
        return self.adapters[route](prompt)


class ToolRuntime(Service):
    """Named tool registry matching the DSH `tools` seam."""

    provide = "tools"

    def __init__(self, context: Context, config: object = None) -> None:
        self.tools: dict[str, Callable[[str], str]] = {}
        super().__init__(context)

    def register(self, name: str, tool: Callable[[str], str]) -> None:
        owner = self.caller_context

        def setup() -> Callable[[], None]:
            self.tools[name] = tool

            def cleanup() -> None:
                self.tools.pop(name, None)

            return cleanup

        owner.effect(setup, f"tools.register({name!r})")

    def execute(self, name: str, value: str) -> str:
        return self.tools[name](value)


class SessionStore(Service):
    """In-memory session service matching the DSH `sessions` seam."""

    provide = "sessions"

    def __init__(self, context: Context, config: object = None) -> None:
        self.sessions: dict[str, Session] = {}
        super().__init__(context)

    def get_or_create(self, session_id: str) -> Session:
        return self.sessions.setdefault(session_id, Session(session_id))


class AgentRegistry(Service):
    """Minimal registry matching the stable DSH `agents` seam."""

    provide = "agents"


class SystemPrompt(Service):
    """Minimal prompt service required by DSH tools and agent-loop."""

    provide = "systemPrompt"


class AgentLoop(Service):
    """Small consumer with the same five injected services as DSH AgentLoop."""

    provide = "agentLoop"
    inject = ("agents", "sessions", "llm", "tools", "systemPrompt")

    def __init__(self, context: Context, config: object = None) -> None:
        raw = cast(Mapping[str, object], config or {})
        self.route = cast(str, raw.get("provider", "mock"))
        super().__init__(context)

    def run(self, session_id: str, prompt: str) -> str:
        sessions = cast(SessionStore, self.context.sessions)
        tools = cast(ToolRuntime, self.context.tools)
        llm = cast(LlmRuntime, self.context.llm)
        session = sessions.get_or_create(session_id)
        session.messages.append(("user", prompt))
        tool_result = tools.execute("uppercase", prompt)
        answer = llm.complete(self.route, tool_result)
        session.messages.append(("assistant", answer))
        self.context.emit("agent/turn", session_id, answer)
        return answer


def mock_llm_provider(context: Context, config: object) -> None:
    """Register a deterministic provider route without network or credentials."""

    raw = cast(Mapping[str, object], config)
    route = cast(str, raw.get("route", "mock"))
    prefix = cast(str, raw.get("prefix", "mock"))
    llm = cast(LlmRuntime, context.llm)
    llm.register_adapter(route, lambda prompt: f"{prefix}:{prompt}")


mock_llm_provider.inject = ("llm",)  # type: ignore[attr-defined]


def uppercase_tool(context: Context, config: object) -> None:
    tools = cast(ToolRuntime, context.tools)
    tools.register("uppercase", str.upper)


uppercase_tool.inject = ("tools",)  # type: ignore[attr-defined]
