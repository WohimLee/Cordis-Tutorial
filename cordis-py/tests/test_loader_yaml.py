from pathlib import Path

import pytest

from cordis.loader import ConfigComposer, LoaderError, YamlConfigParser


def test_yaml_frontend_reuses_entry_validation_and_env_interpolation() -> None:
    text = """
plugins:
  - id: database
    module: ./database.py:plugin
    config:
      url: ${DATABASE_URL}
"""

    entries = YamlConfigParser().loads(
        text,
        Path("cordis.yaml"),
        env={"DATABASE_URL": "sqlite:///data.db"},
    )

    assert entries[0].id == "database"
    assert entries[0].config["url"] == "sqlite:///data.db"  # type: ignore[index]
    assert entries[0].location.line == 4


def test_yaml_python_tags_are_rejected_without_execution(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    text = f"!!python/object/apply:pathlib.Path.touch ['{marker}']"

    with pytest.raises(LoaderError, match="invalid YAML"):
        YamlConfigParser().loads(text, tmp_path / "cordis.yaml")

    assert not marker.exists()


def test_yaml_can_include_toml_relative_to_its_directory(tmp_path: Path) -> None:
    child = tmp_path / "nested" / "child.toml"
    child.parent.mkdir()
    child.write_text('[[plugins]]\nid = "child"\nmodule = "app.child:plugin"\n')
    root = tmp_path / "cordis.yaml"
    root.write_text("include: nested/child.toml\nplugins: []\n")

    entries = ConfigComposer().load(root, env={})

    assert entries[0].id == "child"
    assert entries[0].location.path == child.resolve()
