#
# This file is part of pysmi software.
#
# Copyright (c) 2015-2020, Ilya Etingof <etingof@gmail.com>
# License: https://github.com/pysnmp/pysmi/blob/main/LICENSE.rst
#
"""A DISPLAY-HINT compiles into a converter for the JSON model.

The JSON backend carries ``displayhint`` verbatim; pysmi.displayhint turns it
into a formatter and, for hints that segment unambiguously, a parser. These
tests pin the conversion semantics of RFC 2579 section 3.1 with fixed
samples -- a physical address, a DateAndTime-style octet string and scaled
integers -- and the refusals: ambiguous hints, short repeat counts and
trailing characters are errors, never guesses.
"""

import sys
import unittest

from pysmi.displayhint import compile_display_hint
from pysmi.error import PySmiDisplayHintError

from tests.harness import render_json

MAC_TYPE = {
    "type": "OCTET STRING",
    "class": "type",
    "constraints": {"size": [{"min": 6, "max": 6}]},
}

DATE_AND_TIME_TYPE = {
    "type": "OCTET STRING",
    "class": "type",
    "constraints": {"size": [{"min": 8, "max": 8}, {"min": 11, "max": 11}]},
}

SCALED_TYPE = {
    "type": "INTEGER",
    "class": "type",
    "constraints": {"range": [{"min": 0, "max": 1000000}]},
}

#: 2026-10-03, 15:04:30.0, UTC+0:0 -- the DateAndTime wire form.
DATE_AND_TIME_VALUE = bytes([0x07, 0xEA, 10, 3, 15, 4, 30, 0, 0x2B, 0, 0])
DATE_AND_TIME_TEXT = "2026-10-3,15:4:30.0,+0:0"


class IntegerHintTestCase(unittest.TestCase):
    """Radix and decimal scaling for INTEGER hints."""

    def testDecimalScalingRoundTrip(self):
        conv = compile_display_hint("d-2", SCALED_TYPE)
        self.assertEqual(conv.kind, "integer")
        self.assertEqual(conv.format(12345), "123.45")
        self.assertEqual(conv.format(5), "0.05")
        self.assertEqual(conv.format(100), "1.00")
        self.assertEqual(conv.parse("123.45"), 12345)
        self.assertEqual(conv.parse("0.05"), 5)
        self.assertEqual(conv.parse("123"), 12300)

    def testNegativeScaledValue(self):
        conv = compile_display_hint("d-1")
        self.assertEqual(conv.format(-255), "-25.5")
        self.assertEqual(conv.parse("-25.5"), -255)

    def testRadixFormats(self):
        self.assertEqual(compile_display_hint("x").format(255), "ff")
        self.assertEqual(compile_display_hint("o").format(8), "10")
        self.assertEqual(compile_display_hint("b").format(5), "101")
        self.assertEqual(compile_display_hint("x").parse("ff"), 255)
        self.assertEqual(compile_display_hint("o").parse("10"), 8)
        self.assertEqual(compile_display_hint("b").parse("101"), 5)
        self.assertEqual(compile_display_hint("d").parse("-42"), -42)

    def testRangeIsHonouredBothWays(self):
        conv = compile_display_hint("d", SCALED_TYPE)
        with self.assertRaises(PySmiDisplayHintError):
            conv.format(1000001)
        with self.assertRaises(PySmiDisplayHintError):
            conv.parse("1000001")
        self.assertEqual(conv.check(1000000), 1000000)

    def testParseReportsPositions(self):
        conv = compile_display_hint("d")
        with self.assertRaises(PySmiDisplayHintError) as ctx:
            conv.parse("12x")
        self.assertEqual(ctx.exception.position, 2)
        with self.assertRaises(PySmiDisplayHintError) as ctx:
            conv.parse("")
        self.assertEqual(ctx.exception.position, 0)

    def testSurplusDecimalPlacesAreRefused(self):
        conv = compile_display_hint("d-2")
        with self.assertRaises(PySmiDisplayHintError) as ctx:
            conv.parse("1.234")
        self.assertEqual(ctx.exception.position, 4)

    def testMalformedIntegerHints(self):
        for hint in ("", "d-", "d-0", "x-2", "q"):
            with self.subTest(hint=hint):
                with self.assertRaises(PySmiDisplayHintError):
                    compile_display_hint(hint)


class OctetStringHintTestCase(unittest.TestCase):
    """Length, format, separator, terminator and repeat of octet hints."""

    def testPhysicalAddressRoundTrip(self):
        conv = compile_display_hint("1x:", MAC_TYPE)
        value = bytes.fromhex("001b44113ab7")
        self.assertEqual(conv.format(value), "00:1b:44:11:3a:b7")
        self.assertEqual(conv.parse("00:1b:44:11:3a:b7"), value)
        self.assertEqual(conv.parse("00:1B:44:11:3A:B7"), value)

    def testDateAndTimeRoundTrip(self):
        conv = compile_display_hint("2d-1d-1d,1d:1d:1d.1d,1a1d:1d",
                                    DATE_AND_TIME_TYPE)
        self.assertTrue(conv.reversible)
        self.assertEqual(conv.format(DATE_AND_TIME_VALUE), DATE_AND_TIME_TEXT)
        self.assertEqual(conv.parse(DATE_AND_TIME_TEXT), DATE_AND_TIME_VALUE)
        # The 8-octet form carries no timezone and stops mid-hint.
        self.assertEqual(conv.format(DATE_AND_TIME_VALUE[:8]),
                         "2026-10-3,15:4:30.0")
        self.assertEqual(conv.parse("2026-10-3,15:4:30.0"),
                         DATE_AND_TIME_VALUE[:8])

    def testDottedDecimalQuad(self):
        conv = compile_display_hint(
            "1d.1d.1d.1d",
            {"type": "OCTET STRING",
             "constraints": {"size": [{"min": 4, "max": 4}]}},
        )
        value = bytes([192, 168, 0, 1])
        self.assertEqual(conv.format(value), "192.168.0.1")
        self.assertEqual(conv.parse("192.168.0.1"), value)

    def testGroupValueMustFitItsOctets(self):
        conv = compile_display_hint(
            "1d.1d.1d.1d",
            {"type": "OCTET STRING",
             "constraints": {"size": [{"min": 4, "max": 4}]}},
        )
        with self.assertRaises(PySmiDisplayHintError) as ctx:
            conv.parse("192.168.0.256")
        self.assertEqual(ctx.exception.position, 10)
        self.assertEqual(ctx.exception.segment, 3)

    def testRepeatIndicatorConsumesCountOctet(self):
        conv = compile_display_hint("*1x:")
        self.assertEqual(conv.format(bytes([2, 0xAA, 0xBB])), "aabb")
        self.assertEqual(conv.format(bytes([1, 0xAA, 1, 0xBB])), "aa:bb")

    def testRepeatTerminatorFollowsEachRepetition(self):
        conv = compile_display_hint("*1x:/")
        self.assertEqual(conv.format(bytes([2, 0xAA, 0xBB])), "aa/bb/")

    def testRepeatWithTrailingSpecification(self):
        # SnmpOSIAddress: a count octet, that many hex pairs each closed by
        # the terminator, then the rest of the value as hex pairs.
        conv = compile_display_hint("*1x:/1x:")
        value = bytes([3, 0xAA, 0xBB, 0xCC, 0xDD])
        self.assertEqual(conv.format(value), "aa/bb/cc/dd")

    def testShortRepeatCountIsAnError(self):
        conv = compile_display_hint("*1x:")
        with self.assertRaises(PySmiDisplayHintError) as ctx:
            conv.format(bytes([3, 0xAA, 0xBB]))
        self.assertEqual(ctx.exception.segment, 0)
        self.assertIn("repeat count 3", str(ctx.exception))

    def testEmptyValue(self):
        conv = compile_display_hint("1x:")
        self.assertEqual(conv.format(b""), "")
        self.assertEqual(conv.parse(""), b"")
        with self.assertRaises(PySmiDisplayHintError):
            compile_display_hint("1x:", MAC_TYPE).format(b"")
        with self.assertRaises(PySmiDisplayHintError):
            compile_display_hint("1x:", MAC_TYPE).parse("")

    def testSizeIsHonouredBothWays(self):
        conv = compile_display_hint("1x:", MAC_TYPE)
        with self.assertRaises(PySmiDisplayHintError):
            conv.format(bytes.fromhex("001b44113a"))
        with self.assertRaises(PySmiDisplayHintError):
            conv.parse("00:1b:44:11:3a")
        with self.assertRaises(PySmiDisplayHintError):
            conv.parse("00:1b:44:11:3a:b7:ff")

    def testTrailingCharactersAreRefused(self):
        conv = compile_display_hint("1x:", MAC_TYPE)
        with self.assertRaises(PySmiDisplayHintError) as ctx:
            conv.parse("00:1b:44:11:3a:b7:")
        self.assertEqual(ctx.exception.position, 17)
        with self.assertRaises(PySmiDisplayHintError) as ctx:
            conv.parse("00:1b:44:11:3a:b7::")
        self.assertIsNotNone(ctx.exception.position)

    def testShortGroupIsAnError(self):
        conv = compile_display_hint("1x:")
        with self.assertRaises(PySmiDisplayHintError) as ctx:
            conv.parse("0")
        self.assertEqual(ctx.exception.position, 0)

    def testMalformedOctetHints(self):
        for hint in ("1", "0x", "1q", "1x:;"):
            with self.subTest(hint=hint):
                with self.assertRaises(PySmiDisplayHintError):
                    compile_display_hint(hint)


class AmbiguousHintTestCase(unittest.TestCase):
    """Hints that cannot be segmented are refused, never guessed."""

    def testRepeatIndicatorIsIrreversible(self):
        conv = compile_display_hint("*1x:")
        self.assertFalse(conv.reversible)
        self.assertIn("repeat indicator", conv.irreversible_reason)
        with self.assertRaises(PySmiDisplayHintError) as ctx:
            conv.parse("aabb")
        self.assertIn("cannot be reversed", str(ctx.exception))

    def testBareDecimalGroupIsIrreversible(self):
        conv = compile_display_hint("1d")
        self.assertFalse(conv.reversible)
        with self.assertRaises(PySmiDisplayHintError):
            conv.parse("123")

    def testTextFormatWithSeparatorIsIrreversible(self):
        conv = compile_display_hint("1a:1a")
        self.assertFalse(conv.reversible)
        self.assertIn("separator", conv.irreversible_reason)

    def testHexSeparatorLetterIsIrreversible(self):
        conv = compile_display_hint("1xa")
        self.assertFalse(conv.reversible)

    def testSizeConfinedDecimalIsReversible(self):
        # SIZE (6) confines "1d.1d.1d.1d/2d" to a single pass, so the
        # trailing separatorless decimal runs to the end of the text.
        conv = compile_display_hint(
            "1d.1d.1d.1d/2d",
            {"type": "OCTET STRING",
             "constraints": {"size": [{"min": 6, "max": 6}]}},
        )
        self.assertTrue(conv.reversible)
        value = bytes([192, 168, 0, 1, 0x1F, 0x90])
        self.assertEqual(conv.format(value), "192.168.0.1/8080")
        self.assertEqual(conv.parse("192.168.0.1/8080"), value)


class JsonModelTestCase(unittest.TestCase):
    """The converter consumes the nodes of the generated JSON model."""

    MIB = """
TEST-MIB DEFINITIONS ::= BEGIN
IMPORTS
    TEXTUAL-CONVENTION
        FROM SNMPv2-TC;

TestMac ::= TEXTUAL-CONVENTION
    DISPLAY-HINT "1x:"
    STATUS current
    DESCRIPTION "A physical address."
    SYNTAX OCTET STRING (SIZE (6))

TestScaled ::= TEXTUAL-CONVENTION
    DISPLAY-HINT "d-2"
    STATUS current
    DESCRIPTION "A scaled integer."
    SYNTAX INTEGER (0..1000000)

END
"""

    def testConverterFromJsonTypeNode(self):
        doc = render_json(self.MIB)
        mac = compile_display_hint(
            doc["TestMac"]["displayhint"], doc["TestMac"]["type"]
        )
        self.assertEqual(mac.format(bytes.fromhex("001b44113ab7")),
                         "00:1b:44:11:3a:b7")
        with self.assertRaises(PySmiDisplayHintError):
            mac.format(bytes.fromhex("001b44113a"))

    def testConverterFromWholeConventionNode(self):
        doc = render_json(self.MIB)
        scaled = compile_display_hint(
            doc["TestScaled"]["displayhint"], doc["TestScaled"]
        )
        self.assertEqual(scaled.format(12345), "123.45")
        self.assertEqual(scaled.parse("123.45"), 12345)
        with self.assertRaises(PySmiDisplayHintError):
            scaled.parse("1000000.00")

    def testHintMustMatchTheSyntaxKind(self):
        with self.assertRaises(PySmiDisplayHintError):
            compile_display_hint("d", "OCTET STRING")
        with self.assertRaises(PySmiDisplayHintError):
            compile_display_hint("1x:", "INTEGER")


suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])

if __name__ == "__main__":
    unittest.TextTestRunner(verbosity=2).run(suite)
