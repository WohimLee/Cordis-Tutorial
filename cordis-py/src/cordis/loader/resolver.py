"""Policy-controlled Python plugin module resolution."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from types import ModuleType
from typing import cast

from cordis.model import Plugin

from .errors import LoaderError, LoaderSecurityError, SourceLocation

ModulePolicy = Callable[[str, Path | None], bool]


class ModuleResolver:
    """Resolve `module:attribute` references after applying an allow policy."""

    def __init__(
        self,
        *,
        allowed_packages: Iterable[str] = (),
        allowed_paths: Iterable[Path] = (),
        policy: ModulePolicy | None = None,
    ) -> None:
        self.allowed_packages = tuple(allowed_packages)
        self.allowed_paths = tuple(path.resolve() for path in allowed_paths)
        self.policy = policy

    def resolve(self, specifier: str, location: SourceLocation) -> Plugin:
        module_name, separator, attribute = specifier.partition(":")
        if not separator or not module_name or not attribute:
            raise LoaderError(
                "plugin reference must use 'module:attribute' syntax",
                location,
            )

        source_path = self._source_path(module_name, location.path.parent)
        if not self._allowed(module_name, source_path):
            raise LoaderSecurityError(f"plugin module {module_name!r} is not allowed", location)

        try:
            module = (
                self._load_path(source_path)
                if source_path is not None
                else importlib.import_module(module_name)
            )
            value: object = module
            for part in attribute.split("."):
                value = getattr(value, part)
        except Exception as error:
            raise LoaderError(
                f"cannot resolve plugin {specifier!r}: {error}",
                location,
                cause=error,
            ) from error
        if not callable(value) and not callable(getattr(value, "apply", None)):
            raise LoaderError(f"resolved object {specifier!r} is not a plugin", location)
        return cast(Plugin, value)

    def _allowed(self, module_name: str, source_path: Path | None) -> bool:
        if self.policy is not None:
            return self.policy(module_name, source_path)
        if source_path is not None:
            return any(source_path.is_relative_to(root) for root in self.allowed_paths)
        return any(
            module_name == package or module_name.startswith(f"{package}.")
            for package in self.allowed_packages
        )

    @staticmethod
    def _source_path(module_name: str, base: Path) -> Path | None:
        if not module_name.startswith("."):
            return None
        relative = module_name.removeprefix("./")
        candidate = (base / relative).resolve()
        if candidate.suffix == "":
            candidate = candidate.with_suffix(".py")
        return candidate

    @staticmethod
    def _load_path(path: Path) -> ModuleType:
        key = f"_cordis_loader_{abs(hash(path))}"
        spec = importlib.util.spec_from_file_location(key, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot create an import spec for {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(key, None)
            raise
        return module
