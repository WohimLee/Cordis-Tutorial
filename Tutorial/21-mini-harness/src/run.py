"""Run the chapter 21 mini harness end to end and print what it observes.

Deterministic by construction: the clock is manual, the change source is driven by
this script, and no real time, network or global state is involved.

    cd Tutorial/21-mini-harness/src
    python3 run.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from chapter21 import ChangeEvent, MiniHarness


async def main() -> None:
    config = Path(__file__).resolve().parent.parent / "config" / "cordis.yaml"
    harness = MiniHarness(config)
    await harness.start()
    print("cold start request:", harness.request("hello"))
    print("active entries:", [entry.id for entry in harness.loader.roots])

    harness.clock.advance(1.0)
    heartbeat = harness.context.get("heartbeat")
    agent = harness.context.get("agent")
    print("heartbeats after 1.0 virtual seconds:", heartbeat.ticks, agent.ticks)

    harness.source.publish(
        ChangeEvent(
            kind="module",
            entry="provider",
            specifier="provider_v2:plugin",
            config={"preamble": "demo"},
            has_config=True,
        )
    )
    harness.clock.advance(harness.reloader.debounce)
    await harness.reloader.flush()
    print("after module replacement:", harness.request("hello"))
    print("replacements:", harness.reloader.replacements)

    await harness.close()
    print("shutdown snapshot:", harness.snapshot())


if __name__ == "__main__":
    asyncio.run(main())
