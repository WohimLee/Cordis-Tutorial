from pathlib import Path

import pytest

from cordis.loader import LoaderError, TomlConfigParser


def test_toml_parser_builds_immutable_nested_entries_and_interpolates_env() -> None:
    text = """
[[plugins]]
id = "database"
module = "./database.py:plugin"
config.url = "${DATABASE_URL}"

[[plugins.plugins]]
id = "child"
module = "myapp.child:plugin"
inject.database = {}
"""

    entries = TomlConfigParser().loads(
        text,
        Path("/project/cordis.toml"),
        env={"DATABASE_URL": "sqlite:///data.db"},
    )

    assert entries[0].id == "database"
    assert entries[0].config["url"] == "sqlite:///data.db"  # type: ignore[index]
    assert entries[0].children[0].id == "child"
    assert entries[0].location.line == 4


def test_duplicate_ids_are_rejected_with_source() -> None:
    text = """
[[plugins]]
id = "same"
module = "a:plugin"
[[plugins]]
id = "same"
module = "b:plugin"
"""

    with pytest.raises(LoaderError, match="duplicate plugin entry id"):
        TomlConfigParser().loads(text, Path("cordis.toml"))


def test_missing_environment_variable_is_actionable() -> None:
    text = """
[[plugins]]
id = "database"
module = "a:plugin"
config.url = "${DATABASE_URL}"
"""

    with pytest.raises(LoaderError, match="DATABASE_URL"):
        TomlConfigParser().loads(text, Path("cordis.toml"), env={})


def test_toml_never_evaluates_python_expressions() -> None:
    text = """
[[plugins]]
id = "safe"
module = "a:plugin"
config.value = "__import__('os').getcwd()"
"""

    entries = TomlConfigParser().loads(text, Path("cordis.toml"), env={})

    assert entries[0].config["value"] == "__import__('os').getcwd()"  # type: ignore[index]
