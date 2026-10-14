"""Run the keyless Harness-shaped profile."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

from cordis import Context
from cordis.loader import ConfigComposer, Loader, ModuleResolver

from .plugins import AgentLoop


async def main() -> None:
    context = Context()
    source = Path(__file__).with_name("cordis.yaml")
    loader = Loader(context, ModuleResolver(allowed_packages=["examples"]))
    await loader.mount(ConfigComposer().load(source, env={}))
    agent_loop = cast(AgentLoop, context.agentLoop)
    print(agent_loop.run("demo", "hello cordis"))
    await loader.close()
    await context.aclose()


if __name__ == "__main__":
    asyncio.run(main())
