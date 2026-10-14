"""Stable Cordis runtime errors."""

from __future__ import annotations

from enum import StrEnum


class CordisErrorCode(StrEnum):
    """Machine-readable runtime error codes."""

    INACTIVE_EFFECT = "INACTIVE_EFFECT"
    DUPLICATE_SERVICE = "DUPLICATE_SERVICE"
    MISSING_SERVICE = "MISSING_SERVICE"
    INVALID_PLUGIN = "INVALID_PLUGIN"
    INVALID_EFFECT = "INVALID_EFFECT"


class CordisError(RuntimeError):
    """Framework error carrying a stable code."""

    Code = CordisErrorCode

    def __init__(self, code: CordisErrorCode, message: str | None = None) -> None:
        self.code = code
        default = (
            "cannot create effect on inactive context"
            if code is CordisErrorCode.INACTIVE_EFFECT
            else code.value
        )
        super().__init__(message or default)
