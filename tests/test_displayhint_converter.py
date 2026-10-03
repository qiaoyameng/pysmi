#
# This file is part of pysmi software.
#
# Copyright (c) 2015-2020, Ilya Etingof <etingof@gmail.com>
# License: https://github.com/pysnmp/pysmi/blob/main/LICENSE.rst
#
"""DISPLAY-HINT strings compile into format/parse converters.

The JSON model carries a textual convention's ``displayhint`` verbatim
(see ``tests.test_display_hint``) but never interprets it.
``pysmi.displayhint`` closes that gap for the consumer of the model:
``compile_display_hint`` turns the hint into a converter whose ``format``
renders a native value and whose ``parse`` recovers it.

The fixed samples are the ones RFCs and deployed MIBs actually use -- a
physical (MAC) address under ``1x:``, DateAndTime's
``2d-1d-1d,1d:1d:1d.1d,1a1d:1d`` and the SCTE tenths/hundredths integers.
Reverse parsing is deliberately refused whenever the hint leaves the
segmentation ambiguous rather than guessing a value.
"""

import sys
import unittest

from tests.harness import render_json

from pysmi.displayhint import (
    DisplayHintConstraintError,
    DisplayHintConverter,
    DisplayHintSyntaxError,
    DisplayHintTypeError,
    DisplayHintUnparseableError,
    DisplayHintValueError,
    compile_display_hint,
    compile_json_type,
)

#: The eight-octet local-time DateAndTime from RFC 2579's example, and the
#: eleven-octet form with its timezone fields.
DATETIME_8 = bytes([0x07, 0xC8, 5, 26, 13, 30, 15, 0])
DATETIME_11 = DATETIME_8 + bytes([ord("-"), 4, 0])

DATETIME_HINT = "2d-1d-1d,1d:1d:1d.1d,1a1d:1d"
DATETIME_SIZE = {"size": [{"min": 8, "max": 8}, {"min": 11, "max": 11}]}


def _capture(testcase, exc, fn):
    """Run fn expecting *exc* and return the raised exception."""
    with testcase.assertRaises(exc) as context:
        fn()
    return context.exception



class FixedSampleTestCase(unittest.TestCase):
    """The physical address, date and scaled-integer samples render and
    reverse exactly as the standards show."""

    def testMacAddress(self):
        mac = compile_display_hint(
            "1x:", "OCTET STRING", {"size": [{"min": 6, "max": 6}]}
        )
        value = bytes.fromhex("00000c123456")
        self.assertEqual(mac.format(value), "00:00:0c:12:34:56")
        self.assertEqual(mac.parse("00:00:0c:12:34:56"), value)

    def testPhysicalAddressIsLengthAgnostic(self):
        # PhysAddress carries no SIZE; the single 1x specification repeats
        # implicitly over however many octets the address has.
        phys = compile_display_hint("1x:", "OCTET STRING")
        for value in (bytes.fromhex("00"), bytes.fromhex("0001"),
                      bytes.fromhex("0102030405060708")):
            self.assertEqual(phys.parse(phys.format(value)), value)

    def testDateAndTimeLocal(self):
        dt = compile_display_hint(DATETIME_HINT, "OCTET STRING", DATETIME_SIZE)
        self.assertEqual(dt.format(DATETIME_8), "1992-5-26,13:30:15.0")
        self.assertEqual(dt.parse("1992-5-26,13:30:15.0"), DATETIME_8)

    def testDateAndTimeWithTimezone(self):
        dt = compile_display_hint(DATETIME_HINT, "OCTET STRING", DATETIME_SIZE)
        rendered = "1992-5-26,13:30:15.0,-4:0"
        self.assertEqual(dt.format(DATETIME_11), rendered)
        self.assertEqual(dt.parse(rendered), DATETIME_11)

    def testScaledTenths(self):
        # HeTenthVolt: -48.1 volts is the integer -481.
        tenths = compile_display_hint("d-1", "Integer32")
        self.assertEqual(tenths.format(-481), "-48.1")
        self.assertEqual(tenths.parse("-48.1"), -481)
        self.assertEqual(tenths.format(0), "0.0")
        self.assertEqual(tenths.format(5), "0.5")

    def testScaledHundredths(self):
        hundredths = compile_display_hint("d-2", "Integer32")
        self.assertEqual(hundredths.format(155056), "1550.56")
        self.assertEqual(hundredths.parse("1550.56"), 155056)
        self.assertEqual(hundredths.format(-1), "-0.01")

    def testScaledZeroPlaces(self):
        plain = compile_display_hint("d-0", "Integer32")
        self.assertEqual(plain.format(42), "42")
        self.assertEqual(plain.parse("42"), 42)

    def testIntegerRadixes(self):
        self.assertEqual(compile_display_hint("x", "INTEGER").format(255), "ff")
        self.assertEqual(compile_display_hint("x", "INTEGER").parse("ff"), 255)
        self.assertEqual(compile_display_hint("o", "INTEGER").format(8), "10")
        self.assertEqual(compile_display_hint("b", "INTEGER").format(5), "101")

    def testDottedDecimal(self):
        ip = compile_display_hint(
            "1d.1d.1d.1d", "OCTET STRING",
            {"size": [{"min": 4, "max": 4}]},
        )
        self.assertEqual(ip.format(bytes([192, 168, 0, 1])), "192.168.0.1")
        self.assertEqual(ip.parse("10.0.0.255"), bytes([10, 0, 0, 255]))

    def testExplicitRepeatWithTerminator(self):
        # SnmpOSIAddress: a count octet, colon-separated octets, a slash,
        # then a trailing colon-separated run.
        osi = compile_display_hint(
            "*1x:/1x:", "OCTET STRING",
            {"size": [{"min": 1, "max": 1}, {"min": 4, "max": 85}]},
        )
        value = bytes([3, 1, 2, 3])
        self.assertEqual(osi.format(value), "01:02:03/")
        self.assertEqual(osi.parse("01:02:03/"), value)
        value = bytes([2, 0xAB, 0xCD, 0x01])
        self.assertEqual(osi.format(value), "ab:cd/01")
        self.assertEqual(osi.parse("ab:cd/01"), value)

    def testHexIsLowercaseAndParsesEitherCase(self):
        mac = compile_display_hint("1x:", "OCTET STRING")
        self.assertEqual(mac.format(bytes([0xAB, 0xCD])), "ab:cd")
        self.assertEqual(mac.parse("AB:cd"), bytes([0xAB, 0xCD]))


class ParseabilityTestCase(unittest.TestCase):
    """Reverse parsing is only available where the text is unambiguous.

    Formatting always works for a grammatical hint; a converter that cannot
    be reversed says so up front and ``parse`` refuses with the same reason.
    """

    def assertUnparseable(self, hint, kind="OCTET STRING"):
        converter = compile_display_hint(hint, kind)
        self.assertFalse(
            converter.parseable,
            msg=f"{hint!r} unexpectedly parseable",
        )
        self.assertTrue(converter.unparseable_reason)
        with self.assertRaises(DisplayHintUnparseableError):
            converter.parse("anything")
        # Formatting is still offered.
        self.assertIsInstance(converter, DisplayHintConverter)
        return converter

    def testRepeatedAsciiHasNoBoundaries(self):
        converter = self.assertUnparseable("255a")
        self.assertEqual(converter.format(b"hello"), "hello")

    def testRepeatedUtf8HasNoWidth(self):
        converter = self.assertUnparseable("65535t")
        self.assertEqual(converter.format("ok".encode()), "ok")

    def testRepeatedNumberWithoutSeparator(self):
        # *2d: how many applications, and where one ends, cannot be known.
        self.assertUnparseable("*2d")
        self.assertUnparseable("*1d")
        self.assertUnparseable("*2x")

    def testRepeatedNumberWithNumericSeparator(self):
        # A separator that is itself a radix digit cannot end a field.
        self.assertUnparseable("*1x1x")

    def testMultiOctetRepeatedDecimalLosesWidth(self):
        # 2d fields 00 07 and 07 both render "7": the byte width is gone.
        self.assertUnparseable("*2d.-")
        # A one-octet repeat has no width to lose and is parseable.
        converter = compile_display_hint("*1d.-", "OCTET STRING")
        self.assertTrue(converter.parseable)

    def testExplicitRepeatWithoutTerminator(self):
        # The count octet cannot be located without a closing terminator.
        self.assertUnparseable("*1x:")

    def testCountedZeroRunCannotBeReversed(self):
        osi = compile_display_hint("*1x:/1x:", "OCTET STRING")
        # A count of zero renders nothing, indistinguishable from a missing
        # value, so parsing the empty text is refused rather than guessed.
        self.assertEqual(osi.format(bytes([0])), "")
        with self.assertRaises(DisplayHintUnparseableError):
            osi.parse("")



    def testFixedWidthRepeatWithoutSeparator(self):
        # x renders two digits per octet, so a lone run segments by width
        # without needing a separator (used by the 16x DOCSIS convention).
        converter = compile_display_hint("16x", "OCTET STRING")
        full = bytes(range(16))
        self.assertEqual(converter.parse(converter.format(full)), full)
        partial = bytes([0xAB, 0xCD])
        self.assertEqual(converter.parse(converter.format(partial)), partial)
        bare = compile_display_hint("1x", "OCTET STRING")
        self.assertEqual(
            bare.parse(bare.format(bytes([1, 0xAB, 255]))),
            bytes([1, 0xAB, 255]),
        )

    def testFixedWidthRepeatRejectsMidOctetTail(self):
        converter = compile_display_hint("1x", "OCTET STRING")
        with self.assertRaises(DisplayHintValueError) as context:
            converter.parse("abc")
        self.assertEqual(context.exception.position, 2)

    def testCountedHexRepeat(self):
        converter = compile_display_hint("*2x:-", "OCTET STRING")
        value = bytes([2, 0xAB, 0xCD, 0x12, 0x34])
        self.assertEqual(converter.format(value), "abcd:1234-")
        self.assertEqual(converter.parse("abcd:1234-"), value)

    def testAsciiRejectsHighBitOctets(self):
        converter = compile_display_hint("255a", "OCTET STRING")
        with self.assertRaises(DisplayHintValueError):
            converter.format(bytes([0x80]))
        fixed = compile_display_hint("1a1d", "OCTET STRING")
        with self.assertRaises(DisplayHintValueError):
            fixed.parse("\u00e95")

    def testUtf8RejectsInvalidBytes(self):
        converter = compile_display_hint("255t", "OCTET STRING")
        with self.assertRaises(DisplayHintValueError):
            converter.format(b"\xff\xfe")



    def testTransportAddressPortWidthFromSize(self):
        # SnmpUDPAddress 1d.1d.1d.1d/2d, SIZE(6): the trailing two-octet port
        # has its width fixed by SIZE, so 46 reverses to octets 00 2e.
        udp = compile_display_hint(
            "1d.1d.1d.1d/2d", "OCTET STRING",
            {"size": [{"min": 6, "max": 6}]},
        )
        value = bytes([52, 206, 211, 18, 0, 46])
        self.assertEqual(udp.format(value), "52.206.211.18/46")
        self.assertEqual(udp.parse("52.206.211.18/46"), value)

    def testTrailingVariableWidthNeedsSizeToParse(self):
        # Without a single pinned SIZE the trailing 2d field's byte width is
        # not recoverable and the hint is declared unparseable.
        udp = compile_display_hint("1d.1d.1d.1d/2d", "OCTET STRING")
        self.assertFalse(udp.parseable)
        self.assertIn("SIZE", udp.unparseable_reason)
        # It still formats.
        self.assertEqual(udp.format(bytes([1, 2, 3, 4, 0, 46])), "1.2.3.4/46")


class GrammarTestCase(unittest.TestCase):
    """Hints outside the RFC 2579 grammar name their offset."""

    def assertBadHint(self, hint, kind="OCTET STRING"):
        with self.assertRaises(DisplayHintSyntaxError) as context:
            compile_display_hint(hint, kind)
        self.assertIsInstance(context.exception.position, int)

    def testMalformedOctetHints(self):
        for hint in ("", "1y", "*", "1", "0x", "1b", "1x2d*", "1x:2x:*"):
            self.assertBadHint(hint)

    def testOnlyOneExplicitRepeat(self):
        self.assertBadHint("*1x:*2x")

    def testBinaryIsIntegerOnly(self):
        self.assertBadHint("1b")

    def testMalformedIntegerHints(self):
        for hint in ("q", "d-", "x-1"):
            self.assertBadHint(hint, "INTEGER")

    def testOctetHintAppliedToInteger(self):
        with self.assertRaises(DisplayHintTypeError):
            # "2d" is a grammatical octet hint but not an integer hint.
            compile_display_hint("2d", "INTEGER")

    def testScalingOnlyWithDecimal(self):
        self.assertBadHint("x-2", "INTEGER")

    def testHintFamilyMustMatchType(self):
        with self.assertRaises(DisplayHintTypeError):
            compile_display_hint("1x:", "INTEGER")
        with self.assertRaises(DisplayHintTypeError):
            compile_display_hint("d", "OCTET STRING")
        with self.assertRaises(DisplayHintTypeError):
            compile_display_hint("d", "SomeUnknownType")


class ConstraintTestCase(unittest.TestCase):
    """Both directions respect the type's range and SIZE."""

    def testIntegerRangeOnFormat(self):
        converter = compile_display_hint(
            "d", "Integer32", {"range": [{"min": 0, "max": 99}]}
        )
        with self.assertRaises(DisplayHintConstraintError):
            converter.format(100)
        with self.assertRaises(DisplayHintConstraintError):
            converter.format(-1)

    def testIntegerRangeOnParse(self):
        converter = compile_display_hint("d", "Unsigned32")
        with self.assertRaises(DisplayHintConstraintError):
            converter.parse("-1")
        converter2 = compile_display_hint(
            "d-2", "Integer32", {"range": [{"min": 0, "max": 9999}]}
        )
        with self.assertRaises(DisplayHintConstraintError):
            converter2.parse("999.99")

    def testDefaultBoundsApplyToSmiTypes(self):
        # Integer32 without a declared range still spans -2^31..2^31-1.
        converter = compile_display_hint("d", "Integer32")
        with self.assertRaises(DisplayHintConstraintError):
            converter.format(2**31)
        # A bare INTEGER has no default range and is unbounded.
        bare = compile_display_hint("d", "INTEGER")
        self.assertEqual(bare.format(2**40), str(2**40))

    def testSizeOnFormatAndParse(self):
        mac = compile_display_hint(
            "1x:", "OCTET STRING", {"size": [{"min": 6, "max": 6}]}
        )
        with self.assertRaises(DisplayHintConstraintError):
            mac.format(b"\x00" * 5)
        with self.assertRaises(DisplayHintConstraintError):
            mac.parse("00:00:0c:12:34:56:77")

    def testSizeAlternation(self):
        dt = compile_display_hint(DATETIME_HINT, "OCTET STRING", DATETIME_SIZE)
        with self.assertRaises(DisplayHintConstraintError):
            dt.format(b"\x00" * 9)
        with self.assertRaises(DisplayHintValueError):
            # A dangling separator renders nothing and marks trailing text.
            dt.parse("1992-5-26,13:30:15.0,")


class ErrorPositionTestCase(unittest.TestCase):
    """Errors on text point at the offending character and segment."""

    def setUp(self):
        self.mac = compile_display_hint(
            "1x:", "OCTET STRING", {"size": [{"min": 6, "max": 6}]}
        )

    def testBadDigitHasPosition(self):
        error = _capture(
            self,
            DisplayHintValueError,
            lambda: self.mac.parse("00:gg:0c:12:34:56"),
        )
        self.assertEqual(error.position, 3)
        self.assertEqual(error.segment, 0)

    def testTrailingCharactersHavePosition(self):
        error = _capture(
            self,
            DisplayHintValueError,
            lambda: self.mac.parse("00:00:0c:12:34:56zz"),
        )
        self.assertEqual(error.position, 17)

    def testMissingSeparatorNamesIt(self):
        ip = compile_display_hint("1d.1d.1d.1d", "OCTET STRING")
        error = _capture(
            self, DisplayHintValueError, lambda: ip.parse("192-168-0-1")
        )
        self.assertEqual(error.position, 3)

    def testRepeatedFieldTooWide(self):
        ip = compile_display_hint("1d.1d.1d.1d", "OCTET STRING")
        self.assertRaises(
            DisplayHintValueError, lambda: ip.parse("256.0.0.1")
        )

    def testScaledIntegerFractionCount(self):
        converter = compile_display_hint("d-2", "Integer32")
        with self.assertRaises(DisplayHintValueError):
            converter.parse("1.2")
        with self.assertRaises(DisplayHintValueError):
            converter.parse("1.234")

    def testPlusSignRejected(self):
        converter = compile_display_hint("d", "Integer32")
        with self.assertRaises(DisplayHintValueError) as context:
            converter.parse("+1")
        self.assertEqual(context.exception.position, 0)

    def testEmptyIntegerText(self):
        converter = compile_display_hint("d", "Integer32")
        with self.assertRaises(DisplayHintValueError) as context:
            converter.parse("")
        self.assertEqual(context.exception.position, 0)

    def testMissingRepeatTerminator(self):
        osi = compile_display_hint("*1x:/1x:", "OCTET STRING")
        with self.assertRaises(DisplayHintValueError):
            osi.parse("01:ab")
        with self.assertRaises(DisplayHintValueError):
            osi.parse("01:ab/xx")


class EdgeValueTestCase(unittest.TestCase):
    """Empty values, short repeat counts and excess tail characters."""

    def testEmptyOctetStringWhenSizeAllows(self):
        converter = compile_display_hint(
            "1x:", "OCTET STRING", {"size": [{"min": 0, "max": 6}]}
        )
        self.assertEqual(converter.format(b""), "")
        self.assertEqual(converter.parse(""), b"")

    def testEmptyOctetStringWhenSizeForbids(self):
        converter = compile_display_hint(
            "1x:", "OCTET STRING", {"size": [{"min": 1, "max": 6}]}
        )
        self.assertRaises(DisplayHintConstraintError,
                          lambda: converter.format(b""))
        self.assertRaises(DisplayHintConstraintError,
                          lambda: converter.parse(""))

    def testRepeatCountMoreThanValue(self):
        # Count octet says three applications, the value supplies none.
        converter = compile_display_hint("*1d.-", "OCTET STRING")
        error = _capture(
            self, DisplayHintValueError, lambda: converter.format(bytes([3]))
        )
        self.assertEqual(error.segment, 0)
        self.assertEqual(error.position, 1)

    def testExcessTrailingCharacters(self):
        ip = compile_display_hint("1d.1d.1d.1d", "OCTET STRING")
        error = _capture(
            self,
            DisplayHintValueError,
            lambda: ip.parse("192.168.0.1garbage"),
        )
        self.assertEqual(error.position, len("192.168.0.1"))

    def testHintWithNoSegmentForOctets(self):
        # All fields fixed and the value carries one octet too many.
        converter = compile_display_hint("1x:1x", "OCTET STRING")
        self.assertRaises(DisplayHintValueError,
                          lambda: converter.format(bytes([1, 2, 3])))

    def testTruncatedDateAcceptedWhenSizeAgrees(self):
        # Formatting itself stops once octets run out; an 8-octet value
        # therefore has no timezone fields and parses back unchanged.
        dt = compile_display_hint(DATETIME_HINT, "OCTET STRING", DATETIME_SIZE)
        self.assertEqual(dt.parse(dt.format(DATETIME_8)), DATETIME_8)

    def testBytesLikeAccepted(self):
        converter = compile_display_hint("1x:", "OCTET STRING")
        self.assertEqual(converter.format(bytearray([1, 2])), "01:02")
        self.assertEqual(converter.format(memoryview(bytes([1, 2]))), "01:02")

    def testPythonValueTypeMismatch(self):
        with self.assertRaises(DisplayHintTypeError):
            compile_display_hint("1x:", "OCTET STRING").format(7)
        with self.assertRaises(DisplayHintTypeError):
            compile_display_hint("d", "INTEGER").format(b"x")


class JsonModelEntryPointTestCase(unittest.TestCase):
    """The converter is built straight from a model type specification."""

    def testFromTypeObject(self):
        converter = compile_json_type(
            {"type": "OCTET STRING",
             "constraints": {"size": [{"min": 6, "max": 6}]}},
            "1x:",
        )
        self.assertEqual(converter.format(bytes(6)), "00:00:00:00:00:00")

    def testFromTypeNameAndInteger(self):
        converter = compile_json_type(
            {"type": "Integer32",
             "constraints": {"range": [{"min": 0, "max": 99}]}},
            "d-1",
        )
        self.assertEqual(converter.format(42), "4.2")
        self.assertEqual(converter.parse("4.2"), 42)

    def testUnresolvedTextualConventionRejected(self):
        # No MIB lookup is pulled in; a TC name must be resolved to its base
        # type by the caller first.
        self.assertRaises(
            DisplayHintTypeError,
            lambda: compile_json_type({"type": "DateAndTime"}, DATETIME_HINT),
        )



class JsonModelIntegrationTestCase(unittest.TestCase):
    """A converter is built from the document a MIB actually compiles to."""

    MIB = """
TEST-MIB DEFINITIONS ::= BEGIN
IMPORTS
    TEXTUAL-CONVENTION
        FROM SNMPv2-TC;
MacAddress ::= TEXTUAL-CONVENTION
    DISPLAY-HINT "1x:"
    STATUS       current
    DESCRIPTION  "x"
    SYNTAX       OCTET STRING (SIZE (6))
END
"""

    def testConverterFromCompiledModel(self):
        model = render_json(self.MIB)["MacAddress"]
        converter = compile_json_type(model["type"], model["displayhint"])
        value = bytes.fromhex("001122334455")
        self.assertEqual(converter.format(value), "00:11:22:33:44:55")
        self.assertEqual(converter.parse("00:11:22:33:44:55"), value)


suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])

if __name__ == "__main__":
    unittest.TextTestRunner(verbosity=2).run(suite)
