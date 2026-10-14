from pathlib import Path

import pytest

from cordis.loader import LoaderError, LoaderSecurityError, ModuleResolver, SourceLocation


def test_resolves_allow_listed_package_plugin() -> None:
    resolver = ModuleResolver(allowed_packages=["cordis"])
    location = SourceLocation(Path("config.toml"))

    plugin = resolver.resolve("cordis.service:Service", location)

    assert getattr(plugin, "__name__", None) == "Service"


def test_rejects_package_outside_allow_list() -> None:
    resolver = ModuleResolver(allowed_packages=["myapp"])
    location = SourceLocation(Path("config.toml"), 3, 4)

    with pytest.raises(LoaderSecurityError, match=r"config\.toml:3:4"):
        resolver.resolve("cordis.service:Service", location)


def test_relative_module_resolves_from_config_directory(tmp_path: Path) -> None:
    plugin_path = tmp_path / "plugin.py"
    plugin_path.write_text("def apply(ctx, config):\n    return None\n")
    location = SourceLocation(tmp_path / "cordis.toml", 2, 1)
    resolver = ModuleResolver(allowed_paths=[tmp_path])

    plugin = resolver.resolve("./plugin.py:apply", location)

    assert getattr(plugin, "__name__", None) == "apply"


def test_missing_attribute_reports_source_location() -> None:
    resolver = ModuleResolver(allowed_packages=["cordis"])
    location = SourceLocation(Path("cordis.toml"), 8, 2)

    with pytest.raises(LoaderError, match=r"cordis\.toml:8:2"):
        resolver.resolve("cordis.service:missing", location)
