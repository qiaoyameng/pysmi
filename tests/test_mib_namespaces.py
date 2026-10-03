"""
Version namespaces let incompatible copies of one MIB name load together.
"""

import pathlib
import tempfile
import textwrap
import unittest

from pysmi import error
from pysmi.codegen import JsonCodeGen
from pysmi.codegen import PySnmpCodeGen
from pysmi.compiler import MibCompiler
from pysmi.parser import SmiV2Parser
from pysmi.reader.base import AbstractReader
from pysmi.searcher import StubSearcher
from pysmi.writer import CallbackWriter
from pysmi.writer.pyfile import PyFileWriter


class DictReader(AbstractReader):
    isLocal = True

    def __init__(self, modules):
        self.modules = modules

    def get_data(self, mibname, **options):
        from pysmi.mibinfo import MibInfo

        try:
            data = self.modules[mibname]
        except KeyError:
            raise error.PySmiReaderFileNotFoundError(
                mibname=mibname, reader=self
            ) from None
        return MibInfo(name=mibname, path=f"source://{mibname}"), data

    def list_mibs(self):
        return tuple(self.modules)


def dependency(namespace, enum_value, index_oid):
    return textwrap.dedent(
        f"""\
        DEP-MIB DEFINITIONS ::= BEGIN
        IMPORTS
            MODULE-IDENTITY, OBJECT-TYPE, Integer32, enterprises
                FROM SNMPv2-SMI
            TEXTUAL-CONVENTION
                FROM SNMPv2-TC;

        depMib MODULE-IDENTITY
            LAST-UPDATED "202401010000Z"
            ORGANIZATION "test"
            CONTACT-INFO "test"
            DESCRIPTION "namespace {namespace} dependency"
            REVISION "202401010000Z"
            DESCRIPTION "initial"
            ::= {{ enterprises {index_oid} }}

        DepStatus ::= TEXTUAL-CONVENTION
            STATUS current
            DESCRIPTION "namespace-specific enum"
            SYNTAX INTEGER {{
                namespaceValue({enum_value})
            }}

        depValue OBJECT-TYPE
            SYNTAX DepStatus
            MAX-ACCESS read-only
            STATUS current
            DESCRIPTION "namespace-specific enum value"
            ::= {{ depMib 1 }}
        END
        """
    )


def conflict_mib(root, enum_value, index_oid):
    return textwrap.dedent(
        f"""\
        CONFLICT-MIB DEFINITIONS ::= BEGIN
        IMPORTS
            MODULE-IDENTITY, OBJECT-TYPE, Integer32, enterprises
                FROM SNMPv2-SMI
            DepStatus, depValue
                FROM DEP-MIB;

        conflictMib MODULE-IDENTITY
            LAST-UPDATED "202401010000Z"
            ORGANIZATION "test"
            CONTACT-INFO "test"
            DESCRIPTION "incompatible vendor copy"
            REVISION "202401010000Z"
            DESCRIPTION "initial"
            ::= {{ enterprises {root} }}

        conflictScalar OBJECT-TYPE
            SYNTAX DepStatus
            MAX-ACCESS read-only
            STATUS current
            DESCRIPTION "takes the dependency enum from this namespace"
            DEFVAL {{ namespaceValue }}
            ::= {{ conflictMib 1 }}

        conflictTable OBJECT-TYPE
            SYNTAX SEQUENCE OF ConflictEntry
            MAX-ACCESS not-accessible
            STATUS current
            DESCRIPTION "namespace-specific table"
            ::= {{ conflictMib 2 }}

        conflictEntry OBJECT-TYPE
            SYNTAX ConflictEntry
            MAX-ACCESS not-accessible
            STATUS current
            DESCRIPTION "namespace-specific row"
            INDEX {{ conflictIndex }}
            ::= {{ conflictTable 1 }}

        ConflictEntry ::= SEQUENCE {{
            conflictIndex Integer32
        }}

        conflictIndex OBJECT-TYPE
            SYNTAX Integer32 ({index_oid}..{index_oid})
            MAX-ACCESS not-accessible
            STATUS current
            DESCRIPTION "namespace-specific index range"
            ::= {{ conflictEntry 1 }}
        END
        """
    )


def shared_mib():
    return textwrap.dedent(
        """\
        SHARED-PUBLIC-MIB DEFINITIONS ::= BEGIN
        IMPORTS
            MODULE-IDENTITY, OBJECT-TYPE, Integer32, enterprises
                FROM SNMPv2-SMI;

        sharedPublicMib MODULE-IDENTITY
            LAST-UPDATED "202401010000Z"
            ORGANIZATION "test"
            CONTACT-INFO "test"
            DESCRIPTION "shared public dependency"
            REVISION "202401010000Z"
            DESCRIPTION "initial"
            ::= { enterprises 99999 }

        sharedPublicValue OBJECT-TYPE
            SYNTAX Integer32
            MAX-ACCESS read-only
            STATUS current
            DESCRIPTION "shared"
            ::= { sharedPublicMib 1 }
        END
        """
    )


def sharing_mib(root):
    return textwrap.dedent(
        f"""\
        SHARING-MIB DEFINITIONS ::= BEGIN
        IMPORTS
            MODULE-IDENTITY, enterprises
                FROM SNMPv2-SMI
            sharedPublicValue
                FROM SHARED-PUBLIC-MIB;

        sharingMib MODULE-IDENTITY
            LAST-UPDATED "202401010000Z"
            ORGANIZATION "test"
            CONTACT-INFO "test"
            DESCRIPTION "uses a shared public dependency"
            REVISION "202401010000Z"
            DESCRIPTION "initial"
            ::= {{ enterprises {root} }}
        END
        """
    )


def ambiguous_mib():
    return textwrap.dedent(
        """\
        AMBIGUOUS-USER-MIB DEFINITIONS ::= BEGIN
        IMPORTS
            MODULE-IDENTITY, enterprises
                FROM SNMPv2-SMI
            depValue
                FROM DEP-MIB;

        ambiguousUserMib MODULE-IDENTITY
            LAST-UPDATED "202401010000Z"
            ORGANIZATION "test"
            CONTACT-INFO "test"
            DESCRIPTION "imports an ambiguous dependency"
            REVISION "202401010000Z"
            DESCRIPTION "initial"
            ::= { enterprises 88888 }
        END
        """
    )


class MibNamespaceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output = pathlib.Path(self.tmp.name) / "mibs"
        self.writer = PyFileWriter(str(self.output))
        self.writer.pyCompile = False

    def tearDown(self):
        self.tmp.cleanup()

    def compiler(self, **sources):
        compiler = MibCompiler(
            SmiV2Parser(),
            PySnmpCodeGen(),
            self.writer,
            useBundledMibs=True,
        )
        compiler.add_searchers(StubSearcher(*PySnmpCodeGen.baseMibs))
        for namespace, modules in sources.items():
            if namespace:
                compiler.add_sources(DictReader(modules), namespace=namespace)
            else:
                compiler.add_sources(DictReader(modules))
        return compiler

    def compile_namespace(self, namespace, root, dep_oid):
        compiler = self.compiler(
            **{
                namespace: {
                    "CONFLICT-MIB": conflict_mib(root, dep_oid, dep_oid),
                    "DEP-MIB": dependency(namespace, dep_oid, dep_oid),
                    "SHARING-MIB": sharing_mib(root + 10),
                },
                "": {"SHARED-PUBLIC-MIB": shared_mib()},
            }
        )
        status = compiler.compile(
            "CONFLICT-MIB", "SHARING-MIB", namespace=namespace, rebuild=True
        )
        self.assertNotIn("failed", set(status.values()))
        return status

    def test_incompatible_copies_load_in_one_mib_builder(self):
        first = self.compile_namespace("vendor1", 50001, 11)
        second = self.compile_namespace("vendor2", 50002, 22)

        expected_first = {
            "vendor1@CONFLICT-MIB",
            "vendor1@DEP-MIB",
            "vendor1@SHARING-MIB",
            "SHARED-PUBLIC-MIB",
        }
        expected_second = {
            "vendor2@CONFLICT-MIB",
            "vendor2@DEP-MIB",
            "vendor2@SHARING-MIB",
            "SHARED-PUBLIC-MIB",
        }
        self.assertTrue(expected_first.issubset(first))
        self.assertTrue(expected_second.issubset(second))
        self.assertEqual("CONFLICT-MIB", first["vendor1@CONFLICT-MIB"].sourceName)
        self.assertEqual("vendor1", first["vendor1@CONFLICT-MIB"].namespace)

        source = (self.output / "vendor1@CONFLICT-MIB.py").read_text()
        other = (self.output / "vendor2@CONFLICT-MIB.py").read_text()
        self.assertIn('importSymbols("vendor1@DEP-MIB"', source)
        self.assertIn('importSymbols("vendor2@DEP-MIB"', other)
        self.assertIn("PySNMP MIB module vendor1@CONFLICT-MIB", source)
        self.assertIn("PySNMP MIB module vendor2@CONFLICT-MIB", other)
        self.assertIn('exportSymbols("vendor1@CONFLICT-MIB"', source)
        self.assertIn('exportSymbols("vendor2@CONFLICT-MIB"', other)
        self.assertIn("PYSNMP_ORIGINAL_MODULE = 'CONFLICT-MIB'", source)
        self.assertIn("PYSNMP_MIB_NAMESPACE = 'vendor1'", source)

    def test_ambiguous_dependency_binding_is_an_error(self):
        compiler = self.compiler(
            vendor1={
                "AMBIGUOUS-USER-MIB": ambiguous_mib(),
                "DEP-MIB": dependency("vendor1", 11, 11),
            },
            **{"": {"DEP-MIB": dependency("shared", 33, 33)}},
        )
        with self.assertRaisesRegex(
            error.PySmiError, "ambiguous binding for MIB DEP-MIB"
        ):
            compiler.compile("AMBIGUOUS-USER-MIB", namespace="vendor1", rebuild=True)

    def test_unnamespaced_compile_remains_available(self):
        compiler = self.compiler(
            **{"": {"SHARED-PUBLIC-MIB": shared_mib()}}
        )
        status = compiler.compile("SHARED-PUBLIC-MIB", rebuild=True)
        self.assertEqual("compiled", status["SHARED-PUBLIC-MIB"])
        self.assertFalse((self.output / "vendor1@SHARED-PUBLIC-MIB.py").exists())
        source = (self.output / "SHARED-PUBLIC-MIB.py").read_text()
        self.assertIn('exportSymbols("SHARED-PUBLIC-MIB"', source)
        self.assertNotIn("PYSNMP_MIB_NAMESPACE", source)

    def test_namespace_requires_pysnmp_code_generator(self):
        compiler = MibCompiler(
            SmiV2Parser(),
            JsonCodeGen(),
            CallbackWriter(lambda *args: None),
            useBundledMibs=False,
        )
        compiler.add_sources(
            DictReader({"SHARED-PUBLIC-MIB": shared_mib()}),
            namespace="vendor1",
        )

        with self.assertRaisesRegex(error.PySmiCodegenError, "does not support"):
            compiler.compile(
                "SHARED-PUBLIC-MIB", namespace="vendor1", rebuild=True
            )

    def test_namespace_does_not_read_other_namespace_sources(self):
        compiler = self.compiler(
            vendor1={
                "AMBIGUOUS-USER-MIB": ambiguous_mib(),
            },
            vendor2={"DEP-MIB": dependency("vendor2", 22, 22)},
        )
        status = compiler.compile(
            "AMBIGUOUS-USER-MIB", namespace="vendor1", rebuild=True
        )
        self.assertEqual("missing", status["DEP-MIB"])
        self.assertEqual("unprocessed", status["vendor1@AMBIGUOUS-USER-MIB"])

    def test_set_sources_can_replace_one_namespace(self):
        compiler = self.compiler(
            vendor1={"CONFLICT-MIB": conflict_mib(1, 1, 1)},
            vendor2={"CONFLICT-MIB": conflict_mib(2, 2, 2)},
        )
        replacement = DictReader({"CONFLICT-MIB": conflict_mib(3, 3, 3)})

        compiler.set_sources(replacement, namespace="vendor1")

        self.assertEqual(
            [namespace for _, namespace in compiler._sources],
            ["vendor2", "vendor1"],
        )


if __name__ == "__main__":
    unittest.main()
