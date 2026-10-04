#
# This file is part of pysmi software.
#
# Copyright (c) 2005-2019, Ilya Etingof deceased
#
"""Compiled products are reused only when nothing they are made of changed.

A parse cache saves re-reading the grammar; a *build cache* saves everything
after it. These tests drive one compiler over a small source set -- three
modules that import each other in a cycle, plus one that imports nothing
unusual -- and record every parser and code generator invocation, so that
each kind of change is pinned to exactly the set of products it must
rebuild and the set it must leave alone:

* no change at all reuses every product;
* a TEXTUAL-CONVENTION changing its range rebuilds the whole import cycle
  that sees it, though only one module's text changed, and leaves the
  unrelated module alone;
* a comment-only change re-parses the touched module (the parse cache keys
  on the text) but regenerates nothing (comments never reach the parse
  tree, so the build cache keys on what the text *says*);
* a source-priority switch, a generator version change and a damaged cache
  entry all miss rather than serve the wrong product;
* a failed compilation publishes nothing, and neither does a module dropped
  because its import failed.
"""

import hashlib
import json
import os
import pickle
import shutil
import tempfile
import textwrap
import unittest
from unittest import mock

from pysmi import compiler as compiler_module
from pysmi import error
from pysmi.cache import (
    AbstractBuildCache,
    FileBuildCache,
    InMemoryBuildCache,
    NullBuildCache,
)
from pysmi.codegen import JsonCodeGen
from pysmi.compiler import MibCompiler
from pysmi.parser import SmiV1CompatParser
from pysmi.reader import CallbackReader, FileReader
from pysmi.writer import CallbackWriter

#: The bundled modules every compile below pulls in, and which a correct
#: cache therefore reuses on every rebuild.
BUNDLED = ("SNMPv2-SMI", "SNMPv2-TC", "SNMPv2-CONF")

CYCLE = ("CYCLE-A-MIB", "CYCLE-B-MIB", "CYCLE-C-MIB")


def cycle_a(syntax_range="(0..10)"):
    """The head of the cycle: imports from B, exports the TC C builds on."""
    return textwrap.dedent(
        f"""\
        CYCLE-A-MIB DEFINITIONS ::= BEGIN
        IMPORTS
            MODULE-IDENTITY, OBJECT-TYPE, enterprises FROM SNMPv2-SMI
            TEXTUAL-CONVENTION FROM SNMPv2-TC
            BType FROM CYCLE-B-MIB;

        AType ::= TEXTUAL-CONVENTION
            STATUS current
            DESCRIPTION "a type"
            SYNTAX INTEGER {syntax_range}

        cycleA MODULE-IDENTITY
            LAST-UPDATED "202401010000Z"
            ORGANIZATION "test"
            CONTACT-INFO "test"
            DESCRIPTION "cycle a"
            ::= {{ enterprises 9001 }}

        aObj OBJECT-TYPE
            SYNTAX BType
            MAX-ACCESS read-only
            STATUS current
            DESCRIPTION "a"
            ::= {{ cycleA 1 }}
        END
        """
    )


def cycle_b():
    """The middle of the cycle: imports C's type, exports its own to A."""
    return textwrap.dedent(
        """\
        CYCLE-B-MIB DEFINITIONS ::= BEGIN
        IMPORTS
            MODULE-IDENTITY, enterprises FROM SNMPv2-SMI
            TEXTUAL-CONVENTION FROM SNMPv2-TC
            CType FROM CYCLE-C-MIB;

        BType ::= TEXTUAL-CONVENTION
            STATUS current
            DESCRIPTION "b type"
            SYNTAX CType

        cycleB MODULE-IDENTITY
            LAST-UPDATED "202401010000Z"
            ORGANIZATION "test"
            CONTACT-INFO "test"
            DESCRIPTION "cycle b"
            ::= { enterprises 9002 }
        END
        """
    )


def cycle_c():
    """The back edge of the cycle: imports A's type, closing the loop."""
    return textwrap.dedent(
        """\
        CYCLE-C-MIB DEFINITIONS ::= BEGIN
        IMPORTS
            MODULE-IDENTITY, enterprises FROM SNMPv2-SMI
            TEXTUAL-CONVENTION FROM SNMPv2-TC
            AType FROM CYCLE-A-MIB;

        CType ::= TEXTUAL-CONVENTION
            STATUS current
            DESCRIPTION "c type"
            SYNTAX AType

        cycleC MODULE-IDENTITY
            LAST-UPDATED "202401010000Z"
            ORGANIZATION "test"
            CONTACT-INFO "test"
            DESCRIPTION "cycle c"
            ::= { enterprises 9003 }
        END
        """
    )


def solo(description="solo"):
    """A module outside the cycle, importing only the bundled tree."""
    return textwrap.dedent(
        f"""\
        SOLO-MIB DEFINITIONS ::= BEGIN
        IMPORTS
            MODULE-IDENTITY, OBJECT-TYPE, Integer32, enterprises
                FROM SNMPv2-SMI;

        solo MODULE-IDENTITY
            LAST-UPDATED "202401010000Z"
            ORGANIZATION "test"
            CONTACT-INFO "test"
            DESCRIPTION "{description}"
            ::= {{ enterprises 9004 }}

        soloObj OBJECT-TYPE
            SYNTAX Integer32
            MAX-ACCESS read-only
            STATUS current
            DESCRIPTION "{description}"
            ::= {{ solo 1 }}
        END
        """
    )


def source_set():
    """The four modules as a mutable mapping a test can edit between builds."""
    return {
        "CYCLE-A-MIB": cycle_a(),
        "CYCLE-B-MIB": cycle_b(),
        "CYCLE-C-MIB": cycle_c(),
        "SOLO-MIB": solo(),
    }


def reader(modules):
    """A source serving exactly *modules*, and reporting a miss for anything else."""

    def serve(mibname, context):
        try:
            return modules[mibname]

        except KeyError:
            raise error.PySmiReaderFileNotFoundError(
                mibname=mibname, reader=None
            ) from None

    return CallbackReader(serve)


class ParseCounter:
    """Counts parser invocations, keyed by the bytes handed to them.

    Keying on content rather than module name separates "parsed a second
    module" from "parsed the same text a second time"; only the latter is
    what a cache exists to prevent.
    """

    def __init__(self):
        self.calls = []
        self._original = SmiV1CompatParser.parse

    def __enter__(self):
        counter = self

        def counting(inner, data, **kwargs):
            counter.calls.append(hashlib.sha256(data.encode()).hexdigest())
            return counter._original(inner, data, **kwargs)

        SmiV1CompatParser.parse = counting
        return self

    def __exit__(self, *exc):
        SmiV1CompatParser.parse = self._original

    def take(self):
        """The parses since the last take, as a list of content hashes."""
        calls, self.calls = self.calls, []
        return calls


class CountingCodegen(JsonCodeGen):
    """A JSON generator that records every module it is asked to render.

    Optionally refuses named modules, for the failure tests: a generator
    that cannot render a module is exactly what a real defect looks like to
    the compiler.
    """

    def __init__(self, victims=()):
        super().__init__()
        self.victims = set(victims)
        self.generated = []

    def gen_code(self, ast, symbolTable, **kwargs):
        name = ast[0]

        if name in self.victims:
            raise error.PySmiCodegenError(f"cannot render {name}")

        self.generated.append(name)

        return super().gen_code(ast, symbolTable, **kwargs)

    def take(self):
        """The module names generated since the last take, as a set."""
        names = set(self.generated)
        self.generated.clear()
        return names


def compiler(modules, codegen=None, buildCache=None, **kwargs):
    """A compiler over *modules*, capturing whatever the writer is handed."""
    documents = {}
    mibCompiler = MibCompiler(
        SmiV1CompatParser(),
        CountingCodegen() if codegen is None else codegen,
        CallbackWriter(
            lambda mibname, data, cbCtx: documents.__setitem__(mibname, data)
        ),
        buildCache=InMemoryBuildCache() if buildCache is None else buildCache,
        **kwargs,
    )
    mibCompiler.add_sources(reader(modules))

    return mibCompiler, documents


class IncrementalRebuildTestCase(unittest.TestCase):
    """Which products each kind of change rebuilds, and which it reuses."""

    def setUp(self):
        self.modules = source_set()
        self.compiler, self.documents = compiler(self.modules)
        self.codegen = self.compiler._codegen

    def compile(self, *names, **options):
        return self.compiler.compile(*(names or self.modules), rebuild=True, **options)

    def testColdBuildGeneratesEveryModuleOnce(self):
        """The baseline the deltas below are measured against."""
        with ParseCounter() as parses:
            statuses = self.compile()

        self.assertEqual(
            self.codegen.take(),
            set(CYCLE) | {"SOLO-MIB"} | set(BUNDLED),
        )
        self.assertEqual(
            len(parses.calls),
            len(set(parses.calls)),
            "the cold build parsed some text twice",
        )
        self.assertTrue(all(str(statuses[name]) == "compiled" for name in statuses))

    def testUnchangedBuildReusesEveryProduct(self):
        self.compile()
        self.codegen.take()

        with ParseCounter() as parses:
            statuses = self.compile()

        self.assertEqual(parses.calls, [], "an unchanged module was re-parsed")
        self.assertEqual(
            self.codegen.take(), set(), "an unchanged module was regenerated"
        )
        self.assertTrue(all(str(statuses[name]) == "compiled" for name in statuses))

    def testChangedTextualConventionRangeRebuildsTheWholeCycle(self):
        """A's TC is imported by C, and the cycle closes through B: all three
        rebuild though only A's text changed, and SOLO is left alone."""
        self.compile()
        self.codegen.take()

        self.modules["CYCLE-A-MIB"] = cycle_a("(0..99)")

        with ParseCounter() as parses:
            self.compile()

        self.assertEqual(
            len(parses.calls), 1, "more than the edited module was re-parsed"
        )
        self.assertEqual(self.codegen.take(), set(CYCLE))

        # Reuse must not change a single byte of meaning: the warm build's
        # output, cached products included, is the cold build's output.
        cold, coldDocuments = compiler(self.modules, buildCache=NullBuildCache())
        cold.compile(*self.modules, rebuild=True)

        for mibname in coldDocuments:
            self.assertEqual(
                json.loads(self.documents[mibname]),
                json.loads(coldDocuments[mibname]),
                f"{mibname} differs between the cached and the cold build",
            )

    def testCommentOnlyChangeReusesEveryProduct(self):
        """The explicit rule: comments never reach the parse tree, so they
        cannot change any output mode. The touched module is re-parsed --
        the parse cache keys on the text -- and regenerated never."""
        self.compile()
        self.codegen.take()
        before = self.documents["SOLO-MIB"]

        self.modules["SOLO-MIB"] = solo() + "-- an editorial note\n"

        with ParseCounter() as parses:
            self.compile()

        self.assertEqual(
            len(parses.calls), 1, "the touched module was not re-parsed"
        )
        self.assertEqual(
            self.codegen.take(), set(), "a comment-only change regenerated output"
        )
        self.assertEqual(
            self.documents["SOLO-MIB"],
            before,
            "the product served after a comment-only change is not the original",
        )

    def testChangedUnrelatedModuleRebuildsOnlyItself(self):
        """The cycle's invalidation does not leak outward, either."""
        self.compile()
        self.codegen.take()

        self.modules["SOLO-MIB"] = solo("solo, revised")

        with ParseCounter() as parses:
            self.compile()

        self.assertEqual(len(parses.calls), 1)
        self.assertEqual(self.codegen.take(), {"SOLO-MIB"})

    def testGenerationOptionsArePartOfTheKey(self):
        """genTexts changes the output, so it changes the key -- and the
        entries for either setting stay valid behind their own keys."""
        everything = set(CYCLE) | {"SOLO-MIB"} | set(BUNDLED)

        self.compile()
        self.codegen.take()

        self.compile(genTexts=True)
        self.assertEqual(self.codegen.take(), everything)

        self.compile(genTexts=True)
        self.assertEqual(self.codegen.take(), set())

        self.compile()
        self.assertEqual(
            self.codegen.take(), set(), "the original entries did not survive"
        )


class SourcePrioritySwitchTestCase(unittest.TestCase):
    """A different copy winning the source order is a different input."""

    def setUp(self):
        self.winner = solo("first copy").replace("SOLO-MIB", "PRIORITY-MIB")
        self.loser = (
            solo("second copy")
            .replace("SOLO-MIB", "PRIORITY-MIB")
            .replace("enterprises 9004", "enterprises 9104")
        )

    def testADifferentWinnerIsNotServedFromCache(self):
        """Two sources carry different modules under one name, at the same
        revision, so source order decides. Swapping the order must swap the
        output -- a cache that answered by name would serve the first copy
        forever."""
        mibCompiler, documents = compiler({})
        codegen = mibCompiler._codegen

        mibCompiler.set_sources(reader({"PRIORITY-MIB": self.winner}))
        mibCompiler.compile("PRIORITY-MIB", rebuild=True)
        codegen.take()
        firstDocument = documents["PRIORITY-MIB"]

        mibCompiler.set_sources(reader({"PRIORITY-MIB": self.loser}))
        mibCompiler.compile("PRIORITY-MIB", rebuild=True)

        self.assertEqual(
            codegen.take(),
            {"PRIORITY-MIB"},
            "the second source's copy was not regenerated",
        )
        self.assertNotEqual(documents["PRIORITY-MIB"], firstDocument)
        self.assertIn(
            "1.3.6.1.4.1.9104",
            documents["PRIORITY-MIB"],
            "the output is still the first source's copy",
        )

    def testTheSameCopyFromADifferentSourceIsNotServedEither(self):
        """Identical text at a different path still regenerates: the source
        a product was built from is part of its key, because the product
        records that source in its provenance."""
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)

        directories = []
        for name in ("one", "two"):
            directory = os.path.join(root, name)
            os.makedirs(directory)
            with open(os.path.join(directory, "PRIORITY-MIB"), "w") as fileObj:
                fileObj.write(self.winner)
            directories.append(FileReader(directory))

        mibCompiler, _ = compiler({})
        codegen = mibCompiler._codegen

        mibCompiler.set_sources(directories[0])
        mibCompiler.compile("PRIORITY-MIB", rebuild=True)
        codegen.take()

        mibCompiler.set_sources(directories[1])

        with ParseCounter() as parses:
            mibCompiler.compile("PRIORITY-MIB", rebuild=True)

        self.assertEqual(
            parses.calls, [], "identical text was re-parsed for a new path"
        )
        self.assertEqual(
            codegen.take(),
            {"PRIORITY-MIB"},
            "a product from another source was served for this one",
        )


class GeneratorVersionChangeTestCase(unittest.TestCase):
    """A product is one generator's output at one pysmi release."""

    def testADifferentVersionNeverReadsTheCache(self):
        modules = source_set()
        cache = InMemoryBuildCache()

        first, _ = compiler(modules, buildCache=cache)
        first.compile(*modules, rebuild=True)

        with mock.patch.object(compiler_module, "packageVersion", "0.0.0-other"):
            other, _ = compiler(modules, buildCache=cache)
            otherCodegen = other._codegen

            other.compile(*modules, rebuild=True)
            self.assertEqual(
                otherCodegen.take(),
                set(CYCLE) | {"SOLO-MIB"} | set(BUNDLED),
                "a different pysmi version read this version's products",
            )

            other.compile(*modules, rebuild=True)
            self.assertEqual(
                otherCodegen.take(), set(), "its own entries do not hit either"
            )

        # And the original version's entries are still behind their own keys.
        firstCodegen = first._codegen
        firstCodegen.take()
        first.compile(*modules, rebuild=True)
        self.assertEqual(firstCodegen.take(), set())


class CorruptedCacheTestCase(unittest.TestCase):
    """A damaged cache costs a rebuild and can never serve a wrong product."""

    def setUp(self):
        self.modules = source_set()
        self.cacheDir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cacheDir, True)

    def entries(self):
        return [
            name
            for name in os.listdir(self.cacheDir)
            if name.startswith(FileBuildCache.ENTRY_PREFIX)
        ]

    def testAGarbageEntryIsAMiss(self):
        first, firstDocuments = compiler(
            self.modules, buildCache=FileBuildCache(self.cacheDir)
        )
        first.compile(*self.modules, rebuild=True)
        self.assertTrue(self.entries(), "the cold build cached nothing")

        for name in self.entries():
            with open(os.path.join(self.cacheDir, name), "wb") as fileObj:
                fileObj.write(b"\x00not a pickle at all")

        second, secondDocuments = compiler(
            self.modules, buildCache=FileBuildCache(self.cacheDir)
        )
        second.compile(*self.modules, rebuild=True)

        self.assertEqual(
            second._codegen.take(),
            set(CYCLE) | {"SOLO-MIB"} | set(BUNDLED),
            "a corrupted entry was served",
        )
        for mibname in firstDocuments:
            self.assertEqual(
                json.loads(secondDocuments[mibname]),
                json.loads(firstDocuments[mibname]),
                f"{mibname} changed after the cache was corrupted",
            )

        # The rebuild re-wrote the entries, so the next build hits again.
        second.compile(*self.modules, rebuild=True)
        self.assertEqual(second._codegen.take(), set())

    def testAWellFormedButWrongEntryIsAMiss(self):
        first, _ = compiler(self.modules, buildCache=FileBuildCache(self.cacheDir))
        first.compile(*self.modules, rebuild=True)

        for name in self.entries():
            with open(os.path.join(self.cacheDir, name), "wb") as fileObj:
                pickle.dump({"not": "a compiled product"}, fileObj)

        second, _ = compiler(self.modules, buildCache=FileBuildCache(self.cacheDir))
        second.compile(*self.modules, rebuild=True)

        self.assertEqual(
            second._codegen.take(),
            set(CYCLE) | {"SOLO-MIB"} | set(BUNDLED),
            "a well-formed but wrong entry was served",
        )

    def testClearRemovesOnlyItsOwnEntries(self):
        first, _ = compiler(self.modules, buildCache=FileBuildCache(self.cacheDir))
        first.compile(*self.modules, rebuild=True)

        foreign = os.path.join(self.cacheDir, "not-a-cache-entry.txt")
        with open(foreign, "w") as fileObj:
            fileObj.write("someone else's file")

        FileBuildCache(self.cacheDir).clear()

        self.assertEqual(self.entries(), [])
        self.assertTrue(os.path.exists(foreign))


class FailureIsNotPublishedTestCase(unittest.TestCase):
    """A failure stores nothing, and neither does what its failure taints."""

    def setUp(self):
        self.modules = {
            "BROKEN-MIB": textwrap.dedent(
                """\
                BROKEN-MIB DEFINITIONS ::= BEGIN
                IMPORTS
                    MODULE-IDENTITY, enterprises FROM SNMPv2-SMI
                    TEXTUAL-CONVENTION FROM SNMPv2-TC;

                BrokenType ::= TEXTUAL-CONVENTION
                    STATUS current
                    DESCRIPTION "broken"
                    SYNTAX INTEGER (0..5)

                broken MODULE-IDENTITY
                    LAST-UPDATED "202401010000Z"
                    ORGANIZATION "test"
                    CONTACT-INFO "test"
                    DESCRIPTION "broken"
                    ::= { enterprises 9005 }
                END
                """
            ),
            "VICTIM-MIB": textwrap.dedent(
                """\
                VICTIM-MIB DEFINITIONS ::= BEGIN
                IMPORTS
                    MODULE-IDENTITY, OBJECT-TYPE, enterprises FROM SNMPv2-SMI
                    BrokenType FROM BROKEN-MIB;

                victim MODULE-IDENTITY
                    LAST-UPDATED "202401010000Z"
                    ORGANIZATION "test"
                    CONTACT-INFO "test"
                    DESCRIPTION "victim"
                    ::= { enterprises 9006 }

                victimObj OBJECT-TYPE
                    SYNTAX BrokenType
                    MAX-ACCESS read-only
                    STATUS current
                    DESCRIPTION "victim"
                    ::= { victim 1 }
                END
                """
            ),
            "CLEAN-MIB": solo("clean").replace("SOLO-MIB", "CLEAN-MIB"),
        }

    def testFailureAndTaintLeaveNoCacheEntries(self):
        class RecordingBuildCache(InMemoryBuildCache):
            def __init__(self):
                super().__init__()
                self.stored = []

            def set(self, key, product):
                self.stored.append(product[0].name)
                super().set(key, product)

        cache = RecordingBuildCache()
        codegen = CountingCodegen(victims={"BROKEN-MIB"})
        mibCompiler, _ = compiler(self.modules, codegen=codegen, buildCache=cache)

        statuses = mibCompiler.compile("VICTIM-MIB", "CLEAN-MIB", rebuild=True)
        codegen.take()

        self.assertEqual(str(statuses["BROKEN-MIB"]), "failed")
        self.assertEqual(
            str(statuses["VICTIM-MIB"]),
            "unprocessed",
            "a module importing a failed module was not tainted",
        )
        self.assertEqual(str(statuses["CLEAN-MIB"]), "compiled")

        self.assertIn("CLEAN-MIB", cache.stored)
        self.assertNotIn("BROKEN-MIB", cache.stored, "a failed module was cached")
        self.assertNotIn(
            "VICTIM-MIB",
            cache.stored,
            "a module dropped for its failed import was cached",
        )

        # Repaired, both build for real -- from the generator, not from any
        # entry the failed build might have left behind.
        codegen.victims.clear()
        statuses = mibCompiler.compile("VICTIM-MIB", "CLEAN-MIB", rebuild=True)

        self.assertEqual(codegen.take(), {"BROKEN-MIB", "VICTIM-MIB"})
        self.assertEqual(str(statuses["BROKEN-MIB"]), "compiled")
        self.assertEqual(str(statuses["VICTIM-MIB"]), "compiled")


class BuildCacheProvidersTestCase(unittest.TestCase):
    """The cache is a component, not a fixed behaviour of the compiler."""

    def setUp(self):
        self.modules = source_set()

    def testTheDefaultProviderCachesNothing(self):
        self.assertIsInstance(
            MibCompiler(
                SmiV1CompatParser(),
                JsonCodeGen(),
                CallbackWriter(lambda *args, **kwargs: None),
            )._buildCache,
            NullBuildCache,
        )

        mibCompiler, _ = compiler(self.modules, buildCache=NullBuildCache())
        mibCompiler.compile(*self.modules, rebuild=True)
        mibCompiler._codegen.take()
        mibCompiler.compile(*self.modules, rebuild=True)

        self.assertEqual(
            mibCompiler._codegen.take(),
            set(CYCLE) | {"SOLO-MIB"} | set(BUNDLED),
            "the null cache reused a product",
        )

    def testACustomProviderIsAskedAndFilled(self):
        """A provider written outside pysmi works with no registration step."""

        class Recording(AbstractBuildCache):
            def __init__(self):
                self.gets = []
                self.sets = []
                self.entries = {}

            def get(self, key):
                self.gets.append(key)
                return self.entries.get(key)

            def set(self, key, product):
                self.sets.append(key)
                self.entries[key] = product

            def clear(self):
                self.entries.clear()

        recording = Recording()
        mibCompiler, _ = compiler(self.modules, buildCache=recording)
        mibCompiler.compile(*self.modules, rebuild=True)

        self.assertTrue(recording.gets)
        self.assertEqual(sorted(set(recording.sets)), sorted(recording.entries))

        mibCompiler._codegen.take()
        mibCompiler.compile(*self.modules, rebuild=True)
        self.assertEqual(mibCompiler._codegen.take(), set())

    def testClearBuildCacheDropsEveryProduct(self):
        mibCompiler, _ = compiler(self.modules)
        mibCompiler.compile(*self.modules, rebuild=True)
        mibCompiler._codegen.take()

        mibCompiler.clear_build_cache()
        mibCompiler.compile(*self.modules, rebuild=True)

        self.assertEqual(
            mibCompiler._codegen.take(),
            set(CYCLE) | {"SOLO-MIB"} | set(BUNDLED),
        )


if __name__ == "__main__":
    unittest.main()
