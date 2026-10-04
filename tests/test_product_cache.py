#
# This file is part of pysmi software.
#
# Copyright (c) 2005-2019, Ilya Etingof deceased
#
"""Reusing whole compilation products across incremental builds.

The parse cache saves re-parsing text that has not changed. The product cache
saves re-*generating* a module whose inputs have not changed: the generated
document itself is kept, keyed on everything that can influence it -- the
source actually selected, the generation parameters, the generator's identity
and version, and the transitive import closure.

The fixture is four small modules. A-MIB, B-MIB and C-MIB import each other
in a cycle (A uses B's TEXTUAL-CONVENTION, B names an object under C's arc,
C aliases A's type), so the cyclic invalidation scope is exercised by every
change. U-MIB imports nothing and is imported by nothing -- the control that
no change to the cycle may ever rebuild.

Every test records parser and code generator invocations by module name and
asserts on exactly which products were reused and which were rebuilt.
"""

import os
import pickle
import shutil
import tempfile
import textwrap
import unittest
from unittest import mock

from pysmi import compiler as compiler_module
from pysmi import error
from pysmi.cache import FileProductCache, InMemoryProductCache
from pysmi.codegen import JsonCodeGen
from pysmi.compiler import MibCompiler
from pysmi.mibinfo import MibInfo, digest_of, module_names, source_digest
from pysmi.parser import SmiV1CompatParser
from pysmi.reader import CallbackReader, FileReader
from pysmi.writer import CallbackWriter

#: The modules the cycle is made of, and the control module beside it.
CYCLE = ("A-MIB", "B-MIB", "C-MIB")
CONTROL = "U-MIB"


def a_mib(extra=""):
    """A-MIB: uses B-MIB's textual convention."""
    return textwrap.dedent(
        f"""\
        A-MIB DEFINITIONS ::= BEGIN
        IMPORTS
            BTextualConvention
                FROM B-MIB;

        aRoot OBJECT IDENTIFIER ::= {{ 1 3 6 1 4 1 99999 1 }}
        aLeaf OBJECT IDENTIFIER ::= {{ aRoot 1 }}
        {extra}
        AType ::= BTextualConvention
        END
        """
    )


def b_mib(syntax="INTEGER (1..10)", note="the range of BTextualConvention"):
    """B-MIB: defines the textual convention, names an object under C-MIB's arc.

    *note* is the text of a comment, so a test can change a comment and
    nothing else.
    """
    return textwrap.dedent(
        f"""\
        B-MIB DEFINITIONS ::= BEGIN
        IMPORTS
            TEXTUAL-CONVENTION
                FROM SNMPv2-TC
            CLeaf
                FROM C-MIB;

        -- {note}
        BTextualConvention ::= TEXTUAL-CONVENTION
            STATUS      current
            DESCRIPTION "A textual convention."
            SYNTAX      {syntax}

        bLeaf OBJECT IDENTIFIER ::= {{ CLeaf 1 }}
        END
        """
    )


def c_mib():
    """C-MIB: aliases A-MIB's type, closing the import cycle."""
    return textwrap.dedent(
        """\
        C-MIB DEFINITIONS ::= BEGIN
        IMPORTS
            AType
                FROM A-MIB;

        cRoot OBJECT IDENTIFIER ::= { 1 3 6 1 4 1 99999 3 }
        CLeaf OBJECT IDENTIFIER ::= { cRoot 1 }

        CType ::= AType
        END
        """
    )


def u_mib():
    """U-MIB: the control, importing and imported by nothing."""
    return textwrap.dedent(
        """\
        U-MIB DEFINITIONS ::= BEGIN
        uRoot OBJECT IDENTIFIER ::= { 1 3 6 1 4 1 99999 4 }
        uLeaf OBJECT IDENTIFIER ::= { uRoot 1 }
        END
        """
    )


def four_sources():
    """The four-module set, as a mutable dict a test edits between runs."""
    return {
        "A-MIB": a_mib(),
        "B-MIB": b_mib(),
        "C-MIB": c_mib(),
        "U-MIB": u_mib(),
    }


class Build:
    """One compiler over a mutable source set, recording what it calls.

    The parser and the code generator are wrapped so every invocation is
    recorded by module name -- the counts these tests assert on. The writer
    collects into a dict, so what a run published can be compared against
    what the previous run published.
    """

    def __init__(self, sources, productCache="memory"):
        self.sources = sources
        self.written = {}

        self.parser = SmiV1CompatParser()
        self.parserCalls = []
        realParse = self.parser.parse

        def countingParse(data, **kwargs):
            names = module_names(data)
            self.parserCalls.append(names[0] if names else "<unreadable>")
            return realParse(data, **kwargs)

        self.parser.parse = countingParse

        self.codegen = JsonCodeGen()
        self.codegenCalls = []
        self._realGenCode = self.codegen.gen_code

        def countingGenCode(ast, symbolTable, **kwargs):
            mibInfo, data = self._realGenCode(ast, symbolTable, **kwargs)
            self.codegenCalls.append(mibInfo.name)
            return mibInfo, data

        self.codegen.gen_code = countingGenCode

        if productCache == "memory":
            productCache = InMemoryProductCache()

        self.compiler = MibCompiler(
            self.parser,
            self.codegen,
            CallbackWriter(
                lambda mibname, data, cbCtx: self.written.__setitem__(mibname, data)
            ),
            productCache=productCache,
        )
        self.compiler.add_sources(
            CallbackReader(lambda mibname, cbCtx: self.sources.get(mibname))
        )

    def compile(self, *mibnames, **options):
        """Compile the four-module set (or *mibnames*), counters reset."""
        self.parserCalls.clear()
        self.codegenCalls.clear()

        return self.compiler.compile(*(mibnames or (*CYCLE[:1], CONTROL)), **options)


class ColdBuildTestCase(unittest.TestCase):
    """The baseline the incremental runs are measured against."""

    def testEverythingIsParsedAndGeneratedOnce(self):
        build = Build(four_sources())
        processed = build.compile()

        for mibname in (*CYCLE, CONTROL):
            self.assertEqual("compiled", processed[mibname], mibname)
            self.assertEqual(1, build.parserCalls.count(mibname), mibname)
            self.assertEqual(1, build.codegenCalls.count(mibname), mibname)

    def testTheCacheIsOptIn(self):
        """Without a productCache, every run regenerates -- the old behaviour."""
        build = Build(four_sources(), productCache=None)
        build.compile()
        build.compile()

        self.assertEqual([], build.parserCalls, "the parse cache is still on")
        self.assertEqual(
            sorted((*CYCLE, CONTROL)),
            sorted(
                name for name in build.codegenCalls if name in (*CYCLE, CONTROL)
            ),
        )


class UnchangedInputsTestCase(unittest.TestCase):
    """A run over unchanged inputs builds nothing and publishes everything."""

    def setUp(self):
        self.build = Build(four_sources())
        self.first = self.build.compile()
        self.firstWritten = dict(self.build.written)

    def testNothingIsReparsedOrRegenerated(self):
        self.build.compile()

        self.assertEqual([], self.build.parserCalls)
        self.assertEqual([], self.build.codegenCalls)

    def testTheWriterStillReceivesEveryModule(self):
        """A cache hit is not a skip: the destination sees the full set."""
        processed = self.build.compile()

        for mibname in (*CYCLE, CONTROL):
            self.assertEqual("compiled", processed[mibname], mibname)

        self.assertEqual(self.firstWritten, self.build.written)


class ImportedModuleChangeTestCase(unittest.TestCase):
    """A changed TEXTUAL-CONVENTION rebuilds everything that imports it.

    A-MIB's own text never changes here -- its product is invalidated through
    the import closure in its key, which is the whole point of keying on
    dependencies. The cycle makes the scope deterministic: every member
    carries every other member in its closure, so one change rebuilds the
    whole cycle and the control module is never touched.
    """

    def testATextualConventionChangeRebuildsTheWholeCycle(self):
        sources = four_sources()
        build = Build(sources)
        build.compile()
        controlProduct = build.written[CONTROL]

        sources["B-MIB"] = b_mib(syntax="INTEGER (1..100)")
        processed = build.compile()

        self.assertEqual(["B-MIB"], build.parserCalls)
        self.assertEqual(sorted(CYCLE), sorted(build.codegenCalls))
        self.assertNotIn(CONTROL, build.codegenCalls)

        for mibname in (*CYCLE, CONTROL):
            self.assertEqual("compiled", processed[mibname], mibname)

        self.assertEqual(
            controlProduct,
            build.written[CONTROL],
            "the unrelated module's product must not move",
        )


class CommentOnlyChangeTestCase(unittest.TestCase):
    """The explicit rule for comment-only changes.

    A module's own product records the digest of its full text, so a comment
    change rebuilds that module -- serving the old product would publish a
    document recording a source it was not built from. A dependency reaches
    a module's output only through its parse tree, which comments never
    enter, so nothing that imports the touched module is rebuilt. The rule
    holds for every output mode, because both halves of it are about what
    the output records, not about what any one generator does.
    """

    def setUp(self):
        self.sources = four_sources()
        self.build = Build(self.sources)
        self.build.compile()
        self.firstWritten = dict(self.build.written)

        self.sources["B-MIB"] = b_mib(note="the range of the B textual convention")
        self.processed = self.build.compile()

    def testOnlyTheTouchedModuleIsRegenerated(self):
        self.assertEqual(["B-MIB"], self.build.parserCalls)
        self.assertEqual(["B-MIB"], self.build.codegenCalls)

    def testDependentsAreByteIdentical(self):
        for mibname in ("A-MIB", "C-MIB", CONTROL):
            self.assertEqual(
                self.firstWritten[mibname],
                self.build.written[mibname],
                f"{mibname} was rebuilt over a comment",
            )

    def testTheTouchedModuleRecordsItsNewSource(self):
        """The rebuilt product names the source it was actually built from."""
        self.assertNotEqual(self.firstWritten["B-MIB"], self.build.written["B-MIB"])
        self.assertEqual(
            source_digest(self.sources["B-MIB"]),
            digest_of(self.build.written["B-MIB"]),
        )


class SourcePrioritySwitchTestCase(unittest.TestCase):
    """Which source supplied a module is key material.

    Two directories each carry A-MIB; the rest of the set lives only in the
    first. With no MODULE-IDENTITY revisions to compare, source order
    decides, so swapping the sources changes which copy is selected -- and
    the product built from the other source must not be served.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

        self.dirOne = os.path.join(self.root, "one")
        self.dirTwo = os.path.join(self.root, "two")
        os.makedirs(self.dirOne)
        os.makedirs(self.dirTwo)

        for mibname, text in four_sources().items():
            with open(os.path.join(self.dirOne, mibname), "w") as fileObj:
                fileObj.write(text)

        # The second source's copy, byte-identical for now.
        with open(os.path.join(self.dirTwo, "A-MIB"), "w") as fileObj:
            fileObj.write(a_mib())

        self.build = Build({})
        self.build.compiler.set_sources(
            FileReader(self.dirOne), FileReader(self.dirTwo)
        )

    def _swap(self):
        self.build.compiler.set_sources(
            FileReader(self.dirTwo), FileReader(self.dirOne)
        )

    def testASwitchToAnIdenticalCopyRebuildsOnlyWhatItSupplies(self):
        self.build.compile()

        self._swap()
        processed = self.build.compile()

        self.assertEqual([], self.build.parserCalls)
        self.assertEqual(["A-MIB"], self.build.codegenCalls)
        self.assertIn(self.dirTwo, processed["A-MIB"].path)

    def testASwitchToADifferentCopyRebuildsItsDependents(self):
        self.build.compile()

        self._swap()

        with open(os.path.join(self.dirTwo, "A-MIB"), "w") as fileObj:
            fileObj.write(a_mib(extra="aLeaf2 OBJECT IDENTIFIER ::= { aRoot 2 }"))

        self.build.compile()

        self.assertEqual(["A-MIB"], self.build.parserCalls)
        self.assertEqual(sorted(CYCLE), sorted(self.build.codegenCalls))
        self.assertNotIn(CONTROL, self.build.codegenCalls)


class GeneratorVersionTestCase(unittest.TestCase):
    """A product of another pysmi release is never served by this one."""

    def testAVersionChangeRebuildsEverything(self):
        build = Build(four_sources())
        build.compile()

        with mock.patch.object(compiler_module, "packageVersion", "9.9.9-test"):
            build.compile()

        self.assertEqual(
            [],
            build.parserCalls,
            "the parse cache is keyed on the parser, not the generator",
        )
        for mibname in (*CYCLE, CONTROL):
            self.assertIn(mibname, build.codegenCalls)


class GenerationParametersTestCase(unittest.TestCase):
    """Parameters that reach the code generator are key material."""

    def testGenTextsIsKeyMaterial(self):
        build = Build(four_sources())

        build.compile(genTexts=True)
        withTexts = dict(build.written)

        build.compile(genTexts=True)
        self.assertEqual([], build.codegenCalls)

        build.compile(genTexts=False)
        self.assertEqual(
            sorted((*CYCLE, CONTROL)),
            sorted(m for m in build.codegenCalls if m in (*CYCLE, CONTROL)),
        )
        withoutTexts = dict(build.written)

        self.assertNotEqual(withTexts["B-MIB"], withoutTexts["B-MIB"])

        build.compile(genTexts=False)
        self.assertEqual([], build.codegenCalls)

        build.compile(genTexts=True)
        self.assertEqual(
            [],
            build.codegenCalls,
            "both parameterisations stay cached, each under its own key",
        )
        self.assertEqual(withTexts, build.written)

    def testRebuildBypassesTheCache(self):
        """rebuild=True forces regeneration, and refreshes what it builds."""
        build = Build(four_sources())
        build.compile()

        build.compile(rebuild=True)
        self.assertEqual(
            sorted((*CYCLE, CONTROL)),
            sorted(m for m in build.codegenCalls if m in (*CYCLE, CONTROL)),
        )

        build.compile()
        self.assertEqual([], build.codegenCalls)


class FileProductCacheTestCase(unittest.TestCase):
    """The on-disk provider: shared across compilers, robust to damage."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.cacheDir = os.path.join(self.root, "cache")

    def testANewCompilerReusesWhatAPreviousOneBuilt(self):
        """The shape of a build system invoking the compiler per change set."""
        sources = four_sources()

        first = Build(sources, productCache=FileProductCache(self.cacheDir))
        first.compile()

        second = Build(sources, productCache=FileProductCache(self.cacheDir))
        processed = second.compile()

        self.assertEqual([], second.codegenCalls)
        for mibname in (*CYCLE, CONTROL):
            self.assertEqual("compiled", processed[mibname], mibname)
            self.assertEqual(first.written[mibname], second.written[mibname])

    def testCorruptEntriesAreMissesNeverAnswers(self):
        """A damaged cache costs a rebuild and never changes output."""
        sources = four_sources()
        build = Build(sources, productCache=FileProductCache(self.cacheDir))
        build.compile()
        firstWritten = dict(build.written)

        entries = sorted(
            name
            for name in os.listdir(self.cacheDir)
            if name.startswith(FileProductCache.ENTRY_PREFIX)
        )
        self.assertTrue(entries, "the first run cached something")

        for index, name in enumerate(entries):
            path = os.path.join(self.cacheDir, name)

            if index % 2:
                with open(path, "wb") as fileObj:
                    fileObj.write(b"not a pickle at all")

            else:
                # A well-formed entry filed under the wrong key -- a hit on
                # it would publish another module's product.
                with open(path, "wb") as fileObj:
                    pickle.dump(
                        {
                            "format": FileProductCache.ENTRY_FORMAT,
                            "key": "sha256:forged",
                            "product": ("forged", MibInfo(name="forged")),
                        },
                        fileObj,
                    )

        processed = build.compile()

        self.assertEqual(
            sorted((*CYCLE, CONTROL)),
            sorted(m for m in build.codegenCalls if m in (*CYCLE, CONTROL)),
        )
        for mibname in (*CYCLE, CONTROL):
            self.assertEqual("compiled", processed[mibname], mibname)

        self.assertEqual(firstWritten, build.written)
        self.assertNotIn("forged", build.written.values())


class FailureIsNeverPublishedTestCase(unittest.TestCase):
    """A failed run leaves nothing behind that a later run could serve."""

    def testAParseFailureCachesNothingForItOrItsDependents(self):
        sources = four_sources()
        build = Build(sources)
        build.compile()

        sources["B-MIB"] = "B-MIB DEFINITIONS ::= BEGIN\nthis is not asn1 (((\nEND\n"
        processed = build.compile()

        self.assertEqual("failed", processed["B-MIB"])
        self.assertEqual("unprocessed", processed["A-MIB"])
        self.assertEqual("compiled", processed[CONTROL])

        # B-MIB never generated, and A-MIB generated against an import
        # closure that did not resolve -- so neither may have left a product
        # behind. Fixing B-MIB must rebuild exactly the cycle.
        sources["B-MIB"] = b_mib(syntax="INTEGER (1..77)")
        processed = build.compile()

        self.assertEqual(sorted(CYCLE), sorted(build.codegenCalls))
        self.assertNotIn(CONTROL, build.codegenCalls)

        for mibname in (*CYCLE, CONTROL):
            self.assertEqual("compiled", processed[mibname], mibname)

    def testACodegenFailureCachesNothingForTheFailedModule(self):
        sources = four_sources()
        build = Build(sources)

        realGenCode = build.codegen.gen_code

        def failingGenCode(ast, symbolTable, **kwargs):
            mibInfo, data = realGenCode(ast, symbolTable, **kwargs)

            if mibInfo.name == "C-MIB":
                raise error.PySmiError("injected failure")

            return mibInfo, data

        build.codegen.gen_code = failingGenCode
        processed = build.compile()

        self.assertEqual("failed", processed["C-MIB"])
        self.assertEqual("unprocessed", processed["A-MIB"])
        self.assertEqual("unprocessed", processed["B-MIB"])
        self.assertEqual("compiled", processed[CONTROL])

        # A-MIB and B-MIB generated valid products before C-MIB failed --
        # those are kept, and only the failed module is rebuilt. A cache
        # that published the failure would serve C-MIB here without
        # generating it.
        build.codegen.gen_code = realGenCode
        processed = build.compile()

        self.assertEqual(["C-MIB"], build.codegenCalls)

        for mibname in (*CYCLE, CONTROL):
            self.assertEqual("compiled", processed[mibname], mibname)


if __name__ == "__main__":
    unittest.main()
