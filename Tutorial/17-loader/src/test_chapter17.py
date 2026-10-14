from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from chapter17 import (
    ConfigReloader,
    Context,
    FiberState,
    Loader,
    LoaderError,
    LoaderSecurityError,
    MappingConfigParser,
    ModuleResolver,
    ParsedEntry,
    SourceLocation,
    TomlConfigParser,
)

trace: list[str] = []


def provider(context: Context, config: object) -> object:
    context.provide("value", f"value:{config}")
    trace.append(f"provider:start:{config}")
    return lambda: trace.append(f"provider:stop:{config}")


def consumer(context: Context, config: object) -> object:
    trace.append(f"consumer:start:{context.get('value')}")
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


def configured_a(context: Context, config: object) -> object:
    trace.append(f"a:{config}")
    return lambda: trace.append(f"a:stop:{config}")


def configured_b(context: Context, config: object) -> object:
    trace.append(f"b:{config}")
    return lambda: trace.append(f"b:stop:{config}")


configured_a.Config = PositiveConfig  # type: ignore[attr-defined]
configured_b.Config = PositiveConfig  # type: ignore[attr-defined]


def make_resolver() -> ModuleResolver:
    return ModuleResolver(
        {
            "test:provider": provider,
            "test:consumer": consumer,
            "test:parent": parent,
            "test:child": child,
            "test:broken": broken,
            "test:cfg_a": configured_a,
            "test:cfg_b": configured_b,
        },
        allowed={"test"},
    )


def parsed(
    entry_id: str,
    module: str,
    *,
    config: object = None,
    disabled: bool = False,
    inject: Mapping[str, object | None] | None = None,
    children: tuple[ParsedEntry, ...] = (),
) -> ParsedEntry:
    return ParsedEntry(
        id=entry_id,
        module=module,
        config=config,
        location=SourceLocation(Path("cordis.toml")),
        disabled=disabled,
        inject={} if inject is None else inject,
        children=children,
    )


def write_config(
    source: Path,
    a: int,
    b: int,
    *,
    a_module: str = "test:cfg_a",
) -> None:
    source.write_text(
        "\n".join(
            (
                "[[plugins]]",
                'id = "a"',
                f'module = "{a_module}"',
                f"config = {a}",
                "[[plugins]]",
                'id = "b"',
                'module = "test:cfg_b"',
                f"config = {b}",
                "",
            )
        )
    )


@pytest.fixture(autouse=True)
def clear_trace() -> None:
    trace.clear()


def test_module_resolver_maps_specifiers_and_enforces_policy() -> None:
    location = SourceLocation(Path("cordis.toml"), 4, 3)
    resolver = ModuleResolver({"test:cfg_a": configured_a}, allowed={"test"})

    assert resolver.resolve("test:cfg_a", location) is configured_a
    with pytest.raises(LoaderError, match=r"cordis\.toml:4:3"):
        resolver.resolve("test:missing", location)
    with pytest.raises(LoaderSecurityError, match="is not allowed"):
        resolver.resolve("app:plugin", location)
    with pytest.raises(LoaderError, match="module:attribute"):
        resolver.resolve("no-colon", location)


def test_toml_parser_builds_immutable_nested_entries() -> None:
    text = (
        "[[plugins]]\n"
        'id = "database"\n'
        'module = "test:cfg_a"\n'
        "config = 1\n"
        "inject.value = {}\n"
        "\n"
        "[[plugins.plugins]]\n"
        'id = "child"\n'
        'module = "test:cfg_b"\n'
        'config.url = "${DATABASE_URL}"\n'
    )

    entries = TomlConfigParser().loads(
        text, Path("/project/cordis.toml"), env={"DATABASE_URL": "sqlite:///data.db"}
    )

    assert entries[0].id == "database"
    assert entries[0].location == SourceLocation(Path("/project/cordis.toml"), 3, 11)
    assert set(entries[0].inject) == {"value"}
    assert dict(entries[0].inject["value"]) == {}  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        entries[0].inject["other"] = None  # type: ignore[index]
    nested = entries[0].children[0]
    assert nested.id == "child"
    assert nested.config["url"] == "sqlite:///data.db"  # type: ignore[index]
    with pytest.raises(TypeError):
        nested.config["url"] = "other"  # type: ignore[index]

    with pytest.raises(LoaderError, match="duplicate plugin entry id 'same'"):
        MappingConfigParser().parse(
            {
                "plugins": [
                    {
                        "id": "same",
                        "module": "test:cfg_a",
                        "plugins": [
                            {"id": "same", "module": "test:cfg_b"},
                        ],
                    },
                ]
            },
            Path("/project/cordis.toml"),
            env={},
        )
    with pytest.raises(LoaderError, match="DATABASE_URL"):
        TomlConfigParser().loads(text, Path("/project/cordis.toml"), env={})


@pytest.mark.asyncio
async def test_mount_preserves_config_order_and_waits_for_dependencies() -> None:
    context = Context()
    loader = Loader(context, make_resolver())

    roots = await loader.mount(
        (
            parsed("consumer", "test:consumer", inject={"value": None}),
            parsed("provider", "test:provider", config="one"),
        )
    )

    assert [entry.id for entry in roots] == ["consumer", "provider"]
    consumer_entry = roots[0]
    assert consumer_entry.fiber is not None
    assert consumer_entry.fiber.state is FiberState.ACTIVE
    assert trace == ["provider:start:one", "consumer:start:value:one"]

    await loader.close()
    assert consumer_entry.fiber.state is FiberState.DISPOSED
    assert trace == [
        "provider:start:one",
        "consumer:start:value:one",
        "provider:stop:one",
        "consumer:stop",
    ]
    await context.aclose()


@pytest.mark.asyncio
async def test_pending_consumer_activates_when_service_arrives() -> None:
    context = Context()
    loader = Loader(context, make_resolver())
    roots = await loader.mount(
        (parsed("consumer", "test:consumer", inject={"value": None}),)
    )
    consumer_fiber = roots[0].fiber
    assert consumer_fiber is not None
    assert consumer_fiber.state is FiberState.PENDING
    assert trace == []

    provider_fiber = context.plugin(provider, "one")
    await provider_fiber.wait()
    await consumer_fiber.wait()

    assert consumer_fiber.state is FiberState.ACTIVE
    assert trace == ["provider:start:one", "consumer:start:value:one"]

    await provider_fiber.dispose()
    await consumer_fiber.wait()

    assert consumer_fiber.state is FiberState.PENDING
    assert trace[-1] == "consumer:stop"
    await loader.close()
    await context.aclose()


@pytest.mark.asyncio
async def test_children_are_owned_and_cascaded_by_parent_fiber() -> None:
    context = Context()
    loader = Loader(context, make_resolver())
    roots = await loader.mount(
        (
            parsed("parent", "test:parent", children=(parsed("child", "test:child"),)),
            parsed("off", "test:missing:plugin", disabled=True),
        )
    )

    parent_entry = roots[0]
    assert [entry.id for entry in parent_entry.children] == ["child"]
    assert trace == ["parent:start", "child:start"]
    assert roots[1].fiber is None
    assert loader.entries["off"] is roots[1]

    assert parent_entry.fiber is not None
    await parent_entry.fiber.dispose()

    nested_fiber = parent_entry.children[0].fiber
    assert nested_fiber is not None
    assert nested_fiber.state is FiberState.DISPOSED
    assert trace == ["parent:start", "child:start", "child:stop", "parent:stop"]
    await loader.close()
    await context.aclose()


@pytest.mark.asyncio
async def test_reloader_applies_config_only_update_and_replaces_on_structure_change(
    tmp_path: Path,
) -> None:
    source = tmp_path / "cordis.toml"
    write_config(source, 1, 1)
    context = Context()
    loader = Loader(context, make_resolver())
    reloader = ConfigReloader(loader, source, env={})
    await reloader.start()
    fiber_a = loader.entries["a"].fiber
    assert trace == ["a:1", "b:1"]

    assert await reloader.reload() is False
    write_config(source, 2, 1)
    assert await reloader.reload() is True

    assert loader.entries["a"].fiber is fiber_a  # config-only update reuses the Fiber
    assert fiber_a is not None
    assert fiber_a.config == 2
    assert loader.entries["a"].version == 1
    assert trace == ["a:1", "b:1", "a:stop:1", "a:2"]

    write_config(source, 1, 1, a_module="test:cfg_b")
    assert await reloader.reload() is True

    replacement = loader.entries["a"].fiber
    assert replacement is not fiber_a
    assert fiber_a.state is FiberState.DISPOSED  # structural change replaces the tree
    assert replacement is not None
    assert replacement.state is FiberState.ACTIVE
    assert loader.entries["a"].version == 0
    assert trace == [
        "a:1",
        "b:1",
        "a:stop:1",
        "a:2",
        "b:stop:1",
        "a:stop:2",
        "b:1",
        "b:1",
    ]
    await loader.close()
    await context.aclose()


@pytest.mark.asyncio
async def test_failed_config_update_rolls_back_and_keeps_the_snapshot(
    tmp_path: Path,
) -> None:
    source = tmp_path / "cordis.toml"
    write_config(source, 1, 1)
    context = Context()
    loader = Loader(context, make_resolver())
    reloader = ConfigReloader(loader, source, env={})
    await reloader.start()
    previous = reloader.current

    write_config(source, 2, 0)
    with pytest.raises(
        LoaderError, match="failed to update entry configuration"
    ) as caught:
        await reloader.reload()

    assert isinstance(caught.value.cause, ValueError)
    assert reloader.current is previous
    fiber_a = loader.entries["a"].fiber
    fiber_b = loader.entries["b"].fiber
    assert fiber_a is not None and fiber_a.config == 1
    assert fiber_b is not None and fiber_b.config == 1
    assert loader.entries["a"].version == 2  # one applied change, one rollback
    assert loader.entries["b"].error is not None
    assert trace == ["a:1", "b:1", "a:stop:1", "a:2", "a:stop:2", "a:1"]
    await loader.close()
    await context.aclose()


@pytest.mark.asyncio
async def test_failed_replacement_restores_the_previous_tree() -> None:
    context = Context()
    loader = Loader(context, make_resolver())
    await loader.mount((parsed("a", "test:cfg_a", config=1),))
    old_fiber = loader.entries["a"].fiber

    with pytest.raises(LoaderError, match="failed to replace entry tree"):
        await loader.update((parsed("a", "test:broken"),))

    assert old_fiber is not None
    assert old_fiber.state is FiberState.DISPOSED
    restored = loader.entries["a"]
    assert restored.parsed.module == "test:cfg_a"
    assert restored.fiber is not None
    assert restored.fiber.state is FiberState.ACTIVE
    assert restored.fiber.config == 1
    assert trace == ["a:1", "a:stop:1", "a:1"]
    await loader.close()
    await context.aclose()


@pytest.mark.asyncio
async def test_rejections_happen_before_the_current_tree_is_touched() -> None:
    context = Context()
    loader = Loader(context, make_resolver())
    await loader.mount((parsed("a", "test:cfg_a", config=1),))
    original = loader.entries["a"].fiber

    with pytest.raises(LoaderError, match="cannot resolve plugin"):
        await loader.update((parsed("a", "test:missing", config=1),))
    assert loader.entries["a"].fiber is original
    assert original is not None
    assert original.state is FiberState.ACTIVE
    assert trace == ["a:1"]

    with pytest.raises(RuntimeError, match="already has a mounted entry tree"):
        await loader.mount((parsed("a", "test:cfg_a", config=1),))

    duplicate_context = Context()
    duplicate_loader = Loader(duplicate_context, make_resolver())
    with pytest.raises(LoaderError, match="duplicate runtime entry id 'dup'"):
        await duplicate_loader.mount(
            (parsed("dup", "test:parent"), parsed("dup", "test:parent"))
        )
    await loader.close()
    await context.aclose()
    await duplicate_context.aclose()


@pytest.mark.asyncio
async def test_mount_failure_disposes_the_partial_tree_and_close_is_idempotent() -> (
    None
):
    context = Context()
    loader = Loader(context, make_resolver())
    tree = (
        parsed(
            "parent",
            "test:parent",
            children=(parsed("broken", "test:broken"),),
        ),
    )

    with pytest.raises(RuntimeError, match="broken child"):
        await loader.mount(tree)

    parent_entry = loader.entries["parent"]
    assert parent_entry.fiber is not None
    assert parent_entry.fiber.state is FiberState.DISPOSED
    nested_fiber = parent_entry.children[0].fiber
    assert nested_fiber is not None
    assert nested_fiber.state is FiberState.DISPOSED
    assert trace == ["parent:start", "parent:stop"]

    await loader.close()
    assert trace == ["parent:start", "parent:stop"]
    await context.aclose()
