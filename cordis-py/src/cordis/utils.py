"""Portable Cordis utility contracts."""

from __future__ import annotations

import weakref
from collections.abc import Callable, Iterator
from typing import Generic, TypeVar

T = TypeVar("T")


class DisposableList(Generic[T]):
    """Ordered values with constant-time removal by object identity."""

    def __init__(self) -> None:
        self._sn = 0
        self._map: dict[int, T] = {}
        self._weak: weakref.WeakKeyDictionary[T, int] = weakref.WeakKeyDictionary()

    @property
    def length(self) -> int:
        return len(self._map)

    def push(self, value: T) -> Callable[[], bool]:
        self._sn += 1
        serial = self._sn
        self._map[serial] = value
        self._weak[value] = serial
        return lambda: self._map.pop(serial, None) is not None

    def delete(self, value: T) -> bool:
        serial = self._weak.get(value)
        return serial is not None and self._map.pop(serial, None) is not None

    def clear(self) -> list[T]:
        values = list(reversed(self._map.values()))
        self._map.clear()
        return values

    def __iter__(self) -> Iterator[T]:
        return iter(self._map.values())
