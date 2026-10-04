#
# This file is part of pysmi software.
#
# Copyright (c) 2015-2019, Ilya Etingof <etingof@gmail.com>
# License: https://github.com/pysnmp/pysmi/blob/main/LICENSE.rst
#
"""Interface shared by the parse caches, and what a provider must guarantee."""

from typing import Any

from pysmi._aliases import deprecated_camel_case
from pysmi.mibinfo import MibInfo


@deprecated_camel_case
class AbstractParseCache:
    """Base class for parse-tree caches.

    :py:meth:`~pysmi.compiler.MibCompiler.compile` parses a module's ASN.1 text
    into a tree before generating anything from it, and the same text is
    routinely presented again -- every namespace of a corpus build imports the
    same standard modules. A cache holds those trees so the text is parsed once
    per build rather than once per namespace.

    Subclasses implement :py:meth:`get` and :py:meth:`set`.

    **What the compiler guarantees.** The key it supplies is derived from the
    ASN.1 text itself together with the identity of the parser and the pysmi
    version that would parse it. So an entry is only ever offered back for
    byte-identical text parsed by the same producer: two modules sharing a name
    across vendor trees have different keys, and an entry written by a different
    pysmi release is never read by this one. A provider does not need to reason
    about invalidation, and must not attempt its own.

    **What a provider must guarantee.** A value handed back must be equal to the
    value stored, and must not be shared with the copy the compiler already
    holds -- the compiler copies what it receives, so returning a live reference
    to a stored tree is safe, but returning a partially reconstructed one is
    not. A miss is reported by returning ``None``; that is always allowed, so a
    provider may evict, expire or fail soft at will. A cache is an optimisation
    and never a source of truth.

    **Trust.** A provider that serializes -- anything storing trees outside this
    process -- reconstructs arbitrary Python objects on read. Point one only at
    storage you control. The compiler never resolves a provider by name, from an
    entry point, or from configuration: it uses the object it was handed, so the
    trust boundary is the caller's own code.
    """

    def get(self, key: str) -> list[Any] | None:
        """The parse trees stored under *key*, or ``None`` if there are none.

        Args:
            key: opaque cache key supplied by the compiler

        Returns:
            The stored parse trees, or ``None`` on a miss.
        """
        raise NotImplementedError

    def set(self, key: str, trees: list[Any]) -> None:
        """Store *trees* under *key*.

        A provider that cannot store the value -- too large, backend
        unreachable -- should return without raising. Failing to cache is not
        a compilation failure.

        Args:
            key: opaque cache key supplied by the compiler
            trees: parse trees to store
        """
        raise NotImplementedError

    def clear(self) -> None:
        """Discard everything held.

        Never required for correctness, since keys are content-derived. It is
        here for a caller that wants the space back at a known point.
        """
        raise NotImplementedError


@deprecated_camel_case
class AbstractBuildCache:
    """Base class for compiled-product caches.

    A parse cache saves the parsing of a module's text; a *build* cache saves
    everything after it. :py:meth:`~pysmi.compiler.MibCompiler.compile` turns
    a parse tree into a rendered module -- the JSON document or pysnmp source
    a writer then stores -- and a build cache holds those products so an
    unchanged module is served rather than regenerated.

    Subclasses implement :py:meth:`get` and :py:meth:`set`.

    **What the compiler guarantees.** The key it supplies is derived from
    everything that can change the product: the semantic content of the
    source copy that was selected (its digest with ASN.1 comments stripped --
    comments never reach the parse tree, so a comment-only change is a hit in
    every output mode), the path that copy was read from, the generation
    options that shape the output, the identity and version of the code
    generator, and the semantic digests of every module this one imports,
    transitively. A TEXTUAL-CONVENTION changing its range in an imported
    module therefore invalidates every module that imports it, directly or
    not, while a comment touched anywhere invalidates nothing. A provider
    does not need to reason about invalidation, and must not attempt its own.

    **What a provider must guarantee.** A value handed back must be equal to
    the value stored, and must not be shared with the copy the compiler
    already holds -- the compiler copies what it receives, so returning a
    live reference to a stored product is safe, but returning a partially
    reconstructed one is not. A miss is reported by returning ``None``; that
    is always allowed, so a provider may evict, expire or fail soft at will.
    A cache is an optimisation and never a source of truth.

    **What is never stored.** Only a module that was generated and written
    without error is offered to :py:meth:`set`. A failed compilation stores
    nothing, and neither does a module dropped because one of its imports
    failed, so a failure can never be served back as a success.

    **Trust.** A provider that serializes -- anything storing products
    outside this process -- reconstructs arbitrary Python objects on read.
    Point one only at storage you control. The compiler never resolves a
    provider by name, from an entry point, or from configuration: it uses the
    object it was handed, so the trust boundary is the caller's own code.
    """

    def get(self, key: str) -> "tuple[MibInfo, Any] | None":
        """The product stored under *key*, or ``None`` if there is none.

        Args:
            key: opaque cache key supplied by the compiler

        Returns:
            The stored ``(MibInfo, rendered module)`` pair as a code
            generator's :py:meth:`gen_code` produced it, or ``None`` on a
            miss.
        """
        raise NotImplementedError

    def set(self, key: str, product: "tuple[MibInfo, Any]") -> None:
        """Store *product* under *key*.

        A provider that cannot store the value -- too large, backend
        unreachable -- should return without raising. Failing to cache is not
        a compilation failure.

        Args:
            key: opaque cache key supplied by the compiler
            product: the ``(MibInfo, rendered module)`` pair to store
        """
        raise NotImplementedError

    def clear(self) -> None:
        """Discard everything held.

        Never required for correctness, since keys are content-derived. It is
        here for a caller that wants the space back at a known point.
        """
        raise NotImplementedError
