#
# This file is part of pysmi software.
#
# Copyright (c) 2015-2020, Ilya Etingof <etingof@gmail.com>
# License: https://github.com/pysnmp/pysmi/blob/main/LICENSE.rst
#
"""Compile an SMIv2 ``DISPLAY-HINT`` into value converters.

A compiled MIB only carries the hint text -- the ``displayhint`` field of the
JSON model -- which says how a value is to be rendered but renders nothing.
This module closes that gap for the JSON consumer without pulling in another
MIB parser: the hint string is the only input, and the result is a
:class:`DisplayHintConverter` offering

* :meth:`~DisplayHintConverter.format` -- native value to display string, and
* :meth:`~DisplayHintConverter.parse` -- display string back to native value.

Two hint families are defined by :rfc:`2579` section 3.1.

Integer hints
    A single radix, optionally signed and/or decimal-scaled: ``d``, ``x``,
    ``o``, ``b``, ``-d`` and ``d-2``.
Octet string hints
    One or more octet-format specifications, each carrying an octet length
    segment, a display format (``x`` hex, ``d`` decimal, ``o`` octal,
    ``a`` ASCII, ``t`` UTF-8), a separator, and for a specification an
    optional repeat indicator and repeat terminator: ``1x:``,
    ``2d-1d-1d,1d:1d:1d.1d,1a1d:1d`` and ``*1x:/1x:``.

Formatting is offered whenever the hint is grammatical. Reverse parsing is
offered only when the text can be segmented unambiguously: a hint such as
``255a`` (a repeated ASCII segment), ``*2d`` (a repeated number with no
separator) or ``1d.1d.1d.1d/2d`` (a trailing multi-octet number with no
single SIZE length fixing its width) compiles and formats, but
:meth:`~DisplayHintConverter.parse` raises
:class:`DisplayHintUnparseableError` naming the reason. The parser never
guesses a segmentation.

Both directions respect the original type range or SIZE restriction, and
formatting a value the hint itself cannot consume -- more octets than it has
segments, or a numeric segment wider than its fixed length -- is rejected as
well. Errors on text carry the offending offset (``position``) and, where
relevant, the hint segment index (``segment``).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import ClassVar, Union

from pysmi.error import PySmiError

__all__ = [
    "DisplayHintConverter",
    "DisplayHintError",
    "DisplayHintSyntaxError",
    "DisplayHintTypeError",
    "DisplayHintUnparseableError",
    "DisplayHintValueError",
    "DisplayHintConstraintError",
    "IntegerHint",
    "OctetSpec",
    "compile_display_hint",
    "compile_json_type",
]

#: The five display formats an octet-format specification may use.
OCTET_FORMATS = frozenset("xdoat")


class DisplayHintError(PySmiError):
    """Base class for every error raised by this module.

    The message always says what was wrong. Errors that concern a position
    in a hint or in input text also carry ``position`` (a zero-based offset),
    and those that concern one segment of an octet hint carry ``segment``
    (zero-based, in hint order). A handler should treat either attribute as
    optional.
    """

    #: Zero-based offset into the hint or input text, or ``None``.
    position = None
    #: Zero-based index of the offending octet specification, or ``None``.
    segment = None

    def __init__(self, msg: str, *, position: int | None = None,
                 segment: int | None = None) -> None:
        super().__init__(msg)
        # Always present, so a caller can read either without a hasattr
        # guard; None means the error does not locate that coordinate.
        self.position = position
        self.segment = segment


class DisplayHintSyntaxError(DisplayHintError):
    """The hint text is not a valid :rfc:`2579` section 3.1 hint.

    ``position`` points at the first character the grammar rejects.
    """


class DisplayHintTypeError(DisplayHintError):
    """The hint and the base type it is applied to do not match.

    Integer hints belong on integer types and octet hints on OCTET STRING;
    this is raised, for example, when ``1x:`` is compiled for an INTEGER.
    """


class DisplayHintUnparseableError(DisplayHintError):
    """The hint renders values but its text cannot be reversed unambiguously.

    Raised by :meth:`DisplayHintConverter.parse` (and available up front via
    :attr:`DisplayHintConverter.unparseable_reason`): the hint is a valid
    formatter, but some segment -- a repeated ``a``/``t`` run, or a repeated
    number with no delimiter outside the radix alphabet -- has no unique
    segmentation, so a value cannot be recovered without guessing.
    """


class DisplayHintValueError(DisplayHintError):
    """A value or input text does not fit the hint.

    Raised for a native value of the wrong Python type, a formatted segment
    that does not decode (a non-hex pair under ``x``, say), input left over
    once every segment is consumed, or a hint that runs out of octets before
    all its segments are formatted. ``position`` locates the offending text
    when the error concerns input.
    """


class DisplayHintConstraintError(DisplayHintError):
    """A value is outside the type's range or SIZE restriction.

    Raised in both directions: formatting a native value outside the
    permitted interval, or parsing text that decodes to one. The bounds the
    value was checked against are recorded on the error.
    """

    #: The constraints the value failed, in the JSON model's shape.
    constraints: dict

    def __init__(self, msg: str, *, constraints: dict,
                 position: int | None = None, segment: int | None = None) -> None:
        super().__init__(msg, position=position, segment=segment)
        self.constraints = constraints


@dataclass(frozen=True)
class IntegerHint:
    """A compiled integer display hint.

    Attributes:
        signed: Whether the hint begins with ``-``, marking the field signed.
        radix: The display radix: 10 (``d``), 16 (``x``), 8 (``o``) or
            2 (``b``).
        decimals: Decimal places after the point (the number following
            ``-`` in ``d-2``), or ``None`` for a plain radix rendering. Only
            ``d`` hints carry one.
    """

    signed: bool
    radix: int
    decimals: int | None

    #: Digits each radix renders with; used to reject ambiguous separators.
    _ALPHABETS: ClassVar = {
        10: frozenset("0123456789"),
        16: frozenset("0123456789abcdefABCDEF"),
        8: frozenset("01234567"),
        2: frozenset("01"),
    }

    @property
    def alphabet(self) -> frozenset[str]:
        """The characters a token of this radix may consist of."""
        return self._ALPHABETS[self.radix]


@dataclass(frozen=True)
class OctetSpec:
    """One octet-format specification within an octet string hint.

    Attributes:
        length: Octets consumed per application (1..65535).
        format: The single display-format character (``x`` hex, ``d``
            decimal, ``o`` octal, ``a`` ASCII, ``t`` UTF-8).
        separator: The display separator emitted after every application,
            possibly empty.
        repeat: Whether this specification repeats over the octets the
            formatter reaches.
        terminator: The repeat terminator, or ``None``; present only on a
            specification with an explicit repeat indicator.
        counted: Whether the repeat carries an explicit ``*`` count octet,
            as opposed to a single-specification hint implicitly repeating
            (``1x:`` rendering a six-octet MAC address).
    """

    length: int
    format: str
    separator: str
    repeat: bool = False
    terminator: str | None = None
    counted: bool = False


@dataclass(frozen=True)
class DisplayHintConverter:
    """Formats native values under a hint and parses the text back.

    Instances are produced by :func:`compile_display_hint` and
    :func:`compile_json_type`; they are immutable and safe to share. The
    value types are plain Python :class:`int` and :class:`bytes` -- whatever
    reads the JSON model already holds those.

    Attributes:
        hint: The hint text as compiled.
        kind: ``"integer"`` or ``"octet"``.
        constraints: The ``range``/``size`` restrictions honoured in both
            directions, in the JSON model's shape, or ``None`` when the type
            is unrestricted.
    """

    hint: str
    kind: str
    constraints: dict | None
    _plan: Union[IntegerHint, tuple[OctetSpec, ...]] = field(repr=False)
    unparseable_reason: str | None = field(default=None, compare=False)

    @property
    def parseable(self) -> bool:
        """Whether :meth:`parse` can reverse text under this hint.

        A false value is permanent for the hint: the missing information is
        in the hint, not in the input. The reason is
        :attr:`unparseable_reason`.
        """
        return self.unparseable_reason is None

    def format(self, value: int | bytes | bytearray | memoryview) -> str:
        """Render a native value as the hint prescribes.

        Args:
            value: an :class:`int` for an integer hint, or bytes-like for an
                octet hint.

        Returns:
            The display string.

        Raises:
            DisplayHintTypeError: the Python value is of the wrong kind.
            DisplayHintConstraintError: it violates the type's range/SIZE, or
                a fixed numeric segment cannot hold it, or the hint has no
                segment left for some of its octets.
            DisplayHintValueError: a ``t`` segment is not valid UTF-8.
        """
        if self.kind == "integer":
            return _format_integer(self._plan, value, self.constraints, self.hint)
        return _format_octets(self._plan, value, self.constraints, self.hint)

    def parse(self, text: str) -> int | bytes:
        """Recover a native value from display text.

        Args:
            text: the string :meth:`format` produces, accepted
                case-insensitively where the radix allows (so ``A`` and ``a``
                are both valid hex digits).

        Returns:
            An :class:`int` for an integer hint, :class:`bytes` for an octet
            hint.

        Raises:
            DisplayHintUnparseableError: the hint cannot be reversed at all.
            DisplayHintValueError: a segment does not decode, the text ends
                before a required segment, or characters remain after the
                last one. ``position`` locates the offending character.
            DisplayHintConstraintError: the decoded value violates the
                type's range/SIZE.
        """
        if not self.parseable:
            raise DisplayHintUnparseableError(
                f"DISPLAY-HINT {self.hint!r} cannot be reversed: "
                f"{self.unparseable_reason}"
            )
        if self.kind == "integer":
            return _parse_integer(self._plan, text, self.constraints, self.hint)
        return _parse_octets(self._plan, text, self.constraints, self.hint)


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------

def _normalise_constraints(constraints: dict | None) -> dict | None:
    """Return the range/size entries a converter must honour.

    The JSON model names numeric bounds ``range`` and octet-length bounds
    ``size``; anything else (enumerations, named bits) is irrelevant to
    rendering and dropped.
    """
    if not constraints:
        return None
    kept = {}
    if constraints.get("range"):
        kept["range"] = [
            {"min": int(r["min"]), "max": int(r["max"])} for r in constraints["range"]
        ]
    if constraints.get("size"):
        kept["size"] = [
            {"min": int(r["min"]), "max": int(r["max"])} for r in constraints["size"]
        ]
    return kept or None


def _within(value: int, bounds: Sequence[dict]) -> bool:
    """Whether *value* falls in one of the ``min``/``max`` intervals."""
    return any(bound["min"] <= value <= bound["max"] for bound in bounds)


def _pinned_total_length(constraints: dict | None) -> int | None:
    """Return the one SIZE value the type permits, or ``None``.

    When SIZE pins the octet string to a single length, a trailing
    variable-width number (``d``/``o``) can be given a unique byte width by
    subtracting the octets earlier fields consumed. A range or an
    alternation leaves that width a choice, in which case the trailing
    number is unparseable and the runtime error says so.
    """
    if constraints is None or "size" not in constraints:
        return None
    points = {
        bound["min"]
        for bound in constraints["size"]
        if bound["min"] == bound["max"]
    }
    if len(constraints["size"]) == 1 and len(points) == 1:
        return next(iter(points))
    return None


def _check_integer_range(
    value: int, constraints: dict | None, hint: str, position: int | None = None
) -> None:
    """Reject an integer outside every permitted range."""
    if constraints is not None and "range" in constraints:
        ranges = constraints["range"]
        if not _within(value, ranges):
            spans = " | ".join(f"{r['min']}..{r['max']}" for r in ranges)
            raise DisplayHintConstraintError(
                f"value {value} is outside the permitted range(s) {spans} "
                f"for DISPLAY-HINT {hint!r}",
                constraints={"range": ranges},
                position=position,
            )


def _check_octet_size(
    length: int,
    constraints: dict | None,
    hint: str,
    position: int | None = None,
    segment: int | None = None,
) -> None:
    """Reject an octet string whose length is outside every SIZE interval."""
    if constraints is not None and "size" in constraints:
        sizes = constraints["size"]
        if not _within(length, sizes):
            spans = " | ".join(f"{r['min']}..{r['max']}" for r in sizes)
            raise DisplayHintConstraintError(
                f"octet string of length {length} violates SIZE ({spans}) "
                f"for DISPLAY-HINT {hint!r}",
                constraints={"size": sizes},
                position=position,
                segment=segment,
            )


# ---------------------------------------------------------------------------
# Integer hints
# ---------------------------------------------------------------------------

#: Radix letters and the bases they denote.
_RADIXES = {"d": 10, "x": 16, "o": 8, "b": 2}


def _format_integer(
    plan: IntegerHint,
    value: object,
    constraints: dict | None,
    hint: str,
) -> str:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DisplayHintTypeError(
            f"DISPLAY-HINT {hint!r} formats an integer, got {type(value).__name__}"
        )
    _check_integer_range(value, constraints, hint)

    if plan.decimals is not None:
        # RFC 2579: left-pad the absolute decimal value so at least one digit
        # precedes the point, then split off that many digits as the fraction.
        digits = str(abs(value)).rjust(plan.decimals + 1, "0")
        whole = digits[:-plan.decimals] if plan.decimals else digits
        if plan.decimals:
            text = f"{whole}.{digits[-plan.decimals:]}"
        else:
            text = whole
    else:
        if plan.radix == 10:
            text = str(abs(value))
        elif plan.radix == 16:
            text = format(abs(value), "x")
        elif plan.radix == 8:
            text = format(abs(value), "o")
        else:
            text = format(abs(value), "b")

    if value < 0:
        text = "-" + text
    return text


def _parse_integer(
    plan: IntegerHint,
    text: object,
    constraints: dict | None,
    hint: str,
) -> int:
    if not isinstance(text, str):
        raise DisplayHintValueError(
            f"DISPLAY-HINT {hint!r} parses a string, got {type(text).__name__}"
        )
    if not text:
        raise DisplayHintValueError(
            f"empty text cannot be parsed under DISPLAY-HINT {hint!r}",
            position=0,
        )

    at = 0
    negative = False
    if text[0] == "-":
        negative = True
        at = 1
        if len(text) == 1:
            raise DisplayHintValueError(
                f"lone '-' is not a number under DISPLAY-HINT {hint!r}",
                position=at,
            )
    elif text[0] == "+":
        raise DisplayHintValueError(
            f"a '+' is never produced by DISPLAY-HINT {hint!r}",
            position=0,
        )

    if plan.decimals is not None:
        whole, dot, fraction = text[at:].partition(".")
        if plan.decimals and not dot:
            raise DisplayHintValueError(
                f"expected '.' with {plan.decimals} fractional digit(s) under "
                f"DISPLAY-HINT {hint!r}",
                position=len(text),
            )
        if not dot:
            whole, fraction = text[at:], ""
        if not whole.isdigit() or (dot and not fraction.isdigit()):
            bad = at + (0 if not whole.isdigit() else len(whole))
            raise DisplayHintValueError(
                f"non-decimal digit in scaled integer under DISPLAY-HINT "
                f"{hint!r}",
                position=bad,
            )
        if len(fraction) != plan.decimals:
            raise DisplayHintValueError(
                f"DISPLAY-HINT {hint!r} shows exactly {plan.decimals} "
                f"fractional digit(s), got {len(fraction)}",
                position=at + len(whole) + 1,
            )
        magnitude = int(whole + fraction)
        at = len(text)
    else:
        token = text[at:]
        if not token or any(ch not in plan.alphabet for ch in token):
            bad = next(
                (
                    i
                    for i, ch in enumerate(token)
                    if ch not in plan.alphabet
                ),
                len(token),
            )
            names = {10: "decimal", 16: "hexadecimal", 8: "octal", 2: "binary"}
            raise DisplayHintValueError(
                f"invalid {names[plan.radix]} digit under DISPLAY-HINT "
                f"{hint!r}",
                position=at + bad,
            )
        magnitude = int(token, plan.radix)
        at = len(text)

    if negative:
        magnitude = -magnitude
    _check_integer_range(magnitude, constraints, hint)
    return magnitude


# ---------------------------------------------------------------------------
# Hint grammar (RFC 2579 section 3.1)
# ---------------------------------------------------------------------------

_INTEGER_HINT = re.compile(r"^(-?)([dxob])(?:-(\d+))?$")


def _parse_integer_hint(hint: str) -> IntegerHint:
    match = _INTEGER_HINT.match(hint)
    if match is None:
        return None
    signed, letter, decimals = match.groups()
    if decimals is not None and letter != "d":
        raise DisplayHintSyntaxError(
            f"decimal scaling ('-{decimals}') is only valid with 'd', not "
            f"{letter!r} in DISPLAY-HINT {hint!r}",
            position=hint.index("-", 1),
        )
    return IntegerHint(
        signed=bool(signed),
        radix=_RADIXES[letter],
        decimals=int(decimals) if decimals is not None else None,
    )


def _parse_octet_hint(hint: str) -> tuple[OctetSpec, ...]:
    """Walk an octet hint specification by specification.

    Grammar, per :rfc:`2579` section 3.1: an optional ``*``, one or more
    decimal digits, one display-format character, then up to two trailing
    characters which are not digits or ``*`` (separator and repeat
    terminator).
    """
    specs: list[OctetSpec] = []
    at, end = 0, len(hint)

    while at < end:
        start = at
        repeat = False
        if hint[at] == "*":
            repeat = True
            at += 1

        digits_start = at
        while at < end and hint[at].isdigit():
            at += 1
        if at == digits_start:
            raise DisplayHintSyntaxError(
                f"expected an octet length (decimal digits) in DISPLAY-HINT "
                f"{hint!r}",
                position=at,
            )
        if at == end or hint[at] not in OCTET_FORMATS:
            raise DisplayHintSyntaxError(
                "expected one of the display formats "
                f"'{''.join(sorted(OCTET_FORMATS))}' in DISPLAY-HINT {hint!r}",
                position=min(at, end - 1),
            )
        length = int(hint[digits_start:at])
        if length == 0:
            raise DisplayHintSyntaxError(
                f"octet length must be at least 1 in DISPLAY-HINT {hint!r}",
                position=digits_start,
            )
        format_letter = hint[at]
        at += 1

        trailing = []
        while at < end and not hint[at].isdigit() and hint[at] != "*":
            trailing.append(hint[at])
            at += 1
        separator = trailing[0] if trailing else ""
        terminator = trailing[1] if len(trailing) > 1 else None
        if len(trailing) > 2:
            raise DisplayHintSyntaxError(
                "an octet specification carries at most a separator and a "
                f"repeat terminator in DISPLAY-HINT {hint!r}",
                position=start + 2,
            )
        # A terminator is only meaningful with a repeat indicator, and the
        # hint may hold just one repeat.
        if not repeat and terminator is not None:
            raise DisplayHintSyntaxError(
                "a repeat terminator only follows a repeating specification "
                f"('*') in DISPLAY-HINT {hint!r}",
                position=start + 2,
            )
        if repeat and any(prior.counted for prior in specs):
            raise DisplayHintSyntaxError(
                "only one explicit repeat specification ('*') is allowed in "
                f"DISPLAY-HINT {hint!r}",
                position=start,
            )
        specs.append(
            OctetSpec(
                length=length,
                format=format_letter,
                separator=separator,
                repeat=repeat,
                terminator=terminator,
                counted=repeat,
            )
        )

    if not specs:
        raise DisplayHintSyntaxError(
            f"DISPLAY-HINT {hint!r} is empty", position=0
        )
    return tuple(specs)


#: Base types the JSON model renders with integer hints.
_INTEGER_TYPES = frozenset(
    (
        "INTEGER",
        "Integer32",
        "INTEGER32",
        "Counter32",
        "Counter64",
        "Gauge32",
        "Unsigned32",
        "TimeTicks",
    )
)


def _execution_specs(
    specs: tuple[OctetSpec, ...],
) -> tuple[OctetSpec, ...]:
    """Return the specifications the formatter and parser actually walk.

    Two repeat behaviours appear in real hints.

    * An explicit repeat (``*``) consumes a leading count octet and repeats
      that many times; fixed specifications may follow it once its
      terminator is emitted -- ``*1x:/1x:`` renders a count, a run of
      colon-separated octets, ``/`` and a trailing colon-separated run.
    * A hint of a single fixed specification such as ``1x:`` names no
      repeat, yet values routinely carry more than one application's worth
      of octets: ``PhysAddress`` and ``MacAddress`` render six octets under
      ``1x:``. The lone specification then repeats implicitly until the
      value is exhausted, with no count and no terminator.

    The same implicit rule applies to the final specification following an
    explicit repeat.
    """
    planned: list[OctetSpec] = []
    explicit = next((i for i, spec in enumerate(specs) if spec.counted), None)
    for index, spec in enumerate(specs):
        if not spec.counted and (
            len(specs) == 1
            or (explicit is not None and index == len(specs) - 1 and index > explicit)
        ):
            planned.append(
                OctetSpec(
                    length=spec.length,
                    format=spec.format,
                    separator=spec.separator,
                    repeat=True,
                    terminator=spec.terminator,
                    counted=False,
                )
            )
        else:
            planned.append(spec)
    return tuple(planned)


#: Default bounds when a type carries no range constraint of its own.
_DEFAULT_RANGES = {
    "Integer32": (-2147483648, 2147483647),
    "INTEGER32": (-2147483648, 2147483647),
    "Counter32": (0, 4294967295),
    "Counter64": (0, 18446744073709551615),
    "Gauge32": (0, 4294967295),
    "Unsigned32": (0, 4294967295),
    "TimeTicks": (0, 4294967295),
}


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------

def _repeat_digit_reason(spec: OctetSpec, index: int) -> str | None:
    """The RFC separator test for a repeated numeric specification.

    Two distinct ambiguities make a repeat unparseable:

    * a missing separator, or one starting inside the radix alphabet, gives
      no field boundary (the rule :rfc:`2579` states);
    * a multi-octet ``d``/``o`` field renders without width, so even with a
      boundary the byte length is lost -- ``2d`` fields ``00 07`` and ``07``
      both render ``7``. ``x``/``b`` render a fixed width per octet and keep
      their length.
    """
    if spec.format in ("d", "o"):
        radix = 10 if spec.format == "d" else 8
        if spec.length != 1:
            return (
                f"segment {index + 1} repeats a {spec.length}-octet "
                f"'{spec.format}' number, which renders without its width, "
                "so the octet length of each field cannot be recovered"
            )
        # A variable-width number needs a separator that ends each field;
        # one starting inside the radix alphabet cannot mark the boundary.
        if not spec.separator or (
            spec.separator[0] in IntegerHint._ALPHABETS[radix]
        ):
            return (
                f"segment {index + 1} repeats '{spec.format}' with separator "
                f"{spec.separator!r} that cannot end a field, so where one "
                "number ends cannot be told from where the next begins"
            )
        return None
    if spec.format in ("x", "b"):
        # Each octet renders a fixed-width token (2 hex or 8 binary digits),
        # so fields segment by width with or without a separator.
        return None
    return None


def _octet_unparseable_reason(
    specs: tuple[OctetSpec, ...],
    hint: str,
    constraints: dict | None = None,
) -> str | None:
    """Say why *specs* cannot be reversed, or ``None`` when they can.

    Per :rfc:`2579` section 3.1, parsing is not defined for ``a``/``t``
    fields, a repeated numeric field needs a separator whose first character
    is outside the radix alphabet, and an explicit (``*``) repeat also needs
    a terminator to mark the run and recover the count octet. A variable
    width fixed field (``d``/``o``) that is followed by another field needs
    a separator to end on.
    """
    for index, spec in enumerate(specs):
        is_last = index == len(specs) - 1

        if spec.format == "t":
            return (
                f"segment {index + 1} renders UTF-8 ('t'), whose text has no "
                "fixed-width reverse mapping"
            )

        if spec.counted:
            if spec.format == "a":
                return (
                    f"segment {index + 1} repeats ASCII ('a'); its text has "
                    "no field boundaries, so the run cannot be segmented"
                )
            if spec.format != "t":
                reason = _repeat_digit_reason(spec, index)
                if reason:
                    return reason
            if spec.terminator is None:
                return (
                    f"segment {index + 1} carries a repeat indicator ('*') "
                    "but no terminator, so the rendered run cannot be "
                    "located to recover its count octet"
                )
            continue

        if spec.repeat:
            if spec.format == "a":
                return (
                    f"segment {index + 1} repeats ASCII ('a') with no field "
                    "boundaries, so run length cannot be recovered"
                )
            if spec.format == "t":
                return (
                    f"segment {index + 1} repeats UTF-8 ('t'), which has no "
                    "fixed-width reverse mapping"
                )
            reason = _repeat_digit_reason(spec, index)
            if reason:
                return reason
            continue

        if spec.format in ("d", "o"):
            radix = 10 if spec.format == "d" else 8
            if not is_last:
                if not spec.separator:
                    return (
                        f"segment {index + 1} is a variable-width number "
                        f"('{spec.format}') with no separator ending it "
                        f"before segment {index + 2}"
                    )
                if spec.separator[0] in IntegerHint._ALPHABETS[radix]:
                    return (
                        f"segment {index + 1} separator {spec.separator!r} "
                        "starts inside the radix alphabet, so it cannot end "
                        f"the number before segment {index + 2}"
                    )
            elif spec.length != 1 and _pinned_total_length(constraints) is None:
                return (
                    f"segment {index + 1} is a trailing variable-width "
                    f"{spec.length}-octet '{spec.format}' number whose byte "
                    "width is not fixed by a following field or a single "
                    "SIZE length, so it cannot be reversed unambiguously"
                )
    return None


def compile_display_hint(
    hint: str,
    kind: str,
    constraints: dict | None = None,
) -> DisplayHintConverter:
    """Compile a ``DISPLAY-HINT`` string into a converter.

    Args:
        hint: The hint exactly as it appears in the JSON model's
            ``displayhint`` field.
        kind: The base SMI type the hint applies to -- ``"integer"`` (also
            ``"INTEGER"`` and the other numeric SMI type names) or
            ``"octet"`` / ``"OCTET STRING"``.
        constraints: Optional type restrictions in the JSON model shape:
            ``{"range": [{"min": ..., "max": ...}]}`` for integers or
            ``{"size": [...]}`` for octet strings. Both are honoured in
            formatting and parsing.

    Returns:
        A ready :class:`DisplayHintConverter`.

    Raises:
        DisplayHintSyntaxError: the hint does not follow :rfc:`2579`
            section 3.1.
        DisplayHintTypeError: *kind* is not recognised or does not match the
            hint family.
    """
    normalised_constraints = _normalise_constraints(constraints)

    if kind in _INTEGER_TYPES or kind.lower() == "integer":
        normalised_kind = "integer"
        if kind in _DEFAULT_RANGES and "range" not in (normalised_constraints or {}):
            low, high = _DEFAULT_RANGES[kind]
            normalised_constraints = {
                **(normalised_constraints or {}),
                "range": [{"min": low, "max": high}],
            }
    elif kind in ("OCTET STRING", "octet", "OCTETSTRING"):
        normalised_kind = "octet"
    else:
        raise DisplayHintTypeError(
            f"DISPLAY-HINT applies to INTEGER or OCTET STRING, not {kind!r}"
        )

    integer_plan = _parse_integer_hint(hint)
    if integer_plan is not None:
        if normalised_kind != "integer":
            raise DisplayHintTypeError(
                f"DISPLAY-HINT {hint!r} is an integer hint but the type is "
                f"{kind!r}"
            )
        return DisplayHintConverter(
            hint=hint,
            kind="integer",
            constraints=normalised_constraints,
            _plan=integer_plan,
        )

    specs = _parse_octet_hint(hint)
    if normalised_kind != "octet":
        raise DisplayHintTypeError(
            f"DISPLAY-HINT {hint!r} is an OCTET STRING hint but the type is "
            f"{kind!r}"
        )
    plan = _execution_specs(specs)
    reason = _octet_unparseable_reason(
        plan, hint, normalised_constraints
    )
    return DisplayHintConverter(
        hint=hint,
        kind="octet",
        constraints=normalised_constraints,
        _plan=plan,
        unparseable_reason=reason,
    )


def compile_json_type(
    type_spec: dict,
    displayhint: str,
    *,
    constraints: dict | None = None,
) -> DisplayHintConverter:
    """Compile the hint carried by one JSON model type specification.

    This is the convenience entry point for a consumer walking a compiled
    document: hand it the model's ``type`` object and its ``displayhint``
    string and get a converter back. Only the base SMI types carry hints, so
    a textual-convention name in ``type`` is an error here -- resolve it to
    its base type (the model has no MIB table of its own, and this module
    deliberately does not add one) and call :func:`compile_display_hint`.

    Args:
        type_spec: The JSON ``type`` object, or at least its ``"type"`` name;
            a bare string name is accepted too.
        displayhint: The model's ``displayhint`` value.
        constraints: Optional explicit restrictions; defaults to the
            ``constraints`` object in *type_spec*.

    Returns:
        A compiled :class:`DisplayHintConverter`.

    Raises:
        DisplayHintTypeError: the type name is not a base type that hints
            apply to, so it needs resolving first.
        DisplayHintSyntaxError: the hint is malformed.
    """
    if isinstance(type_spec, str):
        name = type_spec
        embedded = None
    else:
        name = type_spec.get("type")
        embedded = type_spec.get("constraints")

    chosen = constraints or embedded
    if name in ("OCTET STRING", "octet", "OCTETSTRING"):
        return compile_display_hint(displayhint, "OCTET STRING", chosen)
    if name in _INTEGER_TYPES or (name and name.lower() == "integer"):
        return compile_display_hint(displayhint, name, chosen)
    raise DisplayHintTypeError(
        f"{name!r} is not a base type with a DISPLAY-HINT; resolve the "
        "textual convention to INTEGER or OCTET STRING first"
    )


# ---------------------------------------------------------------------------
# Octet string formatting
# ---------------------------------------------------------------------------

def _octet_chunk_to_int(chunk: bytes) -> int:
    """Read a chunk of octets as one big-endian unsigned integer."""
    value = 0
    for octet in chunk:
        value = (value << 8) | octet
    return value


def _render_chunk(spec: OctetSpec, chunk: bytes) -> str:
    """Render one application of an octet-format specification.

    A *chunk* shorter than ``spec.length`` is the last partial application
    :rfc:`2579` allows: ``x``/``o``/``b`` render per octet, ``d`` treats the
    octets present as one number.
    """
    if spec.format == "x":
        return "".join(f"{octet:02x}" for octet in chunk)
    if spec.format == "o":
        return "".join(f"{octet:03o}" for octet in chunk)
    if spec.format == "b":
        return "".join(f"{octet:08b}" for octet in chunk)
    if spec.format == "d":
        return str(_octet_chunk_to_int(chunk))
    if spec.format == "a":
        # RFC 2579 'a' is ASCII: each octet renders as one character and the
        # textual conventions constrain the bytes it covers to below 128.
        return chunk.decode("ascii")
    # 't': the chunk is one UTF-8 unit. A repeat consumes up to length octets,
    # so UTF-8 may span the final boundary; decoding per application is the
    # behaviour RFC 2579 describes.
    return chunk.decode("utf-8")


def _format_octets(
    specs: tuple[OctetSpec, ...],
    value: object,
    constraints: dict | None,
    hint: str,
) -> str:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise DisplayHintTypeError(
            f"DISPLAY-HINT {hint!r} formats an OCTET STRING, got "
            f"{type(value).__name__}"
        )
    octets = bytes(value)
    _check_octet_size(len(octets), constraints, hint)

    out: list[str] = []
    offset = 0
    for index, spec in enumerate(specs):
        if offset >= len(octets):
            break

        if spec.counted:
            # The count is itself an octet of the value; it tells how many
            # applications follow. No count of zero means no terminator.
            count = octets[offset]
            offset += 1
            rendered = 0
            for application in range(count):
                if offset >= len(octets):
                    raise DisplayHintValueError(
                        f"segment {index + 1} repeats {count} time(s) but the "
                        f"value ends after {rendered} at octet position "
                        f"{offset}",
                        position=offset,
                        segment=index,
                    )
                chunk = octets[offset : offset + spec.length]
                if len(chunk) < spec.length:
                    raise DisplayHintValueError(
                        f"segment {index + 1} repeats {count} time(s) but "
                        f"the value ends in the middle of application "
                        f"{rendered + 1} at octet position {offset}",
                        position=offset + len(chunk),
                        segment=index,
                    )
                offset += len(chunk)
                if rendered:
                    out.append(spec.separator)
                try:
                    out.append(_render_chunk(spec, chunk))
                except UnicodeDecodeError as exc:
                    encoding = "UTF-8" if spec.format == "t" else "ASCII"
                    raise DisplayHintValueError(
                        f"segment {index + 1} is not valid {encoding} under "
                        f"DISPLAY-HINT {hint!r}: {exc.reason}",
                        segment=index,
                    ) from exc
                rendered += 1
            if rendered and spec.terminator is not None:
                out.append(spec.terminator)
            continue

        if spec.repeat:
            rendered = 0
            while offset < len(octets):
                chunk = octets[offset : offset + spec.length]
                offset += len(chunk)
                if rendered:
                    out.append(spec.separator)
                try:
                    out.append(_render_chunk(spec, chunk))
                except UnicodeDecodeError as exc:
                    encoding = "UTF-8" if spec.format == "t" else "ASCII"
                    raise DisplayHintValueError(
                        f"segment {index + 1} is not valid {encoding} under "
                        f"DISPLAY-HINT {hint!r}: {exc.reason}",
                        segment=index,
                    ) from exc
                rendered += 1
            if spec.terminator is not None and rendered:
                out.append(spec.terminator)
            continue

        chunk = octets[offset : offset + spec.length]
        offset += len(chunk)
        try:
            out.append(_render_chunk(spec, chunk))
        except UnicodeDecodeError as exc:
            encoding = "UTF-8" if spec.format == "t" else "ASCII"
            raise DisplayHintValueError(
                f"segment {index + 1} is not valid {encoding} under "
                f"DISPLAY-HINT {hint!r}: {exc.reason}",
                segment=index,
            ) from exc
        if offset < len(octets):
            out.append(spec.separator)

    if offset < len(octets):
        raise DisplayHintValueError(
            f"DISPLAY-HINT {hint!r} has no segment for octet at position "
            f"{offset}",
            position=offset,
        )
    return "".join(out)


# ---------------------------------------------------------------------------
# Octet string parsing
# ---------------------------------------------------------------------------

#: Per-octet text width and digit alphabet for the fixed-width radix formats.
_WIDTHS = {"x": 2, "b": 8, "o": 3}
_BASES = {"x": 16, "b": 2, "o": 8}


def _int_to_octets(value: int, length: int) -> bytes:
    """Encode a non-negative integer into *length* big-endian octets."""
    return value.to_bytes(length, byteorder="big")


def _value_error(
    hint: str, segment: int, position: int, message: str
) -> DisplayHintValueError:
    return DisplayHintValueError(message, position=position, segment=segment)


def _expect_separator(
    spec: OctetSpec,
    index: int,
    text: str,
    at: int,
    hint: str,
) -> int:
    """Consume this specification's own separator, if more text remains.

    The formatter emits it exactly when another field follows. The parser
    mirrors that: no separator is required at the end of the text (the value
    is truncated here), and any other character is a segment boundary error.
    """
    if at >= len(text):
        return at
    sep = spec.separator
    if not sep:
        # Two fields with nothing between them: the next fixed-width field
        # can still start, but a variable-width predecessor would already
        # have been refused at compile time. Leave the character for it.
        return at
    if not text.startswith(sep, at):
        raise _value_error(
            hint, index, at,
            f"expected separator {sep!r} after segment {index + 1} at "
            f"position {at} but found {text[at]!r}",
        )
    return at + len(sep)


def _decode_fixed_field(
    spec: OctetSpec,
    index: int,
    field: str,
    field_at: int,
    hint: str,
    pad_to_length: bool = True,
    width_override: int | None = None,
) -> bytes:
    """Decode one application of a fixed (non-repeating) specification.

    *pad_to_length* zero-pads a variable-width number to the specification's
    full width. It is true for a field that another field follows (the hint
    fixes the width) and false for the last field actually rendered, whose
    width is whatever octets the value genuinely ends with.
    """
    if spec.format in _WIDTHS:
        width = _WIDTHS[spec.format]
        base = _BASES[spec.format]
        alphabet = IntegerHint._ALPHABETS[base]
        needed = width * spec.length
        if len(field) > needed:
            # A valid token followed by more characters: the first of those
            # is either an out-of-alphabet digit or genuine trailing text.
            bad = next(
                (
                    i
                    for i in range(needed, len(field))
                    if field[i] not in alphabet
                ),
                needed,
            )
            raise _value_error(
                hint, index, field_at + bad,
                f"segment {index + 1} ('{spec.format}') takes {needed} "
                f"digit(s) for {spec.length} octet(s); unexpected character "
                f"at position {field_at + bad}",
            )
        if len(field) < needed:
            if pad_to_length or len(field) % width:
                raise _value_error(
                    hint, index, field_at + len(field),
                    f"segment {index + 1} ('{spec.format}') needs "
                    f"{needed} digit(s) for {spec.length} octet(s), got "
                    f"{len(field)}",
                )
        octets = []
        for offset in range(0, len(field), width):
            token = field[offset : offset + width]
            bad = next(
                (i for i, ch in enumerate(token) if ch not in alphabet), None
            )
            if bad is not None:
                raise _value_error(
                    hint, index, field_at + offset + bad,
                    f"invalid '{spec.format}' digit at position "
                    f"{field_at + offset + bad}",
                )
            octets.append(int(token, base))
        return bytes(octets)

    if spec.format == "a":
        if len(field) != spec.length:
            raise _value_error(
                hint, index, field_at + len(field),
                f"segment {index + 1} ('a') needs {spec.length} "
                f"character(s), got {len(field)}",
            )
        try:
            return field.encode("ascii")
        except UnicodeEncodeError as exc:
            raise _value_error(
                hint, index, field_at + exc.start,
                f"segment {index + 1} ('a') holds a non-ASCII character at "
                f"position {field_at + exc.start}",
            ) from exc

    base = 10 if spec.format == "d" else 8
    alphabet = IntegerHint._ALPHABETS[base]
    if not field or any(ch not in alphabet for ch in field):
        bad = next(
            (i for i, ch in enumerate(field) if ch not in alphabet),
            len(field),
        )
        raise _value_error(
            hint, index, field_at + bad,
            f"invalid or empty number at segment {index + 1}",
        )
    value = int(field, base)
    needed = max(1, (value.bit_length() + 7) // 8)
    if needed > spec.length:
        raise _value_error(
            hint, index, field_at,
            f"segment {index + 1} value {value} needs {needed} octet(s) but "
            f"the hint allows {spec.length}",
        )
    if width_override is not None:
        if needed > width_override:
            raise _value_error(
                hint, index, field_at,
                f"segment {index + 1} value {value} needs {needed} "
                f"octet(s) but {width_override} remain",
            )
        return _int_to_octets(value, width_override)
    if pad_to_length:
        # Another field follows, so the hint fixes this field's width: the
        # year 7 under 2d is octets 00 07.
        return _int_to_octets(value, spec.length)
    # The value ends on this field; its width is only bounded by spec.length
    # and leading zero octets were not rendered, so keep the natural width.
    return _int_to_octets(value, needed)


def _field_end(
    spec: OctetSpec,
    index: int,
    text: str,
    at: int,
    hint: str,
) -> int:
    """Locate where this fixed field's token ends within *text*.

    A variable-width number (``d``/``o``) runs to its separator when one is
    expected, otherwise to the end of the text; fixed-width fields take an
    exact span and ``a`` fields an exact character count.
    """
    if spec.format in _WIDTHS:
        return min(at + _WIDTHS[spec.format] * spec.length, len(text))
    if spec.format == "a":
        return min(at + spec.length, len(text))
    base = 10 if spec.format == "d" else 8
    alphabet = IntegerHint._ALPHABETS[base]
    if spec.separator:
        end = text.find(spec.separator, at)
        if end < 0:
            # No separator: the number either runs to the end (a truncated
            # last field the formatter can also produce) or a different
            # boundary character appears, which is a separator mismatch.
            for pos in range(at, len(text)):
                if text[pos] not in alphabet:
                    return -pos - 1
            return len(text)
        return end
    return len(text)


def _parse_octets(
    specs: tuple[OctetSpec, ...],
    text: object,
    constraints: dict | None,
    hint: str,
) -> bytes:
    if not isinstance(text, str):
        raise DisplayHintValueError(
            f"DISPLAY-HINT {hint!r} parses a string, got "
            f"{type(text).__name__}"
        )

    pinned = _pinned_total_length(constraints)
    parts: list[bytes] = []
    at = 0
    for index, spec in enumerate(specs):
        if spec.counted:
            chunk, at = _parse_counted(spec, index, text, at, hint)
            parts.append(chunk)
            continue

        if spec.repeat:
            chunk, at = _parse_repeat(spec, index, text, at, hint)
            parts.append(chunk)
            continue

        # Formatting stops at the first specification that cannot be filled,
        # so parsing accepts text ending *before* a later specification
        # starts: an 8-octet DateAndTime carries no timezone fields. It does
        # not accept a separator with nothing behind it, which the formatter
        # never emits either.
        if at >= len(text):
            break

        end = _field_end(spec, index, text, at, hint)
        if end < 0:
            bad = -end - 1
            raise _value_error(
                hint, index, bad,
                f"expected separator {spec.separator!r} after segment "
                f"{index + 1} at position {bad} but found {text[bad]!r}",
            )
        field = text[at:end]
        consumed = sum(len(part) for part in parts)
        follows = end < len(text)
        target_width = None
        if not follows and spec.format in ("d", "o") and pinned is not None:
            # The value ends on this variable-width field and SIZE pins the
            # total length: the field is exactly the remaining octets.
            target_width = pinned - consumed
            if not 1 <= target_width <= spec.length:
                raise _value_error(
                    hint, index, at,
                    f"segment {index + 1} cannot occupy {target_width} "
                    f"octet(s) of a {pinned}-octet value under "
                    f"DISPLAY-HINT {hint!r}",
                )
        parts.append(
            _decode_fixed_field(
                spec, index, field, at, hint,
                pad_to_length=follows or target_width is not None,
                width_override=target_width,
            )
        )
        at = end
        if at < len(text):
            # The formatter only emits this separator when another field is
            # actually rendered, so it must not be the final character.
            if (
                spec.separator
                and text.startswith(spec.separator, at)
                and at + len(spec.separator) == len(text)
            ):
                raise _value_error(
                    hint, index, at,
                    f"separator {spec.separator!r} at position {at} has no "
                    "field following it",
                )
            at = _expect_separator(spec, index, text, at, hint)

    if at != len(text):
        raise DisplayHintValueError(
            f"{len(text) - at} trailing character(s) at position {at} under "
            f"DISPLAY-HINT {hint!r}",
            position=at,
        )

    octets = b"".join(parts)
    _check_octet_size(len(octets), constraints, hint)
    return octets


def _parse_counted(
    spec: OctetSpec,
    index: int,
    text: str,
    at: int,
    hint: str,
) -> tuple[bytes, int]:
    """Consume an explicit ``*`` repeat, recovering its count octet.

    The count is not rendered, so it is reconstructed from the number of
    fields that appear: fields run to the terminator (which a counted
    repeat must carry), separated by *spec.separator*.
    """
    if spec.terminator is None:
        # Ruled out at compile time; guard keeps the helper honest.
        raise DisplayHintUnparseableError(
            f"segment {index + 1} repeats with '*' but names no terminator"
        )
    if at == len(text):
        # A run of zero fields renders no terminator, so the count octet
        # (which is itself zero) cannot be told apart from a missing value.
        raise DisplayHintUnparseableError(
            f"DISPLAY-HINT {hint!r} cannot reverse an empty run at segment "
            f"{index + 1}: the zero count and a truncated value render "
            "identically"
        )
    end = text.find(spec.terminator, at)
    if end < 0:
        raise _value_error(
            hint, index, len(text),
            f"repeating segment {index + 1} is missing its terminator "
            f"{spec.terminator!r}",
        )
    body = text[at:end]
    new_at = end + len(spec.terminator)
    if body:
        fields = body.split(spec.separator) if spec.separator else [body]
    else:
        fields = []
    count = len(fields)
    if count > 255:
        raise _value_error(
            hint, index, at,
            f"segment {index + 1} count {count} does not fit one octet",
        )
    chunks: list[bytes] = [bytes([count])]
    field_at = at
    for field_number, field in enumerate(fields):
        last_field = field_number == len(fields) - 1
        chunk = _decode_fixed_field(
            spec, index, field, field_at, hint, pad_to_length=not last_field
        )
        if len(chunk) > spec.length:
            raise _value_error(
                hint, index, field_at,
                f"repeated field at position {field_at} is wider than "
                f"{spec.length} octet(s)",
            )
        chunks.append(chunk)
        field_at += len(field) + len(spec.separator)
    return b"".join(chunks), new_at


def _parse_repeat(
    spec: OctetSpec,
    index: int,
    text: str,
    at: int,
    hint: str,
) -> tuple[bytes, int]:
    """Consume an implicitly repeating final specification.

    Such a repeat has no count octet and no terminator; it simply consumes
    the remainder of the text. Variable-width numbers are split on their
    separator, while fixed-width radix tokens (``x``/``b``) segment by their
    per-octet width with or without a separator.
    """
    body = text[at:]
    new_at = len(text)
    if not body:
        return b"", new_at

    chunks: list[bytes] = []
    if spec.format in _WIDTHS and not spec.separator:
        width = _WIDTHS[spec.format]
        base = _BASES[spec.format]
        alphabet = IntegerHint._ALPHABETS[base]
        span = width * spec.length
        offset = 0
        while offset < len(body):
            token = body[offset : offset + span]
            if len(token) % width:
                bad = at + offset + len(token) - (len(token) % width)
                raise _value_error(
                    hint, index, bad,
                    f"repeating segment {index + 1} ('{spec.format}') ends "
                    f"mid-octet at position {bad}",
                )
            # A final field may be a partial application; x/o/b render per
            # octet, so consume as many whole-octet tokens as the text holds.
            for inner in range(0, len(token), width):
                digit = token[inner : inner + width]
                if any(ch not in alphabet for ch in digit):
                    bad = next(
                        i for i, ch in enumerate(digit) if ch not in alphabet
                    )
                    raise _value_error(
                        hint, index, at + offset + inner + bad,
                        f"invalid '{spec.format}' digit at position "
                        f"{at + offset + inner + bad}",
                    )
                chunks.append(bytes([int(digit, base)]))
            offset += len(token)
        return b"".join(chunks), new_at

    if not spec.separator:
        # Repeated 'a'/'t' with no separator is refused at compile time.
        raise DisplayHintUnparseableError(
            f"repeating segment {index + 1} has no separator to split on"
        )

    fields = body.split(spec.separator)
    field_at = at
    for field in fields:
        # Every repeated application except possibly the last is full width,
        # and the formatter pads fixed-width fields per octet; only the final
        # application can be partial, so decode without forcing the width.
        chunk = _decode_fixed_field(
            spec, index, field, field_at, hint, pad_to_length=False
        )
        if len(chunk) > spec.length:
            raise _value_error(
                hint, index, field_at,
                f"repeated field at position {field_at} is wider than "
                f"{spec.length} octet(s)",
            )
        chunks.append(chunk)
        field_at += len(field) + len(spec.separator)
    return b"".join(chunks), new_at


def _parse_octets(
    specs: tuple[OctetSpec, ...],
    text: object,
    constraints: dict | None,
    hint: str,
) -> bytes:
    if not isinstance(text, str):
        raise DisplayHintValueError(
            f"DISPLAY-HINT {hint!r} parses a string, got "
            f"{type(text).__name__}"
        )

    pinned = _pinned_total_length(constraints)
    parts: list[bytes] = []
    at = 0
    for index, spec in enumerate(specs):
        if spec.counted:
            chunk, at = _parse_counted(spec, index, text, at, hint)
            parts.append(chunk)
            continue

        if spec.repeat:
            chunk, at = _parse_repeat(spec, index, text, at, hint)
            parts.append(chunk)
            continue

        # Formatting stops at the first specification that cannot be filled,
        # so parsing accepts text ending *before* a later specification
        # starts: an 8-octet DateAndTime carries no timezone fields. It does
        # not accept a separator with nothing behind it, which the formatter
        # never emits either.
        if at >= len(text):
            break

        end = _field_end(spec, index, text, at, hint)
        if end < 0:
            bad = -end - 1
            raise _value_error(
                hint, index, bad,
                f"expected separator {spec.separator!r} after segment "
                f"{index + 1} at position {bad} but found {text[bad]!r}",
            )
        field = text[at:end]
        consumed = sum(len(part) for part in parts)
        follows = end < len(text)
        target_width = None
        if not follows and spec.format in ("d", "o") and pinned is not None:
            # The value ends on this variable-width field and SIZE pins the
            # total length: the field is exactly the remaining octets.
            target_width = pinned - consumed
            if not 1 <= target_width <= spec.length:
                raise _value_error(
                    hint, index, at,
                    f"segment {index + 1} cannot occupy {target_width} "
                    f"octet(s) of a {pinned}-octet value under "
                    f"DISPLAY-HINT {hint!r}",
                )
        parts.append(
            _decode_fixed_field(
                spec, index, field, at, hint,
                pad_to_length=follows or target_width is not None,
                width_override=target_width,
            )
        )
        at = end
        if at < len(text):
            # The formatter only emits this separator when another field is
            # actually rendered, so it must not be the final character.
            if (
                spec.separator
                and text.startswith(spec.separator, at)
                and at + len(spec.separator) == len(text)
            ):
                raise _value_error(
                    hint, index, at,
                    f"separator {spec.separator!r} at position {at} has no "
                    "field following it",
                )
            at = _expect_separator(spec, index, text, at, hint)

    if at != len(text):
        raise DisplayHintValueError(
            f"{len(text) - at} trailing character(s) at position {at} under "
            f"DISPLAY-HINT {hint!r}",
            position=at,
        )

    octets = b"".join(parts)
    _check_octet_size(len(octets), constraints, hint)
    return octets


def _parse_counted(
    spec: OctetSpec,
    index: int,
    text: str,
    at: int,
    hint: str,
) -> tuple[bytes, int]:
    """Consume an explicit ``*`` repeat, recovering its count octet.

    The count is not rendered, so it is reconstructed from the number of
    fields that appear: fields run to the terminator (which a counted
    repeat must carry), separated by *spec.separator*.
    """
    if spec.terminator is None:
        # Ruled out at compile time; guard keeps the helper honest.
        raise DisplayHintUnparseableError(
            f"segment {index + 1} repeats with '*' but names no terminator"
        )
    if at == len(text):
        # A run of zero fields renders no terminator, so the count octet
        # (which is itself zero) cannot be told apart from a missing value.
        raise DisplayHintUnparseableError(
            f"DISPLAY-HINT {hint!r} cannot reverse an empty run at segment "
            f"{index + 1}: the zero count and a truncated value render "
            "identically"
        )
    end = text.find(spec.terminator, at)
    if end < 0:
        raise _value_error(
            hint, index, len(text),
            f"repeating segment {index + 1} is missing its terminator "
            f"{spec.terminator!r}",
        )
    body = text[at:end]
    new_at = end + len(spec.terminator)
    if body:
        fields = body.split(spec.separator) if spec.separator else [body]
    else:
        fields = []
    count = len(fields)
    if count > 255:
        raise _value_error(
            hint, index, at,
            f"segment {index + 1} count {count} does not fit one octet",
        )
    chunks: list[bytes] = [bytes([count])]
    field_at = at
    for field_number, field in enumerate(fields):
        last_field = field_number == len(fields) - 1
        chunk = _decode_fixed_field(
            spec, index, field, field_at, hint, pad_to_length=not last_field
        )
        if len(chunk) > spec.length:
            raise _value_error(
                hint, index, field_at,
                f"repeated field at position {field_at} is wider than "
                f"{spec.length} octet(s)",
            )
        chunks.append(chunk)
        field_at += len(field) + len(spec.separator)
    return b"".join(chunks), new_at


