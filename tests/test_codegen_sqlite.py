#
# This file is part of pysmi software.
#
# Copyright (c) 2015-2019, Ilya Etingof <etingof@gmail.com>
# License: https://github.com/pysnmp/pysmi/blob/main/LICENSE.rst
#
import sqlite3
import unittest

from pysmi import jsonio
from pysmi.codegen.sqlite import SqliteCodeGen, bind_parameters, decode_row
from tests.harness import symbol_table
from tests.mibs import SNMPV2_SMI

ROUNDTRIP_MIB = """
TEST-MIB DEFINITIONS ::= BEGIN
IMPORTS
    MODULE-IDENTITY, OBJECT-TYPE, Counter64, Gauge32, IpAddress,
    Integer32, snmpModules
    FROM SNMPv2-SMI;

testMib MODULE-IDENTITY
    LAST-UPDATED "202610040000Z"
    ORGANIZATION "test"
    CONTACT-INFO "test"
    DESCRIPTION "test"
    REVISION "202610040000Z"
    DESCRIPTION "test"
    ::= { snmpModules 9999 }

testTable OBJECT-TYPE
    SYNTAX SEQUENCE OF TestEntry
    MAX-ACCESS not-accessible
    STATUS current
    DESCRIPTION "test"
    ::= { testMib 1 }

testEntry OBJECT-TYPE
    SYNTAX TestEntry
    MAX-ACCESS not-accessible
    STATUS current
    DESCRIPTION "test"
    INDEX { testId, IMPLIED testLabel, testOid }
    ::= { testTable 1 }

TestEntry ::= SEQUENCE {
    testId Integer32,
    testLabel OCTET STRING (SIZE (0..8)),
    testOid OBJECT IDENTIFIER,
    testCounter Counter64,
    testGauge Gauge32,
    testData OCTET STRING,
    testBits BITS,
    testIp IpAddress,
    testStatus INTEGER
}

testId OBJECT-TYPE
    SYNTAX Integer32
    MAX-ACCESS not-accessible
    STATUS current
    DESCRIPTION "test"
    ::= { testEntry 1 }

testLabel OBJECT-TYPE
    SYNTAX OCTET STRING (SIZE (0..8))
    MAX-ACCESS not-accessible
    STATUS current
    DESCRIPTION "test"
    ::= { testEntry 2 }

testOid OBJECT-TYPE
    SYNTAX OBJECT IDENTIFIER
    MAX-ACCESS not-accessible
    STATUS current
    DESCRIPTION "test"
    ::= { testEntry 3 }

testCounter OBJECT-TYPE
    SYNTAX Counter64
    MAX-ACCESS read-only
    STATUS current
    DESCRIPTION "test"
    ::= { testEntry 4 }

testGauge OBJECT-TYPE
    SYNTAX Gauge32
    MAX-ACCESS read-only
    STATUS current
    DESCRIPTION "test"
    ::= { testEntry 5 }

testData OBJECT-TYPE
    SYNTAX OCTET STRING
    MAX-ACCESS read-only
    STATUS current
    DESCRIPTION "test"
    ::= { testEntry 6 }

testBits OBJECT-TYPE
    SYNTAX BITS { one(0), sixteen(15) }
    MAX-ACCESS read-only
    STATUS current
    DESCRIPTION "test"
    ::= { testEntry 7 }

testIp OBJECT-TYPE
    SYNTAX IpAddress
    MAX-ACCESS read-only
    STATUS current
    DESCRIPTION "test"
    ::= { testEntry 8 }

testStatus OBJECT-TYPE
    SYNTAX INTEGER { up(1), down(2) }
    MAX-ACCESS read-only
    STATUS current
    DESCRIPTION "test"
    ::= { testEntry 9 }
END
"""

BASE_MIB = """
BASE-MIB DEFINITIONS ::= BEGIN
IMPORTS
    MODULE-IDENTITY, OBJECT-TYPE, Integer32, snmpModules
    FROM SNMPv2-SMI;

baseMib MODULE-IDENTITY
    LAST-UPDATED "202610040000Z"
    ORGANIZATION "test"
    CONTACT-INFO "test"
    DESCRIPTION "test"
    REVISION "202610040000Z"
    DESCRIPTION "test"
    ::= { snmpModules 9998 }

baseTable OBJECT-TYPE
    SYNTAX SEQUENCE OF BaseEntry
    MAX-ACCESS not-accessible
    STATUS current
    DESCRIPTION "test"
    ::= { baseMib 1 }

baseEntry OBJECT-TYPE
    SYNTAX BaseEntry
    MAX-ACCESS not-accessible
    STATUS current
    DESCRIPTION "test"
    INDEX { baseId, baseOid }
    ::= { baseTable 1 }

BaseEntry ::= SEQUENCE {
    baseId Integer32,
    baseOid OBJECT IDENTIFIER,
    baseValue Integer32
}

baseId OBJECT-TYPE
    SYNTAX Integer32
    MAX-ACCESS not-accessible
    STATUS current
    DESCRIPTION "test"
    ::= { baseEntry 1 }

baseOid OBJECT-TYPE
    SYNTAX OBJECT IDENTIFIER
    MAX-ACCESS not-accessible
    STATUS current
    DESCRIPTION "test"
    ::= { baseEntry 2 }

baseValue OBJECT-TYPE
    SYNTAX Integer32
    MAX-ACCESS read-only
    STATUS current
    DESCRIPTION "test"
    ::= { baseEntry 3 }
END
"""

EXT_MIB = """
EXT-MIB DEFINITIONS ::= BEGIN
IMPORTS
    MODULE-IDENTITY, OBJECT-TYPE, Integer32, snmpModules
    FROM SNMPv2-SMI
    baseEntry
    FROM BASE-MIB;

extMib MODULE-IDENTITY
    LAST-UPDATED "202610040000Z"
    ORGANIZATION "test"
    CONTACT-INFO "test"
    DESCRIPTION "test"
    REVISION "202610040000Z"
    DESCRIPTION "test"
    ::= { snmpModules 9997 }

extTable OBJECT-TYPE
    SYNTAX SEQUENCE OF ExtEntry
    MAX-ACCESS not-accessible
    STATUS current
    DESCRIPTION "test"
    ::= { extMib 1 }

extEntry OBJECT-TYPE
    SYNTAX ExtEntry
    MAX-ACCESS not-accessible
    STATUS current
    DESCRIPTION "test"
    AUGMENTS { baseEntry }
    ::= { extTable 1 }

ExtEntry ::= SEQUENCE {
    extValue Integer32
}

extValue OBJECT-TYPE
    SYNTAX Integer32
    MAX-ACCESS read-only
    STATUS current
    DESCRIPTION "test"
    ::= { extEntry 1 }
END
"""


RESERVED_INDEX_MIB = """
RESERVED-MIB DEFINITIONS ::= BEGIN
IMPORTS
    MODULE-IDENTITY, OBJECT-TYPE, Integer32, snmpModules
    FROM SNMPv2-SMI;

reservedMib MODULE-IDENTITY
    LAST-UPDATED "202610040000Z"
    ORGANIZATION "test"
    CONTACT-INFO "test"
    DESCRIPTION "test"
    REVISION "202610040000Z"
    DESCRIPTION "test"
    ::= { snmpModules 9995 }

reservedTable OBJECT-TYPE
    SYNTAX SEQUENCE OF ReservedEntry
    MAX-ACCESS not-accessible
    STATUS current
    DESCRIPTION "test"
    ::= { reservedMib 1 }

reservedEntry OBJECT-TYPE
    SYNTAX ReservedEntry
    MAX-ACCESS not-accessible
    STATUS current
    DESCRIPTION "test"
    INDEX { order }
    ::= { reservedTable 1 }

ReservedEntry ::= SEQUENCE {
    order Integer32,
    collideA Integer32,
    collidea Integer32
}

order OBJECT-TYPE
    SYNTAX Integer32
    MAX-ACCESS not-accessible
    STATUS current
    DESCRIPTION "test"
    ::= { reservedEntry 1 }

collideA OBJECT-TYPE
    SYNTAX Integer32
    MAX-ACCESS read-only
    STATUS current
    DESCRIPTION "test"
    ::= { reservedEntry 2 }

collidea OBJECT-TYPE
    SYNTAX Integer32
    MAX-ACCESS read-only
    STATUS current
    DESCRIPTION "test"
    ::= { reservedEntry 3 }
END
"""


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def mapping(mib, dependencies=()):
    ast, _module, symbols = symbol_table(mib, list(dependencies))
    _info, text = SqliteCodeGen().gen_code(ast, symbols)
    return jsonio.loads(text)


class SqliteCodeGenTestCase(unittest.TestCase):
    def test_symbol_table_keeps_index_and_augmentation_metadata(self):
        _ast, _module, symbols = symbol_table(
            EXT_MIB, [SNMPV2_SMI, BASE_MIB]
        )

        self.assertEqual(
            symbols["BASE-MIB"]["baseEntry"]["indices"],
            (
                (0, "BASE-MIB", "baseId"),
                (0, "BASE-MIB", "baseOid"),
            ),
        )
        self.assertEqual(
            symbols["EXT-MIB"]["extEntry"]["augmentation"],
            {"module": "BASE-MIB", "object": "baseEntry"},
        )

    def test_generated_ddl_roundtrip_preserves_boundary_and_index_values(self):
        doc = mapping(ROUNDTRIP_MIB, (SNMPV2_SMI,))
        table = doc["tables"][0]
        values = {
            "testId": 2147483647,
            "testLabel": b"",
            "testOid": (1, 4294967294, 7, 8, 9),
            "testCounter": 18446744073709551615,
            "testGauge": 4294967295,
            "testData": b"\x00\xff\x00",
            "testBits": b"\x80\x00",
            "testIp": b"\xff\x00\xff\x00",
            "testStatus": 1,
        }
        parameters = bind_parameters(table, values)

        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            connection.execute(doc["ddl"][0])
            connection.execute(table["insert_sql"], parameters)
            connection.commit()
            raw = connection.execute(
                "SELECT * FROM " + _quote(table["table"]["name"])
            ).fetchone()
            restored = decode_row(table, raw)
        finally:
            connection.close()

        self.assertEqual(raw["testCounter"], "18446744073709551615")
        self.assertEqual(raw["testGauge"], 4294967295)
        self.assertEqual(
            table["fields"]["testGauge"]["representation"], "unsigned32"
        )
        self.assertIsInstance(raw["testData"], bytes)
        self.assertIsInstance(raw["testLabel"], bytes)
        self.assertIsInstance(raw["testBits"], bytes)
        self.assertEqual(raw["testOid"], "1.4294967294.7.8.9")
        self.assertEqual(restored, values)

    def test_cross_module_augments_reuses_the_base_primary_key(self):
        base_doc = mapping(BASE_MIB, (SNMPV2_SMI,))
        ext_doc = mapping(EXT_MIB, (SNMPV2_SMI, BASE_MIB))
        base_table = base_doc["tables"][0]
        ext_table = ext_doc["tables"][0]

        self.assertEqual(ext_table["primary_key"], ["baseId", "baseOid"])
        self.assertEqual(ext_table["row"]["indexed_by"], "BASE-MIB::baseEntry")
        self.assertEqual(
            ext_table["fields"]["BASE-MIB.baseId"]["module"], "BASE-MIB"
        )

        base_values = {
            "baseId": 7,
            "baseOid": (1, 3, 6, 4294967294),
            "baseValue": 11,
        }
        ext_values = {
            "BASE-MIB.baseId": 7,
            "BASE-MIB.baseOid": (1, 3, 6, 4294967294),
            "extValue": 22,
        }

        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            connection.execute(base_doc["ddl"][0])
            connection.execute(ext_doc["ddl"][0])
            connection.execute(
                base_table["insert_sql"], bind_parameters(base_table, base_values)
            )
            connection.execute(
                ext_table["insert_sql"], bind_parameters(ext_table, ext_values)
            )
            connection.commit()
            raw = connection.execute(
                "SELECT * FROM " + _quote(ext_table["table"]["name"])
            ).fetchone()
            restored = decode_row(ext_table, raw)
        finally:
            connection.close()

        self.assertEqual(
            restored,
            {
                "BASE-MIB.baseId": 7,
                "BASE-MIB.baseOid": (1, 3, 6, 4294967294),
                "extValue": 22,
            },
        )

    def test_reserved_words_and_case_insensitive_name_collisions(self):
        first = mapping(RESERVED_INDEX_MIB, (SNMPV2_SMI,))
        second = mapping(RESERVED_INDEX_MIB, (SNMPV2_SMI,))
        table = first["tables"][0]

        self.assertEqual(first["ddl"], second["ddl"])
        self.assertIn('"order" INTEGER NOT NULL', table["ddl"])
        self.assertIn('"collideA" INTEGER', table["ddl"])
        self.assertIn('"collidea_2" INTEGER', table["ddl"])
        self.assertEqual(table["fields"]["order"]["column"], "order")
        self.assertEqual(table["primary_key"], ["order"])
        self.assertEqual(table["fields"]["collidea"]["column"], "collidea_2")

        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            for statement in first["ddl"]:
                connection.execute(statement)
            parameters = bind_parameters(
                table, {"order": 1, "collideA": 2, "collidea": 3}
            )
            connection.execute(table["insert_sql"], parameters)
            raw = connection.execute(
                "SELECT * FROM " + _quote(table["table"]["name"])
            ).fetchone()
        finally:
            connection.close()

        self.assertEqual(
            decode_row(table, raw),
            {
                "order": 1,
                "collideA": 2,
                "collidea": 3,
            },
        )


if __name__ == "__main__":
    unittest.main()
