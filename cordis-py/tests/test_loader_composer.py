from pathlib import Path

import pytest

from cordis.loader import ConfigComposer, LoaderError


def test_include_is_relative_and_overlay_targets_stable_id(tmp_path: Path) -> None:
    base = tmp_path / "config" / "base.toml"
    base.parent.mkdir()
    base.write_text(
        """
[[plugins]]
id = "database"
module = "myapp.database:plugin"
config.url = "sqlite:///default.db"
config.pool.size = 2
"""
    )
    root = tmp_path / "cordis.toml"
    root.write_text(
        """
include = ["config/base.toml"]

[overlays.database]
disabled = true
config.pool.size = 8

[[plugins]]
id = "agent"
module = "myapp.agent:plugin"
"""
    )

    entries = ConfigComposer().load(root, env={})

    assert [entry.id for entry in entries] == ["database", "agent"]
    database = entries[0]
    assert database.disabled is True
    assert database.location.path == base.resolve()
    assert database.location.line == 4
    assert database.config["url"] == "sqlite:///default.db"  # type: ignore[index]
    assert database.config["pool"]["size"] == 8  # type: ignore[index]


def test_include_cycle_reports_the_canonical_chain(tmp_path: Path) -> None:
    first = tmp_path / "first.toml"
    second = tmp_path / "second.toml"
    first.write_text('include = "second.toml"\n')
    second.write_text('include = "first.toml"\n')

    with pytest.raises(LoaderError, match="include cycle detected") as caught:
        ConfigComposer().load(first)

    assert str(first.resolve()) in str(caught.value)
    assert str(second.resolve()) in str(caught.value)


def test_overlay_missing_target_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "cordis.toml"
    source.write_text("[overlays.missing]\ndisabled = true\n")

    with pytest.raises(LoaderError, match="overlay target 'missing' does not exist"):
        ConfigComposer().load(source)


def test_duplicate_ids_across_includes_are_rejected(tmp_path: Path) -> None:
    one = tmp_path / "one.toml"
    two = tmp_path / "two.toml"
    one.write_text('[[plugins]]\nid = "same"\nmodule = "a:plugin"\n')
    two.write_text('[[plugins]]\nid = "same"\nmodule = "b:plugin"\n')
    root = tmp_path / "cordis.toml"
    root.write_text('include = ["one.toml", "two.toml"]\n')

    with pytest.raises(LoaderError, match="duplicate plugin entry id 'same'"):
        ConfigComposer().load(root)
