from pathlib import Path

import pytest

from cordis import Context
from cordis.loader import ConfigReloader, Loader, LoaderError, ModuleResolver

activations: list[int] = []


class PositiveConfig:
    @staticmethod
    def validate(value: object) -> int:
        if not isinstance(value, int) or value <= 0:
            raise ValueError("expected positive config")
        return value


def configured(context: Context, config: object) -> None:
    assert isinstance(config, int)
    activations.append(config)


configured.Config = PositiveConfig  # type: ignore[attr-defined]


def write_config(path: Path, value: int) -> None:
    path.write_text(
        "\n".join(
            (
                "[[plugins]]",
                'id = "configured"',
                'module = "test_loader_reloader:configured"',
                f"config = {value}",
            )
        )
    )


@pytest.fixture(autouse=True)
def clear_activations() -> None:
    activations.clear()


@pytest.mark.asyncio
async def test_reloader_applies_config_changes_and_skips_identical_content(
    tmp_path: Path,
) -> None:
    source = tmp_path / "cordis.toml"
    write_config(source, 1)
    context = Context()
    loader = Loader(context, ModuleResolver(allowed_packages=["test_loader_reloader"]))
    reloader = ConfigReloader(loader, source, env={})
    await reloader.start()

    assert await reloader.reload() is False
    write_config(source, 2)
    assert await reloader.reload() is True
    assert activations == [1, 2]
    await loader.close()
    await context.aclose()


@pytest.mark.asyncio
async def test_invalid_reload_preserves_previous_runtime_and_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "cordis.toml"
    write_config(source, 1)
    context = Context()
    loader = Loader(context, ModuleResolver(allowed_packages=["test_loader_reloader"]))
    reloader = ConfigReloader(loader, source, env={})
    await reloader.start()
    previous = reloader.current

    write_config(source, 0)
    with pytest.raises(LoaderError, match="failed to update entry configuration"):
        await reloader.reload()

    assert reloader.current is previous
    assert loader.entries["configured"].fiber is not None
    assert loader.entries["configured"].fiber.config == 1
    await loader.close()
    await context.aclose()
