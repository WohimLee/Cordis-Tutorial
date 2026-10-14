from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
import yaml
from chapter21 import (
    ChangeEvent,
    HarnessError,
    LocalModuleSource,
    MiniHarness,
)

HERE = Path(__file__).resolve().parent
CONFIG_DIR = HERE.parent / "config"
PLUGINS_DIR = HERE / "plugins"


def stage_config(tmp_path: Path) -> Path:
    """Copy the real config tree so a test can rewrite it on disk."""

    target = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, target)
    return target / "cordis.yaml"


def set_provider_preamble(config: Path, preamble: str) -> None:
    path = config.parent / "provider.yaml"
    document = yaml.safe_load(path.read_text())
    document["plugins"][0]["config"]["preamble"] = preamble
    path.write_text(yaml.safe_dump(document))


def module_event(
    specifier: str,
    *,
    preamble: str | None = None,
) -> ChangeEvent:
    return ChangeEvent(
        kind="module",
        entry="provider",
        specifier=specifier,
        config=None if preamble is None else {"preamble": preamble},
        has_config=preamble is not None,
    )


@pytest.mark.asyncio
async def test_cold_start_mounts_the_tree_without_manual_ordering(
    tmp_path: Path,
) -> None:
    config = stage_config(tmp_path)
    harness = MiniHarness(config)
    await harness.start()
    try:
        assert harness.request("ping") == "v1:alpha:ping"
        assert harness.context.get("agent") is not None
        assert [entry.id for entry in harness.loader.roots] == [
            "gateway",
            "heartbeat",
            "consumer",
            "provider",
        ]
        # the consumer is declared before the provider and still activates after it
        assert harness.trace.order("activate:provider") < harness.trace.order(
            "activate:consumer"
        )
        assert harness.timer.pending == 1
        # every specifier in the real config points at a real plugin file
        source = LocalModuleSource(PLUGINS_DIR)
        for entry in harness.composer.load(config):
            assert source.path_for(entry.module).is_file()
        assert harness.snapshot()["watcher_subscriptions"] == 1
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_manual_clock_drives_the_heartbeat(tmp_path: Path) -> None:
    harness = MiniHarness(stage_config(tmp_path))
    await harness.start()
    try:
        heartbeat = harness.context.get("heartbeat")
        agent = harness.context.get("agent")
        assert (heartbeat.ticks, agent.ticks) == (0, 0)

        # the root overlay halves the declared period, so 1.0 virtual seconds fire twice
        assert harness.clock.advance(1.0) == 2
        assert (heartbeat.ticks, agent.ticks) == (2, 2)

        assert harness.clock.advance(0.5) == 1
        assert (heartbeat.ticks, agent.ticks) == (3, 3)
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_config_change_updates_the_epoch_in_place(tmp_path: Path) -> None:
    config = stage_config(tmp_path)
    harness = MiniHarness(config)
    await harness.start()
    try:
        uid = harness.loader.entries["provider"].fiber.uid
        assert harness.request("ping") == "v1:alpha:ping"

        set_provider_preamble(config, "alpha-2")
        harness.source.publish(ChangeEvent(kind="config"))
        assert await harness.reloader.flush() == 0  # nothing fires before the debounce
        harness.clock.advance(harness.reloader.debounce)
        assert await harness.reloader.flush() == 1

        assert harness.request("ping") == "v1:alpha-2:ping"
        assert harness.reloader.config_updates == 1
        assert harness.reloader.replacements == 0
        assert harness.loader.replacements == 0
        entry = harness.loader.entries["provider"]
        assert entry.fiber.uid == uid  # the same fiber serves the new epoch
        assert entry.fiber.version == 1
        assert harness.trace.count("cleanup:provider") == 1
        assert harness.timer.pending == 1
        # the consumer was reloaded around the service gap, not left stale
        assert harness.context.get("agent").provider.preamble == "alpha-2"
        assert harness.trace.count("activate:consumer") == 2
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_module_change_replaces_the_generation(tmp_path: Path) -> None:
    config = stage_config(tmp_path)
    harness = MiniHarness(config)
    await harness.start()
    try:
        old_agent = harness.context.get("agent")
        harness.clock.advance(0.5)
        assert old_agent.ticks == 1

        harness.source.publish(module_event("provider_v2:plugin", preamble="beta"))
        harness.clock.advance(harness.reloader.debounce)
        assert await harness.reloader.flush() == 1

        assert harness.request("ping") == "v2:beta:PING"
        assert harness.reloader.replacements == 1
        assert harness.trace.count("cleanup:provider") == 1
        assert harness.trace.count("cleanup:consumer") == 1

        # one heartbeat interval and one consumer listener, not two of each
        assert harness.timer.pending == 1
        assert harness.events.count() == 1

        new_agent = harness.context.get("agent")
        assert new_agent is not old_agent
        harness.clock.advance(0.5)
        assert new_agent.ticks == 1
        assert old_agent.ticks == 1  # the old epoch's interval and listener are gone
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_request_is_pending_while_the_candidate_activates(tmp_path: Path) -> None:
    harness = MiniHarness(stage_config(tmp_path))
    await harness.start()
    try:
        harness.gate.close()
        harness.source.publish(module_event("provider_v2:plugin", preamble="beta"))
        harness.clock.advance(harness.reloader.debounce)

        applying = asyncio.create_task(harness.reloader.flush())
        await harness.gate.arrived.wait()
        assert harness.request("ping") == "PENDING"

        harness.gate.release()
        assert await applying == 1
        assert harness.request("ping") == "v2:beta:PING"
        assert harness.context.get("agent") is not None
    finally:
        harness.gate.release()
        await harness.close()


@pytest.mark.asyncio
async def test_invalid_candidate_keeps_the_previous_generation(tmp_path: Path) -> None:
    config = stage_config(tmp_path)
    harness = MiniHarness(config)
    await harness.start()
    try:
        previous = harness.loader.entries["provider"].fiber.uid

        # (a) the specifier cannot be resolved: the tree is never touched
        harness.source.publish(module_event("provider_missing:plugin", preamble="beta"))
        harness.clock.advance(harness.reloader.debounce)
        assert await harness.reloader.flush() == 1
        assert harness.reloader.rejected == 1
        assert harness.loader.replacements == 0
        assert harness.request("ping") == "v1:alpha:ping"
        assert harness.loader.entries["provider"].fiber.uid == previous

        # (b) the module resolves but the candidate fails validation while loading
        patch = ChangeEvent(
            kind="module",
            entry="provider",
            specifier="provider_v2:plugin",
            config={"preamble": 42},
            has_config=True,
        )
        harness.source.publish(patch)
        harness.clock.advance(harness.reloader.debounce)
        assert await harness.reloader.flush() == 1
        assert harness.reloader.rejected == 2
        assert isinstance(harness.reloader.last_error, HarnessError)
        assert "preamble" in str(harness.reloader.last_error)
        assert harness.loader.replacements == 0
        assert harness.request("ping") == "v1:alpha:ping"
        assert harness.timer.pending == 1

        # the legal recovery applies the same structural change successfully
        harness.source.publish(module_event("provider_v2:plugin", preamble="beta"))
        harness.clock.advance(harness.reloader.debounce)
        assert await harness.reloader.flush() == 1
        assert harness.loader.replacements == 1
        assert harness.request("ping") == "v2:beta:PING"
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_burst_of_changes_is_merged_into_one_batch(tmp_path: Path) -> None:
    config = stage_config(tmp_path)
    harness = MiniHarness(config)
    await harness.start()
    try:
        set_provider_preamble(config, "alpha-burst")
        for _ in range(3):
            harness.source.publish(ChangeEvent(kind="config"))
        # the three events share one pending debounce entry and one heartbeat interval
        assert harness.clock.pending == 2

        assert await harness.reloader.flush() == 0  # the clock has not fired yet
        harness.clock.advance(harness.reloader.debounce)
        assert await harness.reloader.flush() == 1

        assert harness.reloader.applied == 1
        assert harness.reloader.config_updates == 1
        assert harness.request("ping") == "v1:alpha-burst:ping"
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_shutdown_zeroes_every_counter_and_reports_cleanup_errors(
    tmp_path: Path,
) -> None:
    config = stage_config(tmp_path)
    harness = MiniHarness(config)
    await harness.start()
    harness.source.publish(module_event("provider_v2:plugin", preamble="beta"))
    harness.clock.advance(harness.reloader.debounce)
    await harness.reloader.flush()
    harness.clock.advance(0.5)

    live = harness.snapshot()
    assert live["services"] == 8  # four harness services plus four plugin services
    assert live["fibers"] == 4
    assert live["entries"] == 4
    assert live["listeners"] == 1
    assert live["timer_handles"] == 1
    assert live["watcher_subscriptions"] == 1
    assert live["effects"] > 0

    await harness.close()
    assert set(harness.snapshot().values()) == {0}
    assert harness.trace.count("cleanup:provider") == 2  # v1 replaced, v2 closed
    assert harness.request("ping") == "PENDING"
    await harness.close()  # idempotent

    # a failing cleanup is aggregated, does not stop the other cleanups, and does
    # not stop the shutdown from reaching zero
    broken = stage_config(tmp_path / "broken")
    document = yaml.safe_load((broken.parent / "infra.yaml").read_text())
    document["plugins"][0]["config"]["cleanup_error"] = True
    (broken.parent / "infra.yaml").write_text(yaml.safe_dump(document))

    failing = MiniHarness(broken)
    await failing.start()
    await failing.close()
    assert len(failing.loader.cleanup_errors) == 1
    assert isinstance(failing.loader.cleanup_errors[0], BaseExceptionGroup)
    assert "gateway cleanup failed" in repr(failing.loader.cleanup_errors[0])
    assert failing.trace.count("cleanup:heartbeat") == 1
    assert set(failing.snapshot().values()) == {0}
