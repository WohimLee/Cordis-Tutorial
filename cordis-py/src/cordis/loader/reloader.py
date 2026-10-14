"""Host-driven configuration reload adapter."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .composer import ConfigComposer
from .model import Entry, ParsedEntry
from .runtime import Loader


class ConfigReloader:
    """Recompose a source file and apply changes through Loader transactions."""

    def __init__(
        self,
        loader: Loader,
        source: Path,
        *,
        composer: ConfigComposer | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.loader = loader
        self.source = source.resolve()
        self.composer = composer or ConfigComposer()
        self.env = env
        self.current: tuple[ParsedEntry, ...] | None = None

    async def start(self) -> tuple[Entry, ...]:
        """Load and mount the initial configuration."""

        parsed = self.composer.load(self.source, env=self.env)
        roots = await self.loader.mount(parsed)
        self.current = parsed
        return roots

    async def reload(self) -> bool:
        """Apply a changed configuration, returning whether it differed."""

        if self.current is None:
            raise RuntimeError("config reloader has not been started")
        candidate = self.composer.load(self.source, env=self.env)
        if candidate == self.current:
            return False
        await self.loader.update(candidate)
        self.current = candidate
        return True
