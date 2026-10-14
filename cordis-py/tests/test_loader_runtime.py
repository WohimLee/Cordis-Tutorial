from pathlib import Path

import pytest

from cordis import Context, Service
from cordis.fiber import FiberState
from cordis.loader import Loader, LoaderError, ModuleResolver, ParsedEntry, SourceLocation

trace: list[str] = []


class Value(Service):
    provide = "value"


def provider(context: Context, config: object) -> object:
    Value(context)
    trace.append("provider:start")
    return lambda: trace.append("provider:stop")


def consumer(context: Context, config: object) -> object:
    context.get("value")
    trace.append("consumer:start")
    return lambda: trace.append("consumer:stop")


def parent(context: Context, config: object) -> object:
    trace.append("parent:start")
    return lambda: trace.append("parent:stop")


def child(context: Context, config: object) -> object:
    trace.append("child:start")
    return lambda: trace.append("child:stop")


def broken(context: Context, config: object) -> None:
    raise RuntimeError("broken child")


class PositiveConfig:
    @staticmethod
    def validate(value: object) -> int:
        if not isinstance(value, int) or value <= 0:
            raise ValueError("expected positive config")
        return value


def configurable_a(context: Context, config: object) -> object:
    trace.append(f"a:{config}")
    return lambda: trace.append(f"a:stop:{config}")


def configurable_b(context: Context, config: object) -> object:
    trace.append(f"b:{config}")
    return lambda: trace.append(f"b:stop:{config}")


configurable_a.Config = PositiveConfig  # type: ignore[attr-defined]
configurable_b.Config = PositiveConfig  # type: ignore[attr-defined]


def parsed(
    entry_id: str,
    module: str,
    *,
    disabled: bool = False,
    config: object = None,
    inject: dict[str, object | None] | None = None,
    children: tuple[ParsedEntry, ...] = (),
) -> ParsedEntry:
    return ParsedEntry(
        id=entry_id,
        module=module,
        config=config,
        location=SourceLocation(Path(__file__)),
        disabled=disabled,
        inject={} if inject is None else inject,
        children=children,
    )


@pytest.fixture(autouse=True)
def clear_trace() -> None:
    trace.clear()


@pytest.mark.asyncio
async def test_loader_mounts_without_topological_sorting() -> None:
    context = Context()
    resolver = ModuleResolver(allowed_packages=["test_loader_runtime"])
    loader = Loader(context, resolver)

    roots = await loader.mount(
        (
            parsed(
                "consumer",
                "test_loader_runtime:consumer",
                inject={"value": None},
            ),
            parsed("provider", "test_loader_runtime:provider"),
        )
    )

    assert roots[0].fiber is not None
    assert roots[0].fiber.state is FiberState.ACTIVE
    assert trace[:2] == ["provider:start", "consumer:start"]
    await loader.close()
    await context.aclose()


@pytest.mark.asyncio
async def test_children_are_owned_by_parent_fiber() -> None:
    context = Context()
    loader = Loader(context, ModuleResolver(allowed_packages=["test_loader_runtime"]))
    child_entry = parsed("child", "test_loader_runtime:child")
    roots = await loader.mount(
        (parsed("parent", "test_loader_runtime:parent", children=(child_entry,)),)
    )
    parent_entry = roots[0]
    mounted_child = parent_entry.children[0]

    assert parent_entry.fiber is not None
    await parent_entry.fiber.dispose()

    assert mounted_child.fiber is not None
    assert mounted_child.fiber.state is FiberState.DISPOSED
    assert trace == ["parent:start", "child:start", "child:stop", "parent:stop"]
    await loader.close()
    await context.aclose()


@pytest.mark.asyncio
async def test_disabled_entry_is_retained_but_not_imported() -> None:
    context = Context()
    loader = Loader(context, ModuleResolver())

    roots = await loader.mount((parsed("off", "not.allowed:plugin", disabled=True),))

    assert roots[0].fiber is None
    assert loader.entries["off"] is roots[0]
    await loader.close()
    await context.aclose()


@pytest.mark.asyncio
async def test_mount_failure_rolls_back_started_parent() -> None:
    context = Context()
    loader = Loader(context, ModuleResolver(allowed_packages=["test_loader_runtime"]))
    tree = (
        parsed(
            "parent",
            "test_loader_runtime:parent",
            children=(parsed("broken", "test_loader_runtime:broken"),),
        ),
    )

    with pytest.raises(RuntimeError, match="broken child"):
        await loader.mount(tree)

    parent_entry = loader.entries["parent"]
    assert parent_entry.fiber is not None
    assert parent_entry.fiber.state is FiberState.DISPOSED
    assert trace == ["parent:start", "parent:stop"]
    await context.aclose()


@pytest.mark.asyncio
async def test_config_update_rolls_back_previously_updated_entries() -> None:
    context = Context()
    loader = Loader(context, ModuleResolver(allowed_packages=["test_loader_runtime"]))
    initial = (
        parsed("a", "test_loader_runtime:configurable_a", config=1),
        parsed("b", "test_loader_runtime:configurable_b", config=1),
    )
    await loader.mount(initial)

    invalid = (
        parsed("a", "test_loader_runtime:configurable_a", config=2),
        parsed("b", "test_loader_runtime:configurable_b", config=0),
    )
    with pytest.raises(LoaderError, match="failed to update entry configuration"):
        await loader.update(invalid)

    assert loader.entries["a"].fiber is not None
    assert loader.entries["a"].fiber.config == 1
    assert loader.entries["a"].parsed.config == 1
    assert loader.entries["b"].fiber is not None
    assert loader.entries["b"].fiber.config == 1
    assert loader.entries["b"].parsed.config == 1
    assert trace == ["a:1", "b:1", "a:stop:1", "a:2", "a:stop:2", "a:1"]
    await loader.close()
    await context.aclose()


@pytest.mark.asyncio
async def test_config_only_update_reuses_the_existing_fiber() -> None:
    context = Context()
    loader = Loader(context, ModuleResolver(allowed_packages=["test_loader_runtime"]))
    await loader.mount((parsed("a", "test_loader_runtime:configurable_a", config=1),))
    entry = loader.entries["a"]
    original_fiber = entry.fiber

    await loader.update((parsed("a", "test_loader_runtime:configurable_a", config=2),))

    assert entry.fiber is original_fiber
    assert entry.fiber is not None
    assert entry.fiber.config == 2
    assert entry.version == 1
    await loader.close()
    await context.aclose()


@pytest.mark.asyncio
async def test_structural_change_replaces_the_runtime_tree() -> None:
    context = Context()
    loader = Loader(context, ModuleResolver(allowed_packages=["test_loader_runtime"]))
    initial = (parsed("a", "test_loader_runtime:configurable_a", config=1),)
    await loader.mount(initial)
    fiber = loader.entries["a"].fiber

    await loader.update((parsed("a", "test_loader_runtime:parent", config=1),))

    assert loader.entries["a"].fiber is not fiber
    assert fiber is not None
    assert fiber.state is FiberState.DISPOSED
    assert loader.entries["a"].fiber is not None
    assert loader.entries["a"].fiber.state is FiberState.ACTIVE
    await loader.close()
    await context.aclose()


@pytest.mark.asyncio
async def test_failed_replacement_restores_previous_tree() -> None:
    context = Context()
    loader = Loader(context, ModuleResolver(allowed_packages=["test_loader_runtime"]))
    initial = (parsed("a", "test_loader_runtime:configurable_a", config=1),)
    await loader.mount(initial)
    old_fiber = loader.entries["a"].fiber

    with pytest.raises(LoaderError, match="failed to replace entry tree"):
        await loader.update((parsed("a", "test_loader_runtime:broken"),))

    restored = loader.entries["a"]
    assert old_fiber is not None
    assert old_fiber.state is FiberState.DISPOSED
    assert restored.parsed.module == "test_loader_runtime:configurable_a"
    assert restored.fiber is not None
    assert restored.fiber.state is FiberState.ACTIVE
    assert restored.fiber.config == 1
    await loader.close()
    await context.aclose()


@pytest.mark.asyncio
async def test_import_preflight_failure_does_not_dispose_current_tree() -> None:
    context = Context()
    loader = Loader(context, ModuleResolver(allowed_packages=["test_loader_runtime"]))
    initial = (parsed("a", "test_loader_runtime:configurable_a", config=1),)
    await loader.mount(initial)
    fiber = loader.entries["a"].fiber

    with pytest.raises(LoaderError, match="cannot resolve plugin"):
        await loader.update((parsed("a", "test_loader_runtime:missing"),))

    assert loader.entries["a"].fiber is fiber
    assert fiber is not None
    assert fiber.state is FiberState.ACTIVE
    assert trace == ["a:1"]
    await loader.close()
    await context.aclose()
