"""Public structural types for plugin authors and integrations."""

from __future__ import annotations

from typing import Protocol, TypeAlias

from .effect import EffectResult


class PluginContext(Protocol):
    """Minimum Context surface passed to plugin entrypoints."""

    def get(self, name: str) -> object:
        """Resolve a named service."""


class PluginFunction(Protocol):
    """Structural type for function plugins."""

    def __call__(self, context: PluginContext, config: object) -> EffectResult:
        """Activate the plugin and optionally return cleanup work."""


class PluginObject(Protocol):
    """Structural type for object plugins."""

    def apply(self, context: PluginContext, config: object) -> EffectResult:
        """Activate the plugin and optionally return cleanup work."""


EventResult: TypeAlias = object
