"""Source-aware failures raised by the declarative Loader."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SourceLocation:
    """A location in a configuration source."""

    path: Path
    line: int = 1
    column: int = 1

    def __str__(self) -> str:
        return f"{self.path}:{self.line}:{self.column}"


class LoaderError(Exception):
    """A Loader error with an optional source location and causal exception."""

    def __init__(
        self,
        message: str,
        location: SourceLocation | None = None,
        *,
        cause: BaseException | None = None,
    ) -> None:
        self.message = message
        self.location = location
        self.cause = cause
        detail = f"{location}: {message}" if location is not None else message
        super().__init__(detail)


class LoaderSecurityError(LoaderError):
    """A module specifier rejected by the import policy."""
