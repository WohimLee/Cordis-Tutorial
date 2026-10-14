from __future__ import annotations

import json
from pathlib import Path

import pytest
from chapter18 import (
    ComposedEntry,
    ComposerError,
    CompositeRuntime,
    ConfigComposer,
    ConfigSession,
    JsonMappingAdapter,
    SourceLocation,
    TomlMappingAdapter,
)

BASE_TOML = """\
[[plugins]]
id = "database"
module = "app.database:plugin"
config.url = "sqlite:///default.db"
config.pool.size = 2
"""


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def shape(entries: tuple[ComposedEntry, ...]) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            entry.id,
            entry.module,
            entry.config,
            entry.disabled,
            dict(entry.inject),
            shape(entry.children),
        )
        for entry in entries
    )


class StaticAdapter:
    """Adapter that returns a mapping, proving the composer never parses text."""

    def __init__(self, document: object) -> None:
        self.document = document

    def read(self, source: Path) -> object:
        return self.document


class PositionedAdapter:
    """A position-aware adapter, as a YAML parser adapter would be."""

    def read(self, source: Path) -> dict[str, object]:
        entries: list[dict[str, object]] = []
        for number, line in enumerate(source.read_text().splitlines(), 1):
            if line.startswith("plugin "):
                _, entry_id, module = line.split()
                entries.append(
                    {"id": entry_id, "module": module, "_cordis_line": number}
                )
        return {"plugins": entries}


def test_include_is_relative_and_recursive_across_directories(tmp_path: Path) -> None:
    shared = write(tmp_path / "shared" / "base.toml", BASE_TOML)
    config = write(
        tmp_path / "config" / "db.toml",
        'include = "../shared/base.toml"\n\n'
        "[[plugins]]\n"
        'id = "cache"\n'
        'module = "app.cache:plugin"\n',
    )
    root = write(
        tmp_path / "cordis.toml",
        'include = ["config/db.toml"]\n\n'
        "[[plugins]]\n"
        'id = "agent"\n'
        'module = "app.agent:plugin"\n',
    )

    entries = ConfigComposer(TomlMappingAdapter()).load(root)

    assert [entry.id for entry in entries] == ["database", "cache", "agent"]
    assert entries[0].location == SourceLocation(shared.resolve())
    assert entries[1].location == SourceLocation(config.resolve())
    assert entries[2].location == SourceLocation(root.resolve())
    assert entries[0].config["pool"]["size"] == 2  # type: ignore[index]


def test_cycle_chain_is_reported_with_canonical_paths(tmp_path: Path) -> None:
    first = write(tmp_path / "first.toml", 'include = "second.toml"\n')
    second = write(tmp_path / "second.toml", 'include = "nested/third.toml"\n')
    third = write(tmp_path / "nested" / "third.toml", 'include = "../first.toml"\n')

    with pytest.raises(ComposerError) as caught:
        ConfigComposer(TomlMappingAdapter()).load(first)

    message = str(caught.value)
    assert "include cycle detected" in message
    assert caught.value.location == SourceLocation(first.resolve())
    for path in (first, second, third):
        assert str(path.resolve()) in message
    assert message.index(str(first.resolve())) < message.index(str(second.resolve()))
    assert message.index(str(second.resolve())) < message.index(str(third.resolve()))


def test_duplicate_entry_id_across_includes_reports_the_second_source(
    tmp_path: Path,
) -> None:
    write(tmp_path / "one.toml", '[[plugins]]\nid = "same"\nmodule = "a:plugin"\n')
    second = write(
        tmp_path / "two.toml", '[[plugins]]\nid = "same"\nmodule = "b:plugin"\n'
    )
    root = write(tmp_path / "cordis.toml", 'include = ["one.toml", "two.toml"]\n')

    with pytest.raises(ComposerError) as caught:
        ConfigComposer(TomlMappingAdapter()).load(root)

    assert "duplicate plugin entry id 'same'" in str(caught.value)
    assert caught.value.location == SourceLocation(second.resolve())


def test_overlay_patches_by_entry_id_and_deep_merges(tmp_path: Path) -> None:
    write(tmp_path / "config" / "base.toml", BASE_TOML)
    root = write(
        tmp_path / "cordis.toml",
        'include = ["config/base.toml"]\n\n'
        "[overlays.database]\n"
        "disabled = true\n"
        'module = "app.database:v2"\n'
        "config.pool.size = 8\n\n"
        "[[plugins]]\n"
        'id = "agent"\n'
        'module = "app.agent:plugin"\n',
    )

    entries = ConfigComposer(TomlMappingAdapter()).load(root)

    database = entries[0]
    assert database.disabled is True
    assert database.module == "app.database:v2"
    assert database.config == {  # type: ignore[typeddict-item]
        "url": "sqlite:///default.db",
        "pool": {"size": 8},
    }
    assert database.location == SourceLocation(root.resolve())
    assert entries[1].module == "app.agent:plugin"


def test_overlay_rejects_missing_target_and_unknown_field(tmp_path: Path) -> None:
    composer = ConfigComposer(TomlMappingAdapter())
    missing = write(tmp_path / "missing.toml", "[overlays.absent]\ndisabled = true\n")
    unknown = write(
        tmp_path / "unknown.toml",
        '[[plugins]]\nid = "app"\nmodule = "a:plugin"\n[overlays.app]\nunknown = 1\n',
    )

    with pytest.raises(ComposerError, match="overlay target 'absent' does not exist"):
        composer.load(missing)
    with pytest.raises(ComposerError, match="unsupported field 'unknown'"):
        composer.load(unknown)
    with pytest.raises(ComposerError, match="'overlays' must be a mapping"):
        ConfigComposer(StaticAdapter({"plugins": [], "overlays": 1})).load(
            Path("/virtual/cordis.toml")
        )


def test_toml_and_json_adapters_feed_the_same_composer(tmp_path: Path) -> None:
    toml_source = write(
        tmp_path / "same.toml",
        "[[plugins]]\n"
        'id = "app"\n'
        'module = "app.main:plugin"\n'
        'config.url = "x"\n'
        "inject.db = {}\n",
    )
    json_source = write(
        tmp_path / "same.json",
        json.dumps(
            {
                "plugins": [
                    {
                        "id": "app",
                        "module": "app.main:plugin",
                        "config": {"url": "x"},
                        "inject": {"db": {}},
                    }
                ]
            }
        ),
    )

    from_toml = ConfigComposer(TomlMappingAdapter()).load(toml_source)
    from_json = ConfigComposer(JsonMappingAdapter()).load(json_source)

    assert shape(from_toml) == shape(from_json)
    assert from_toml[0].location.path == toml_source.resolve()
    assert from_json[0].location.path == json_source.resolve()


def test_adapter_reported_positions_drive_locations_and_errors(
    tmp_path: Path,
) -> None:
    source = write(tmp_path / "cordis.conf", "# comment\nplugin app ./app.py\n")
    composer = ConfigComposer(PositionedAdapter())

    entries = composer.load(source)

    assert entries[0].location == SourceLocation(source.resolve(), 2)
    assert str(entries[0].location) == f"{source.resolve()}:2"

    duplicate = write(
        tmp_path / "duplicate.conf",
        "# comment\nplugin app ./app.py\nplugin app ./other.py\n",
    )
    with pytest.raises(ComposerError) as caught:
        composer.load(duplicate)

    assert caught.value.location == SourceLocation(duplicate.resolve(), 3)
    assert str(caught.value).startswith(f"{duplicate.resolve()}:3")


def test_reload_applies_added_and_removed_entries(tmp_path: Path) -> None:
    base = write(tmp_path / "base.toml", BASE_TOML)
    root = write(
        tmp_path / "cordis.toml",
        'include = ["base.toml"]\n\n'
        "[[plugins]]\n"
        'id = "agent"\n'
        'module = "app.agent:plugin"\n',
    )
    runtime = CompositeRuntime()
    session = ConfigSession(ConfigComposer(TomlMappingAdapter()), runtime, root)

    session.load()
    assert runtime.ids == ("database", "agent")

    write(
        base,
        '[[plugins]]\nid = "cache"\nmodule = "app.cache:plugin"\n',
    )
    assert session.reload() is True
    assert runtime.ids == ("cache", "agent")
    assert runtime.activations == [
        "mount:database",
        "mount:agent",
        "mount:cache",
        "mount:agent",
    ]

    assert session.reload() is False  # identical content is not reinstalled
    assert len(runtime.activations) == 4


def test_composition_failure_leaves_the_runtime_tree_unchanged(
    tmp_path: Path,
) -> None:
    write(tmp_path / "one.toml", '[[plugins]]\nid = "same"\nmodule = "a:plugin"\n')
    second = write(
        tmp_path / "two.toml", '[[plugins]]\nid = "other"\nmodule = "b:plugin"\n'
    )
    root = write(tmp_path / "cordis.toml", 'include = ["one.toml", "two.toml"]\n')
    runtime = CompositeRuntime()
    session = ConfigSession(ConfigComposer(TomlMappingAdapter()), runtime, root)
    session.load()
    previous_snapshot = session.snapshot
    previous_activations = list(runtime.activations)

    write(second, '[[plugins]]\nid = "same"\nmodule = "b:plugin"\n')
    with pytest.raises(ComposerError, match="duplicate plugin entry id 'same'"):
        session.reload()

    assert runtime.ids == ("same", "other")
    assert runtime.activations == previous_activations
    assert session.snapshot is previous_snapshot


def test_missing_include_reports_the_commanding_chain(tmp_path: Path) -> None:
    root = write(tmp_path / "cordis.toml", 'include = "nested/missing.toml"\n')
    missing = (tmp_path / "nested" / "missing.toml").resolve()

    with pytest.raises(ComposerError) as caught:
        ConfigComposer(TomlMappingAdapter()).load(root)

    assert caught.value.location == SourceLocation(missing)
    assert "include chain" in str(caught.value)
    assert str(root.resolve()) in str(caught.value)
    assert str(missing) in str(caught.value)
    assert isinstance(caught.value.cause, OSError)
