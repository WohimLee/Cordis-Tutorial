"""Convenience base class for named services."""

from __future__ import annotations

import contextvars
import copy
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from .context import Context

_current_context: contextvars.ContextVar[Context | None] = contextvars.ContextVar(
    "cordis_service_context",
    default=None,
)


class ServiceView:
    """Forward a Service while binding method calls to a consumer Context."""

    __slots__ = ("_context", "_target")

    def __init__(self, target: Service, context: Context) -> None:
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_context", context)

    def __getattr__(self, name: str) -> object:
        value = getattr(self._target, name)
        if not callable(value):
            return value
        callback = value

        def bound(*args: object, **kwargs: object) -> object:
            token = _current_context.set(self._context)
            try:
                return callback(*args, **kwargs)
            finally:
                _current_context.reset(token)

        return bound

    def __setattr__(self, name: str, value: object) -> None:
        setattr(self._target, name, value)

    def __call__(self, *args: object, **kwargs: object) -> object:
        token = _current_context.set(self._context)
        try:
            callback = cast(Callable[..., object], self._target)
            return callback(*args, **kwargs)
        finally:
            _current_context.reset(token)


class Service:
    """Register an instance under a stable Context service name."""

    provide: str | None = None
    Config: object | None = None

    def __init__(self, context: Context, name: str | None = None) -> None:
        self.context = context
        self.name = name or self.provide or type(self).__name__.lower()
        context.provide(self.name, self, self.available)

    def available(self) -> bool:
        """Return whether strict consumers may use this service."""

        return True

    @property
    def caller_context(self) -> Context:
        """Context through which the current service method was invoked."""

        return _current_context.get() or self.context

    def extend(self, **properties: object) -> Service:
        """Create an unregistered derived service sharing this instance's state."""

        service = copy.copy(self)
        vars(service).update(properties)
        return service

    def resolve_config(
        self,
        base: Mapping[str, object] | None = None,
        head: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Merge service intercept configuration from outer to inner Contexts."""

        context = _current_context.get() or self.context
        configs: list[Mapping[str, object]] = []
        for config in (base, *context.intercepts.get(self.name, ()), head):
            if config is None:
                continue
            if not isinstance(config, Mapping):
                raise TypeError(f"intercept config for {self.name!r} must be a mapping")
            raw_mapping = cast(Mapping[object, object], config)
            if not all(isinstance(key, str) for key in raw_mapping):
                raise TypeError(f"intercept config for {self.name!r} requires string keys")
            configs.append(cast(Mapping[str, object], raw_mapping))
        merge = getattr(self.Config, "merge", None)
        if callable(merge):
            return cast(dict[str, object], merge(*configs))
        result: dict[str, object] = {}
        for config in configs:
            result.update(config)
        return result


def bind_service(value: object, context: Context) -> object:
    """Bind a Service value to the Context through which it was resolved."""

    if isinstance(value, Service):
        return ServiceView(value, context)
    return value
