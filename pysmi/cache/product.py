#
# This file is part of pysmi software.
#
# Copyright (c) 2015-2019, Ilya Etingof <etingof@gmail.com>
# License: https://github.com/pysnmp/pysmi/blob/main/LICENSE.rst
#
"""Whole compilation products, so an unchanged module is not regenerated.

A parse cache (see :py:mod:`pysmi.cache.base`) saves re-reading the grammar
over text that was already parsed. A *product* cache sits one step later in
the pipeline: it holds what the code generator emitted for a module, so a
module whose inputs are all unchanged is not even re-generated.
"""

import logging
import os
import pickle
import tempfile
from collections import OrderedDict

from pysmi.mibinfo import MibInfo

logger = logging.getLogger(__name__)

#: What a cache holds under a key: the generated document and the
#: :py:class:`~pysmi.mibinfo.MibInfo` the generator returned with it.
Product = tuple[str, MibInfo]


class AbstractProductCache:
    """Base class for compilation-product caches.

    Subclasses implement :py:meth:`get` and :py:meth:`set`.

    **What the compiler guarantees.** The key it supplies is composed from
    everything that can change what a module compiles to:

    * the module actually selected -- the source that supplied it, named as
      the compiler records it, and the digest of its text. A source-priority
      change that selects a different copy selects different key material,
      so the stale product is never served;
    * the generation parameters that reach the code generator --
      ``genTexts``, ``textFilter`` and ``repairImports`` -- together with the
      generator's class and the pysmi version. A generator upgrade is a
      different key, so a product of the old one is never served;
    * the comment-stripped digest of every module in the transitive import
      closure. A TEXTUAL-CONVENTION redefined in an imported module changes
      that digest, so everything importing it is regenerated even though its
      own text is untouched. The closure is walked with a visited set, so an
      import *cycle* has a deterministic invalidation scope: every member of
      the cycle carries every other member in its key, and one change
      regenerates the whole cycle, never a subset of it.

    **The comment rule.** A module's own key carries the digest of its full
    text, comments included, because the generated document records that
    digest -- serving a product built from a comment-differing source would
    publish a document recording a source it was not built from. A
    *dependency's* key material is comment-stripped, because a dependency
    reaches this module's output only through its parse tree, which comments
    never enter. So a comment-only change regenerates exactly the module
    that was edited, in every output mode, and nothing that imports it.

    **What a provider must guarantee.** Same contract as
    :py:class:`~pysmi.cache.base.AbstractParseCache`: hand back what was
    stored or report a miss by returning ``None`` -- always allowed, so a
    provider may evict, expire or fail soft at will. A cache is an
    optimisation and never a source of truth.

    **Trust.** Same boundary as the parse cache: a provider that serializes
    reconstructs arbitrary Python objects on read, so point one only at
    storage you control. The compiler uses the object it was handed and never
    resolves a provider by name, from an entry point, or from configuration.
    """

    def get(self, key: str) -> Product | None:
        """The product stored under *key*, or ``None`` on a miss.

        Args:
            key: opaque cache key supplied by the compiler

        Returns:
            The stored ``(document, mibInfo)`` pair, or ``None`` on a miss.
        """
        raise NotImplementedError

    def set(self, key: str, product: Product) -> None:
        """Store *product* under *key*.

        Only ever called with the output of a *successful* generation -- a
        module that failed to compile has no product, and a module whose
        import closure could not be fully resolved has no key, so neither is
        ever offered for storage. A provider that cannot store the value
        should return without raising: failing to cache is not a compilation
        failure.

        Args:
            key: opaque cache key supplied by the compiler
            product: the ``(document, mibInfo)`` pair to store
        """
        raise NotImplementedError

    def clear(self) -> None:
        """Discard everything held.

        Never required for correctness, since keys are content-derived. It is
        here for a caller that wants the space back at a known point.
        """
        raise NotImplementedError


class InMemoryProductCache(AbstractProductCache):
    """Holds compilation products in memory for the lifetime of the object.

    The right answer for an incremental build that runs in one process --
    the edit-compile loop the product cache exists for. Nothing is
    serialized, so there is no trust boundary.

    Bounded, evicting least-recently-used. The bound should exceed the
    module count of one build; below it, a build that touches more modules
    than the cache holds evicts every entry before it can be asked for, and
    the hit rate is zero rather than reduced.

    Args:
        maxEntries: how many products to hold. A value of 0 or less means
            unbounded.
    """

    def __init__(self, maxEntries: int = 1024) -> None:
        """Create a cache bounded to *maxEntries* products."""
        self._entries: OrderedDict[str, Product] = OrderedDict()
        self._maxEntries = maxEntries

    def __repr__(self) -> str:
        """Name the class and its bound."""
        return f"{self.__class__.__name__}(maxEntries={self._maxEntries})"

    def get(self, key: str) -> Product | None:
        """The product stored under *key*, marking the entry recently used."""
        product = self._entries.get(key)

        if product is not None:
            self._entries.move_to_end(key)

        return product

    def set(self, key: str, product: Product) -> None:
        """Store *product*, evicting the least recently used to stay in bounds."""
        self._entries[key] = product
        self._entries.move_to_end(key)

        if self._maxEntries > 0:
            while len(self._entries) > self._maxEntries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        """Discard every held product."""
        self._entries.clear()


class FileProductCache(AbstractProductCache):
    """Holds compilation products as files in a directory.

    The provider for an incremental build that does not run in one process --
    a build system invoking the compiler once per change set. The directory
    survives the process, so a module whose inputs did not change is not
    regenerated across invocations.

    .. warning::

       **Reading this cache reconstructs arbitrary Python objects.** A stored
       product includes a :py:class:`~pysmi.mibinfo.MibInfo`, and reading one
       back means unpickling, which executes whatever the file says to
       execute. Point this at a directory only your own build writes. Do not
       share one between trust domains, and do not populate one from an
       artifact you did not produce.

       :py:class:`InMemoryProductCache` has no such boundary. Use this one
       when the build genuinely spans processes, and treat the directory with
       the same care as a directory of executables.

    Every entry carries the key it was stored under, checked on read, so a
    damaged, truncated or foreign file is a miss rather than a wrong answer:
    a corrupt cache costs a regeneration and never changes output. Writes are
    atomic, so a reader never sees a partial entry.

    Args:
        path: directory to hold the entries. Created if it does not exist.
    """

    def __init__(self, path: str) -> None:
        """Create a cache backed by the directory at *path*."""
        self._path = path

        os.makedirs(path, exist_ok=True)

    def __repr__(self) -> str:
        """Name the class and the directory."""
        return f"{self.__class__.__name__}({self._path!r})"

    #: Prefixed onto every entry file, so :py:meth:`clear` removes only what
    #: this cache wrote. See :py:class:`~pysmi.cache.file.FileParseCache`,
    #: which makes the same choice for the same reason.
    ENTRY_PREFIX = "pysmi-product-"

    #: Extension for entry files.
    ENTRY_SUFFIX = ".pickle"

    #: Version of the entry's own layout, distinct from anything the key
    #: already says about the product. Bumping it orphans old entries rather
    #: than misreading them.
    ENTRY_FORMAT = 1

    def _entry(self, key: str) -> str:
        """The file an entry lives in, confined to the cache directory."""
        name = self.ENTRY_PREFIX + os.path.basename(key) + self.ENTRY_SUFFIX

        return os.path.join(self._path, name)

    def get(self, key: str) -> Product | None:
        """The product stored under *key*, or ``None`` on a miss or any failure."""
        entry = self._entry(key)

        try:
            with open(entry, "rb") as fd:
                # The directory is the trust boundary, and the class docstring
                # says so: an entry is only ever one this build wrote.
                stored = pickle.load(fd)  # noqa: S301

        except FileNotFoundError:
            return None

        # Unpickling raises whatever the stored graph raises. A cache is an
        # optimisation, so every one of them is a miss.
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "discarding unreadable cache entry %s: %s",
                entry,
                exc,
                extra={"entry": entry},
            )
            return None

        # Shape and identity check, not a safety one -- by here the pickle
        # has already run. It catches an entry that is merely wrong: damaged,
        # written by something else, or filed under a name it does not belong
        # to. Any of those is a miss, never a wrong answer.
        if not isinstance(stored, dict) or stored.get("format") != self.ENTRY_FORMAT:
            logger.debug(
                "ignoring cache entry %s: not a version %d entry",
                entry,
                self.ENTRY_FORMAT,
                extra={"entry": entry},
            )
            return None

        if stored.get("key") != key:
            logger.debug(
                "ignoring cache entry %s: stored under a different key",
                entry,
                extra={"entry": entry},
            )
            return None

        product = stored.get("product")

        if (
            not isinstance(product, tuple)
            or len(product) != 2
            or not isinstance(product[0], str)
            or not isinstance(product[1], MibInfo)
        ):
            logger.debug(
                "ignoring cache entry %s: unexpected content",
                entry,
                extra={"entry": entry},
            )
            return None

        return product

    def set(self, key: str, product: Product) -> None:
        """Store *product*, atomically, so a reader never sees a partial write."""
        entry = self._entry(key)

        try:
            fd, temporary = tempfile.mkstemp(dir=self._path, suffix=".partial")

            try:
                with os.fdopen(fd, "wb") as handle:
                    pickle.dump(
                        {
                            "format": self.ENTRY_FORMAT,
                            "key": key,
                            "product": product,
                        },
                        handle,
                        protocol=pickle.HIGHEST_PROTOCOL,
                    )

                os.replace(temporary, entry)

            except BaseException:
                try:
                    os.unlink(temporary)

                except OSError:
                    pass

                raise

        # Failing to store is not a compilation failure: a full disk or a
        # read-only directory just means no entry.
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not cache %s: %s", entry, exc, extra={"entry": entry})

    def clear(self) -> None:
        """Remove every entry this cache wrote, and nothing else."""
        try:
            names = os.listdir(self._path)

        except OSError:
            return

        for name in names:
            if not (
                name.startswith(self.ENTRY_PREFIX) and name.endswith(self.ENTRY_SUFFIX)
            ):
                continue

            try:
                os.unlink(os.path.join(self._path, name))

            except OSError:
                pass
