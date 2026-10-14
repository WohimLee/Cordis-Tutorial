import pytest

import cordis
from cordis import Context, CordisError, CordisErrorCode, Inject, Property, isBailed, resolveConfig
from cordis.loader import ConfigReloader, Loader, YamlConfigParser


def test_package_is_importable() -> None:
    assert cordis.__version__ == "0.1.0"
    assert Loader.__module__ == "cordis.loader.runtime"
    assert ConfigReloader.__module__ == "cordis.loader.reloader"
    assert YamlConfigParser.__module__ == "cordis.loader.parser"


@pytest.mark.asyncio
async def test_canonical_portable_exports() -> None:
    context = Context()
    assert [isBailed(value) for value in (None, False, True, 0, "")] == [
        False,
        False,
        True,
        True,
        True,
    ]
    assert Inject.resolve(["a", "b"]) == {"a": None, "b": None}
    assert Property.Service(type="service") == {"type": "service"}
    assert Property.Accessor(get=lambda _receiver: 1)["get"](context) == 1
    assert CordisError.Code is CordisErrorCode
    assert str(CordisError(CordisError.Code.INACTIVE_EFFECT)) == (
        "cannot create effect on inactive context"
    )

    class Validator:
        def validate(self, value: object) -> object:
            return {"value": value}

    def plugin(_context: Context, _config: object) -> None:
        return None

    plugin.Config = Validator()  # type: ignore[attr-defined]
    fiber = context.plugin(plugin, "input")
    assert await fiber is fiber
    runtime = context.registry.get(plugin)
    assert runtime is not None
    assert resolveConfig(runtime, "input") == {"value": "input"}
    await fiber.dispose()
    await context.aclose()
