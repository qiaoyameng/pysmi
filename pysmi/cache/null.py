#
# This file is part of pysmi software.
#
# Copyright (c) 2015-2019, Ilya Etingof <etingof@gmail.com>
# License: https://github.com/pysnmp/pysmi/blob/main/LICENSE.rst
#
"""Caches that hold nothing, for turning caching off."""

from typing import Any

from pysmi.cache.base import AbstractBuildCache, AbstractParseCache
from pysmi.mibinfo import MibInfo


class NullParseCache(AbstractParseCache):
    """Stores nothing and always misses.

    Every module is parsed each time it is presented, which is the behaviour
    of every pysmi release before the cache existed. Use it to take the cache
    out of the picture when measuring, or where holding trees is not wanted.
    """

    def __repr__(self) -> str:
        """Name the class."""
        return f"{self.__class__.__name__}()"

    def get(self, key: str) -> list[Any] | None:
        """Always a miss."""
        return None

    def set(self, key: str, trees: list[Any]) -> None:
        """Discards *trees*."""

    def clear(self) -> None:
        """Nothing to discard."""


class NullBuildCache(AbstractBuildCache):
    """Stores nothing and always misses.

    Every module is regenerated each time its product is needed, which is the
    behaviour of every pysmi release before the build cache existed. It is
    the default: product caching is opt-in, because reusing a rendered module
    is a stronger statement than reusing a parse tree, and the caller opting
    in is the caller who knows the build wants it.
    """

    def __repr__(self) -> str:
        """Name the class."""
        return f"{self.__class__.__name__}()"

    def get(self, key: str) -> "tuple[MibInfo, Any] | None":
        """Always a miss."""
        return None

    def set(self, key: str, product: "tuple[MibInfo, Any]") -> None:
        """Discards *product*."""

    def clear(self) -> None:
        """Nothing to discard."""
