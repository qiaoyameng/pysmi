#
# This file is part of pysmi software.
#
# Copyright (c) 2015-2020, Ilya Etingof <etingof@gmail@gmail.com>
# License: https://github.com/pysnmp/pysmi/blob/main/LICENSE.rst
#
"""Symbol-level pruning: build a compact artifact for named target symbols.

A deployment polling a handful of OIDs does not want every symbol of every
vendor MIB pulled into the generated output. ``targetSymbols`` restricts a
compile to a target and to its *symbol-level* closure -- OID ancestors,
referenced types, table rows and index columns, AUGMENTS relations, and the
imports loading the kept symbols needs. A module-level dependency (one symbol
refers to another module) must not drag that module's whole content back.

These tests use local mini MIBs only; nothing is downloaded. The parity tests
execute both the full and the pruned generated modules under pysnmp and
compare the OIDs, type constraints and index encoding; load parity is checked
in the consumer-layer test file.
"""

import textwrap
import unittest

from pysmi import error
from pysmi.codegen import PySnmpCodeGen
from pysmi.codegen.pruner import prune_modules
from pysmi.codegen.symtable import SymtableCodeGen
from pysmi.compiler import MibCompiler
from pysmi.parser import SmiV1CompatParser
from pysmi.reader import CallbackReader
from pysmi.searcher import StubSearcher
from pysmi.writer import CallbackWriter

from tests import mibs
from tests.harness import parse


def _build_tables(*mib_texts):
    """Parse *mib_texts* and their base stubs, returning ASTs and tables."""
    tables = {}
    asts = {}

    for dep in (mibs.SNMPV2_SMI, mibs.SNMPV2_TC, _SNMPV2_CONF):
        ast = parse(dep)
        info, table = SymtableCodeGen().gen_code(ast, tables, repairImports=True)
        tables[info.name] = table
        asts[info.name] = ast

    for text in mib_texts:
        ast = parse(text)
        info, table = SymtableCodeGen().gen_code(ast, tables, repairImports=True)
        tables[info.name] = table
        asts[info.name] = ast

    return asts, tables


_SNMPV2_CONF = """
SNMPv2-CONF DEFINITIONS ::= BEGIN
END
"""

# A module with a scalar, a table with an index and a constrained column, an
# AUGMENTS table, a notification, groups and a compliance statement.
MAIN_MIB = textwrap.dedent("""\
    PRUNE-MAIN-MIB DEFINITIONS ::= BEGIN

    IMPORTS
        MODULE-IDENTITY, OBJECT-TYPE, NOTIFICATION-TYPE,
        Counter32, Integer32, enterprises
            FROM SNMPv2-SMI
        TEXTUAL-CONVENTION, DisplayString
            FROM SNMPv2-TC
        OBJECT-GROUP, NOTIFICATION-GROUP, MODULE-COMPLIANCE
            FROM SNMPv2-CONF;

    pmMIB MODULE-IDENTITY
        LAST-UPDATED "202601010000Z"
        ORGANIZATION "Prune"
        CONTACT-INFO "Prune"
        DESCRIPTION "Prune"
        REVISION "202601010000Z"
        DESCRIPTION "Prune"
        ::= { enterprises 424242 }

    pmObjects OBJECT IDENTIFIER ::= { pmMIB 1 }

    PmState ::= TEXTUAL-CONVENTION
        STATUS current
        DESCRIPTION "state"
        SYNTAX INTEGER { up(1), down(2) }

    pmScalar OBJECT-TYPE
        SYNTAX PmState
        MAX-ACCESS read-write
        STATUS current
        DESCRIPTION "scalar"
        ::= { pmObjects 1 }

    pmTable OBJECT-TYPE
        SYNTAX SEQUENCE OF PmEntry
        MAX-ACCESS not-accessible
        STATUS current
        DESCRIPTION "table"
        ::= { pmObjects 2 }

    pmEntry OBJECT-TYPE
        SYNTAX PmEntry
        MAX-ACCESS not-accessible
        STATUS current
        DESCRIPTION "row"
        INDEX { pmIndex }
        ::= { pmTable 1 }

    PmEntry ::= SEQUENCE {
        pmIndex Integer32,
        pmName  DisplayString,
        pmCount Counter32
    }

    pmIndex OBJECT-TYPE
        SYNTAX Integer32
        MAX-ACCESS not-accessible
        STATUS current
        DESCRIPTION "index column"
        ::= { pmEntry 1 }

    pmName OBJECT-TYPE
        SYNTAX DisplayString (SIZE (1..32))
        MAX-ACCESS read-create
        STATUS current
        DESCRIPTION "named column"
        ::= { pmEntry 2 }

    pmCount OBJECT-TYPE
        SYNTAX Counter32
        MAX-ACCESS read-only
        STATUS current
        DESCRIPTION "counter column"
        DEFVAL { 0 }
        ::= { pmEntry 3 }

    pmAuxTable OBJECT-TYPE
        SYNTAX SEQUENCE OF PmAuxEntry
        MAX-ACCESS not-accessible
        STATUS current
        DESCRIPTION "augmenting table"
        ::= { pmObjects 3 }

    pmAuxEntry OBJECT-TYPE
        SYNTAX PmAuxEntry
        MAX-ACCESS not-accessible
        STATUS current
        DESCRIPTION "augmenting row"
        AUGMENTS { pmEntry }
        ::= { pmAuxTable 1 }

    PmAuxEntry ::= SEQUENCE { pmAuxData Integer32 }

    pmAuxData OBJECT-TYPE
        SYNTAX Integer32
        MAX-ACCESS read-only
        STATUS current
        DESCRIPTION "augmenting column"
        ::= { pmAuxEntry 1 }

    pmEvent NOTIFICATION-TYPE
        OBJECTS { pmCount, pmName }
        STATUS current
        DESCRIPTION "event"
        ::= { pmMIB 2 0 }

    pmObjectGroup OBJECT-GROUP
        OBJECTS { pmScalar, pmIndex, pmName, pmCount }
        STATUS current
        DESCRIPTION "object group"
        ::= { pmMIB 3 }

    pmNotificationGroup NOTIFICATION-GROUP
        NOTIFICATIONS { pmEvent }
        STATUS current
        DESCRIPTION "notification group"
        ::= { pmMIB 4 }

    pmCompliance MODULE-COMPLIANCE
        STATUS current
        DESCRIPTION "compliance"
        MODULE
            MANDATORY-GROUPS { pmObjectGroup, pmNotificationGroup }
        ::= { pmMIB 5 }

    END
""")

MODULE = "PRUNE-MAIN-MIB"


# A second vendor module that references one column of the first. Only the
# referenced symbol and its own closure may survive in the first module.
DEP_MIB = textwrap.dedent("""\
    PRUNE-DEP-MIB DEFINITIONS ::= BEGIN

    IMPORTS
        MODULE-IDENTITY, OBJECT-TYPE, Integer32, enterprises
            FROM SNMPv2-SMI
        pmName
            FROM PRUNE-MAIN-MIB;

    pdMIB MODULE-IDENTITY
        LAST-UPDATED "202601010000Z"
        ORGANIZATION "Dep"
        CONTACT-INFO "Dep"
        DESCRIPTION "Dep"
        REVISION "202601010000Z"
        DESCRIPTION "Dep"
        ::= { enterprises 434343 }

    pdRef OBJECT-TYPE
        SYNTAX Integer32
        MAX-ACCESS read-only
        STATUS current
        DESCRIPTION "hangs off an imported column"
        ::= { pmName 100 }

    END
""")

DEP_MODULE = "PRUNE-DEP-MIB"


class ClosureTestCase(unittest.TestCase):
    """The closure keeps exactly the symbols a target needs."""

    def test_column_keeps_row_table_index_and_ancestors(self):
        asts, tables = _build_tables(MAIN_MIB)
        new_asts, new_tables, reasons, imports = prune_modules(
            asts,
            tables,
            {MODULE: ["pmName"]},
            generated_modules=frozenset({MODULE}),
        )

        kept = reasons[MODULE]

        # The target and the row machinery it loads with.
        self.assertIn("pmName", kept)
        self.assertIn("pmEntry", kept)
        self.assertIn("pmTable", kept)
        self.assertIn("pmIndex", kept)
        self.assertIn("pmObjects", kept)
        self.assertIn("pmMIB", kept)

        # Reasons are given for every symbol that is not itself a target.
        self.assertEqual(kept["pmName"], "target symbol")
        self.assertEqual(kept["pmIndex"], "table index object")
        self.assertIn("OID ancestor", kept["pmTable"])

        # Unrelated symbols are gone: other columns, the scalar, its TC, the
        # AUGMENTS partner, and all conformance clauses.
        for gone in (
            "pmCount",
            "pmScalar",
            "PmState",
            "pmAuxData",
            "pmEvent",
            "pmObjectGroup",
            "pmNotificationGroup",
            "pmCompliance",
        ):
            self.assertNotIn(gone, kept, gone)

    def test_augments_relation_kept_in_both_directions(self):
        asts, tables = _build_tables(MAIN_MIB)
        _asts, tables2, reasons, _imports = prune_modules(
            asts,
            tables,
            {MODULE: ["pmAuxData"]},
            generated_modules=frozenset({MODULE}),
        )

        kept = reasons[MODULE]

        # The augmented row, its index and the augmenting row both survive.
        self.assertIn("pmAuxEntry", kept)
        self.assertIn("pmEntry", kept)
        self.assertIn("pmIndex", kept)
        self.assertIn("pmAuxTable", kept)
        self.assertIn("pmTable", kept)

        # And targeting the augmented row keeps the row that augments it.
        asts, tables = _build_tables(MAIN_MIB)
        _asts, _tables, reverse_reasons, _i = prune_modules(
            asts,
            tables,
            {MODULE: ["pmCount"]},
            generated_modules=frozenset({MODULE}),
        )
        self.assertIn("pmAuxEntry", reverse_reasons[MODULE])

    def test_notification_expands_to_its_objects(self):
        asts, tables = _build_tables(MAIN_MIB)
        _asts, _tables, reasons, _imports = prune_modules(
            asts,
            tables,
            {MODULE: ["pmEvent"]},
            generated_modules=frozenset({MODULE}),
        )

        kept = reasons[MODULE]
        self.assertIn("pmEvent", kept)
        self.assertIn("pmCount", kept)
        self.assertIn("pmName", kept)
        self.assertIn("pmIndex", kept)
        self.assertNotIn("pmScalar", kept)
        self.assertEqual(kept["pmCount"], "notification OBJECTS member")

    def test_object_group_target_expands_through_members(self):
        asts, tables = _build_tables(MAIN_MIB)
        _asts, _tables, reasons, _imports = prune_modules(
            asts,
            tables,
            {MODULE: ["pmObjectGroup"]},
            generated_modules=frozenset({MODULE}),
        )

        kept = reasons[MODULE]
        self.assertEqual(kept["pmObjectGroup"], "target symbol")
        for member in ("pmScalar", "pmIndex", "pmName", "pmCount"):
            self.assertIn(member, kept)
        self.assertEqual(kept["pmScalar"], "OBJECT-GROUP member")
        self.assertIn("PmState", kept)
        # The notification group is not referenced by this object group.
        self.assertNotIn("pmNotificationGroup", kept)

    def test_notification_group_target_expands_through_notifications(self):
        asts, tables = _build_tables(MAIN_MIB)
        _asts, _tables, reasons, _imports = prune_modules(
            asts,
            tables,
            {MODULE: ["pmNotificationGroup"]},
            generated_modules=frozenset({MODULE}),
        )

        kept = reasons[MODULE]
        self.assertIn("pmEvent", kept)
        self.assertEqual(kept["pmEvent"], "NOTIFICATION-GROUP member")
        self.assertIn("pmCount", kept)

    def test_compliance_target_expands_groups_and_their_objects(self):
        asts, tables = _build_tables(MAIN_MIB)
        _asts, _tables, reasons, _imports = prune_modules(
            asts,
            tables,
            {MODULE: ["pmCompliance"]},
            generated_modules=frozenset({MODULE}),
        )

        kept = reasons[MODULE]
        self.assertIn("pmObjectGroup", kept)
        self.assertIn("pmNotificationGroup", kept)
        self.assertIn("pmEvent", kept)
        for member in ("pmScalar", "pmName", "pmCount"):
            self.assertIn(member, kept)

    def test_scalar_keeps_only_its_type_chain(self):
        asts, tables = _build_tables(MAIN_MIB)
        _asts, _tables, reasons, imports = prune_modules(
            asts,
            tables,
            {MODULE: ["pmScalar"]},
            generated_modules=frozenset({MODULE}),
        )

        kept = reasons[MODULE]
        self.assertEqual(set(kept), {"pmScalar", "PmState", "pmObjects", "pmMIB"})
        self.assertEqual(kept["PmState"], "table row or referenced type")

    def test_imports_reduced_to_what_the_closure_loads(self):
        asts, tables = _build_tables(MAIN_MIB)
        new_asts, _tables, reasons, import_reasons = prune_modules(
            asts,
            tables,
            {MODULE: ["pmName"]},
            generated_modules=frozenset({MODULE}),
        )

        # DisplayString (the constrained type) and enterprises (an OID
        # ancestor) must load; Counter32 and the conformance macros must not.
        imports = new_asts[MODULE][2]
        self.assertIn("DisplayString", imports.get("SNMPv2-TC", []))
        self.assertIn("enterprises", imports.get("SNMPv2-SMI", []))
        self.assertNotIn("Counter32", imports.get("SNMPv2-SMI", []))
        self.assertNotIn(
            "OBJECT-GROUP", imports.get("SNMPv2-CONF", [])
        )
        self.assertEqual(import_reasons[MODULE]["DisplayString"], "SYNTAX type")

    def test_non_constant_macro_import_survives_for_group(self):
        # OBJECT-GROUP is the one conformance macro the pysnmp backend does
        # not put back with its constant imports, so a kept object group keeps
        # the expanded class import itself. NOTIFICATION-GROUP, OBJECT-TYPE
        # and the rest are supplied as constants and stay off the rewritten
        # line even though a kept clause uses them.
        asts, tables = _build_tables(MAIN_MIB)
        new_asts, _tables, _reasons, _imports = prune_modules(
            asts,
            tables,
            {MODULE: ["pmObjectGroup", "pmNotificationGroup"]},
            generated_modules=frozenset({MODULE}),
        )

        conf_imports = new_asts[MODULE][2].get("SNMPv2-CONF", [])
        self.assertIn("ObjectGroup", conf_imports)
        self.assertNotIn("NotificationGroup", conf_imports)

    def test_missing_target_fails(self):
        asts, tables = _build_tables(MAIN_MIB)
        with self.assertRaises(error.PySmiPruneError) as ctx:
            prune_modules(
                asts,
                tables,
                {MODULE: ["pmNoSuchObject"]},
                generated_modules=frozenset({MODULE}),
            )

        self.assertEqual(ctx.exception.mibname, MODULE)

    def test_missing_target_module_fails(self):
        asts, tables = _build_tables(MAIN_MIB)
        with self.assertRaises(error.PySmiPruneError):
            prune_modules(
                asts,
                tables,
                {"NO-SUCH-MIB": ["x"]},
                generated_modules=frozenset({MODULE}),
            )

    def test_unresolvable_dependency_fails(self):
        # A foreign module the compiler generated but that does not define
        # the symbol a kept clause references is a dependency the closure
        # cannot resolve. Drop the referenced column from its symbol table to
        # emulate a module that fails to supply what the MIB says it does.
        asts, tables = _build_tables(MAIN_MIB, DEP_MIB)
        del tables[MODULE]["pmName"]

        with self.assertRaises(error.PySmiPruneError) as ctx:
            prune_modules(
                asts,
                tables,
                {DEP_MODULE: ["pdRef"]},
                generated_modules=frozenset({MODULE, DEP_MODULE}),
            )

        self.assertEqual(ctx.exception.symbol, "pmName")


class CrossModuleTestCase(unittest.TestCase):
    """A referred module is reduced to the referenced symbols, not copied."""

    def test_dependency_module_not_brought_back_wholesale(self):
        asts, tables = _build_tables(MAIN_MIB, DEP_MIB)
        new_asts, new_tables, reasons, imports = prune_modules(
            asts,
            tables,
            {DEP_MODULE: ["pdRef"]},
            generated_modules=frozenset({MODULE, DEP_MODULE}),
        )

        main_kept = reasons[MODULE]
        # Only the referenced column and its row/index/ancestor closure.
        self.assertIn("pmName", main_kept)
        self.assertIn("pmEntry", main_kept)
        for gone in (
            "pmScalar",
            "pmCount",
            "PmState",
            "pmEvent",
            "pmObjectGroup",
            "pmAuxData",
        ):
            self.assertNotIn(gone, main_kept, gone)

        # The consumer still imports the column from the reduced module.
        self.assertEqual(
            new_asts[DEP_MODULE][2][MODULE], ["pmName"]
        )
        self.assertEqual(
            imports[DEP_MODULE]["pmName"],
            "OID ancestor",
        )


class Smiv1FakeIndexTestCase(unittest.TestCase):
    """A bare-type SMIv1 INDEX keeps the column the generator synthesizes."""

    SMIV1_MIB = """
FAKE-IDX-MIB DEFINITIONS ::= BEGIN

testTable OBJECT-TYPE
    SYNTAX  SEQUENCE OF TestEntry
    ACCESS  not-accessible
    STATUS  mandatory
    DESCRIPTION "a table"
    ::= { 1 3 6 1 4 1 99999 1 }

testEntry OBJECT-TYPE
    SYNTAX  TestEntry
    ACCESS  not-accessible
    STATUS  mandatory
    DESCRIPTION "a row"
    INDEX   { INTEGER }
    ::= { testTable 1 }

TestEntry ::= SEQUENCE { testValue INTEGER }

testValue OBJECT-TYPE
    SYNTAX  INTEGER
    ACCESS  read-only
    STATUS  mandatory
    DESCRIPTION "a column"
    ::= { testEntry 1 }

END
"""

    def test_synthetic_column_rides_with_kept_row(self):
        from pysmi.parser.smi import parserFactory
        from pysmi.parser.dialect import smiV1Relaxed

        ast = parserFactory(**smiV1Relaxed)().parse(self.SMIV1_MIB)[0]
        tables = {}
        info, table = SymtableCodeGen().gen_code(ast, tables, repairImports=True)
        tables[info.name] = table

        new_asts, new_tables, reasons, _imports = prune_modules(
            {info.name: ast},
            tables,
            {info.name: ["testValue"]},
            generated_modules=frozenset({info.name}),
        )

        order = new_tables[info.name]["_symtable_order"]
        self.assertIn("pysmiFakeCol1000", order)
        self.assertIn("testEntry", reasons[info.name])

        # The rendered module carries the synthesized column and index.
        source = PySnmpCodeGen().gen_code(
            new_asts[info.name], new_tables, genTexts=False
        )[1]
        self.assertIn("pysmiFakeCol1000", source)
        self.assertIn("setIndexNames", source)


class CompilerIntegrationTestCase(unittest.TestCase):
    """The compiler wires targetSymbols through and reports keep reasons."""

    def _compile(self, targets, sources=None):
        sources = sources or {MODULE: MAIN_MIB}
        written = {}

        def read(mibname, context):
            if mibname not in sources:
                raise error.PySmiReaderFileNotFoundError(
                    mibname=mibname, reader=None
                )
            return sources[mibname]

        compiler = MibCompiler(
            SmiV1CompatParser(),
            PySnmpCodeGen(),
            CallbackWriter(
                lambda mibname, data, ctx: written.update({mibname: data})
            ),
        )
        compiler.add_sources(CallbackReader(read))
        compiler.add_searchers(StubSearcher(*PySnmpCodeGen.baseMibs))

        results = compiler.compile(
            *sources, targetSymbols=targets, genTexts=False
        )
        return results, written

    def test_compile_reports_kept_symbols_and_reasons(self):
        results, written = self._compile({MODULE: ["pmName"]})

        self.assertEqual(str(results[MODULE]), "compiled")
        kept = results[MODULE].keptSymbols
        self.assertIn("pmName", kept)
        self.assertNotIn("pmCount", kept)
        reasons = results[MODULE].keepReasons
        self.assertEqual(reasons["pmName"], "target symbol")
        self.assertEqual(reasons["pmIndex"], "table index object")
        self.assertIn("pmIndex", written[MODULE])
        self.assertNotIn("pmCount", written[MODULE])

    def test_compile_fails_on_missing_target(self):
        results, _written = self._compile({MODULE: ["pmNoSuchObject"]})
        self.assertEqual(str(results[MODULE]), "failed")
        self.assertIsInstance(results[MODULE].error, error.PySmiPruneError)

    def test_full_compile_without_targets_is_unchanged(self):
        results, written = self._compile(None)
        self.assertEqual(str(results[MODULE]), "compiled")
        self.assertIn("pmCount", written[MODULE])
        self.assertIn("pmCompliance", written[MODULE])
