#
# This file is part of pysmi software.
#
# Copyright (c) 2015-2020, Ilya Etingof <etingof@gmail.com>
# License: https://github.com/pysnmp/pysmi/blob/main/LICENSE.rst
#
"""Compile a DISPLAY-HINT into a value converter.

The JSON model pysmi generates carries a textual convention's DISPLAY-HINT
verbatim (``displayhint``), leaving every management interface to re-implement
the RFC 2579 section 3.1 grammar for rendering and parsing values. This
module compiles the hint once and hands back a converter, so a JSON document
is all a tool needs:

.. code-block:: python

   from pysmi.displayhint import compile_display_hint

   node = json_document["MacAddress"]
   mac = compile_display_hint(node["displayhint"], node["type"])

   mac.format(bytes.fromhex("001b44113ab7"))   # '00:1b:44:11:3a:b7'
   mac.parse("00:1b:44:11:3a:b7")              # b'\\x00\\x1bD\\x11:\\xb7'

The conversion semantics follow RFC 2579 section 3.1, as also implemented by
pysnmp's ``TextualConvention.prettyOut``: a ``*`` repeat indicator consumes
an octet of the value as the repetition count, a repeat terminator is
appended after every repetition, a separator is emitted between applications
of a specification, and the hint starts over while value octets remain.

Formatting works for every well-formed hint. Parsing is only offered for
hints whose output segments unambiguously -- the converter refuses to guess:
:py:attr:`DisplayHintConverter.reversible` tells the two apart, and
:py:meth:`DisplayHintConverter.parse` raises with the reason when it is not.

Constraints of the underlying type (integer ranges, OCTET STRING sizes) are
honoured in both directions when the JSON type node is supplied, and every
error carries the offending input position or the hint segment it came from.

One deliberate departure from pysnmp's rendering: integer radix hints
(``x``, ``o``, ``b``) format as bare digits without a ``0x``/``0``/``B``
decoration, so that :py:meth:`DisplayHintConverter.parse` can read its own
output back.
"""

import re
from typing import Any, NamedTuple

from pysmi.error import PySmiDisplayHintError

__all__ = ["DisplayHintConverter", "compile_display_hint"]

#: Display formats RFC 2579 section 3.1 defines for an octet-format
#: specification, plus ``b`` (binary), which some modules use for octets too.
OCTET_FORMATS = frozenset("xdoatb")

#: Numeric octet formats whose rendering has a fixed width per octet, so
#: adjacent groups segment without a separator.
_FIXED_WIDTH_FORMATS = frozenset("xob")

#: Octet formats rendering text rather than a number.
_TEXT_FORMATS = frozenset("at")

#: Characters per octet for the fixed-width numeric formats.
_DIGITS_PER_OCTET = {"x": 2, "o": 3, "b": 8}

#: Alphabets a parsed group may draw from, per numeric format.
_GROUP_ALPHABETS = {
    "x": frozenset("0123456789abcdefABCDEF"),
    "d": frozenset("0123456789"),
    "o": frozenset("01234567"),
    "b": frozenset("01"),
}

#: Letters a hexadecimal group may contain -- a separator from this set makes
#: a group indistinguishable from its separator.
_HEX_LETTERS = frozenset("abcdefABCDEF")

#: An integer hint: a display format, with decimal scaling for ``d`` only.
_INTEGER_HINT_RE = re.compile(r"([dxob])(?:-(\d+))?")

#: Number bases for the integer display formats.
_INTEGER_BASES = {"d": 10, "x": 16, "o": 8, "b": 2}

#: Type names whose values are integers for the purpose of a hint.
_INTEGER_TYPES = frozenset(
    {
        "INTEGER",
        "Integer32",
        "Gauge",
        "Gauge32",
        "Unsigned32",
        "Counter",
        "Counter32",
        "Counter64",
        "TimeTicks",
    }
)

#: Type names whose values are octet strings for the purpose of a hint.
_OCTET_STRING_TYPES = frozenset({"OCTET STRING", "Opaque"})

#: The range of a bare INTEGER when no constraints are supplied.
_DEFAULT_INTEGER_RANGE = ((-2147483648, 2147483647),)

#: The SIZE of a bare OCTET STRING when no constraints are supplied.
_DEFAULT_SIZE = ((0, 65535),)


class OctetFormatSpec(NamedTuple):
    """One octet-format specification of a DISPLAY-HINT, RFC 2579 3.1."""

    #: Whether the specification starts with the ``*`` repeat indicator.
    repeat: bool
    #: Octets each application of the specification consumes.
    octets: int
    #: Display format: ``x``, ``d``, ``o``, ``a``, ``t`` or ``b``.
    format: str
    #: Character emitted between applications, if any.
    separator: "str | None"
    #: Character emitted after each repetition, if any.
    terminator: "str | None"


class DisplayHintConverter:
    """A DISPLAY-HINT compiled into a formatter and, where possible, a parser.

    Instances are built by :py:func:`compile_display_hint`; the two
    specialisations are integer hints (``d``, ``x``, ``o``, ``b`` with
    optional decimal scaling) and octet-format specifications.
    """

    #: The hint as written in the module.
    hint: str
    #: ``"integer"`` or ``"octets"``.
    kind: str
    #: The type name the converter was compiled against, if one was supplied.
    type_name: "str | None"
    #: Whether :py:meth:`parse` is meaningful for this hint.
    reversible: bool
    #: Why the hint cannot be parsed back, when it cannot.
    irreversible_reason: "str | None"

    def format(self, value: Any) -> str:
        """Render *value* (an int or an octet string) as the hint dictates."""
        raise NotImplementedError

    def parse(self, text: str) -> Any:
        """Recover a value from hint-formatted *text*."""
        raise NotImplementedError

    def check(self, value: Any) -> Any:
        """Validate *value* against the underlying type's constraints."""
        raise NotImplementedError

    def __repr__(self) -> str:
        """Spell out the hint and the type it was compiled against."""
        return f"{self.__class__.__name__}({self.hint!r}, type={self.type_name!r})"


class _IntegerDisplayHint(DisplayHintConverter):
    """An integer hint: a radix, plus decimal scaling for ``d``."""

    kind = "integer"
    reversible = True
    irreversible_reason = None

    def __init__(
        self,
        hint: str,
        display_format: str,
        scale: int,
        ranges: "tuple[tuple[int, int], ...]",
        type_name: "str | None",
    ) -> None:
        self.hint = hint
        self.display_format = display_format
        self.scale = scale
        self.ranges = ranges
        self.type_name = type_name

    def _ranges_text(self) -> str:
        return " or ".join(f"{low}..{high}" for low, high in self.ranges)

    def check(self, value: Any) -> int:
        """Validate *value* against the type's ranges, returning it."""
        if not isinstance(value, int):
            raise PySmiDisplayHintError(
                f"an integer value is expected, got {type(value).__name__}"
            )
        for low, high in self.ranges:
            if low <= value <= high:
                return value
        raise PySmiDisplayHintError(
            f"value {value} is outside the range {self._ranges_text()} "
            f"of type {self.type_name or 'INTEGER'}"
        )

    def format(self, value: Any) -> str:
        """Render an integer, applying the radix and any decimal scaling."""
        self.check(value)
        if self.display_format == "d":
            if not self.scale:
                return str(value)
            sign = "-" if value < 0 else ""
            digits = str(abs(value)).rjust(self.scale + 1, "0")
            return f"{sign}{digits[:-self.scale]}.{digits[-self.scale:]}"
        return format(value, self.display_format)

    def parse(self, text: str) -> int:
        """Parse hint-formatted text back into an integer."""
        if not isinstance(text, str):
            raise PySmiDisplayHintError(
                f"text to parse is expected to be a string, got "
                f"{type(text).__name__}"
            )
        at, end = 0, len(text)
        negative = False
        if at < end and text[at] in "+-":
            negative = text[at] == "-"
            at += 1

        alphabet = _GROUP_ALPHABETS[self.display_format]
        start = at
        while at < end and text[at] in alphabet:
            at += 1
        whole = text[start:at]
        if not whole:
            raise PySmiDisplayHintError(
                f"position {start}: a {self.display_format!r}-formatted "
                f"number is expected in {text!r}",
                position=start,
            )

        fraction = ""
        if self.scale and at < end and text[at] == ".":
            at += 1
            start = at
            while at < end and text[at].isdigit():
                at += 1
            fraction = text[start:at]
            if not fraction:
                raise PySmiDisplayHintError(
                    f"position {start}: decimal places are expected after "
                    f"the point in {text!r}",
                    position=start,
                )
            if len(fraction) > self.scale:
                raise PySmiDisplayHintError(
                    f"position {start + self.scale}: more decimal places than "
                    f"the scale {self.scale} of hint {self.hint!r} allows",
                    position=start + self.scale,
                )

        if at != end:
            raise PySmiDisplayHintError(
                f"position {at}: unexpected character {text[at]!r} in {text!r}",
                position=at,
            )

        if self.display_format == "d":
            value = int(whole) * 10**self.scale
            if fraction:
                value += int(fraction.ljust(self.scale, "0"))
            if negative:
                value = -value
        else:
            value = int(("-" if negative else "") + whole,
                        _INTEGER_BASES[self.display_format])

        return self.check(value)


class _OctetStringDisplayHint(DisplayHintConverter):
    """A hint made of octet-format specifications."""

    kind = "octets"

    def __init__(
        self,
        hint: str,
        specs: "list[OctetFormatSpec]",
        sizes: "tuple[tuple[int, int], ...]",
        type_name: "str | None",
    ) -> None:
        self.hint = hint
        self.specs = specs
        self.sizes = sizes
        self.type_name = type_name
        self.reversible, self.irreversible_reason = self._analyse()

    def _sizes_text(self) -> str:
        return " or ".join(
            f"{low}" if low == high else f"{low}..{high}" for low, high in self.sizes
        )

    def _analyse(self) -> "tuple[bool, str | None]":
        """Decide whether formatted text segments back unambiguously."""
        for segment, spec in enumerate(self.specs):
            if spec.repeat:
                return False, (
                    f"segment {segment} carries the repeat indicator: the "
                    f"repetition count is an octet of the value and never "
                    f"appears in the formatted text, so the text cannot be "
                    f"segmented back"
                )

        one_pass = sum(spec.octets for spec in self.specs)
        single_pass = all(high <= one_pass for _, high in self.sizes)

        for segment, spec in enumerate(self.specs):
            if spec.separator is not None:
                if spec.format in _TEXT_FORMATS:
                    return False, (
                        f"segment {segment}: format {spec.format!r} renders "
                        f"arbitrary text that may itself contain the "
                        f"separator {spec.separator!r}"
                    )
                if spec.format == "x" and spec.separator in _HEX_LETTERS:
                    return False, (
                        f"segment {segment}: separator {spec.separator!r} is "
                        f"also a hexadecimal digit, so a group cannot be told "
                        f"from its separator"
                    )
            elif spec.format == "d" and (
                segment != len(self.specs) - 1 or not single_pass
            ):
                return False, (
                    f"segment {segment}: a decimal group has no fixed width, "
                    f"and without a separator its end cannot be told from "
                    f"what follows"
                )

        return True, None

    def check(self, value: Any) -> bytes:
        """Coerce *value* to octets and validate it against SIZE."""
        if isinstance(value, str):
            value = value.encode("utf-8")
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise PySmiDisplayHintError(
                f"an octet string value is expected, got {type(value).__name__}"
            )
        octets = bytes(value)
        for low, high in self.sizes:
            if low <= len(octets) <= high:
                return octets
        raise PySmiDisplayHintError(
            f"value of {len(octets)} octets is outside SIZE "
            f"({self._sizes_text()}) of type {self.type_name or 'OCTET STRING'}"
        )

    def format(self, value: Any) -> str:
        """Render an octet string as the hint dictates."""
        data = self.check(value)
        out = []
        offset = 0
        index = 0
        while offset < len(data):
            segment = index % len(self.specs)
            spec = self.specs[segment]
            index += 1

            if spec.repeat:
                count = data[offset]
                offset += 1
            else:
                count = 1

            for repetition in range(count):
                chunk = data[offset : offset + spec.octets]
                if spec.repeat and len(chunk) < spec.octets:
                    raise PySmiDisplayHintError(
                        f"segment {segment}: repeat count {count} asks for "
                        f"{spec.octets} octets in group {repetition + 1}, but "
                        f"the value has only {len(data) - offset} left",
                        segment=segment,
                    )
                if not chunk:
                    break
                out.append(self._render_group(spec, chunk, segment))
                offset += len(chunk)
                if spec.terminator is not None:
                    out.append(spec.terminator)

            # A repeat terminator closes every repetition, so the separator
            # has nothing to separate and is not emitted (as pysnmp does).
            if (
                offset < len(data)
                and spec.separator is not None
                and spec.terminator is None
            ):
                out.append(spec.separator)

        return "".join(out)

    def _render_group(
        self, spec: OctetFormatSpec, chunk: bytes, segment: int
    ) -> str:
        """Render one group of octets."""
        if spec.format == "a":
            return chunk.decode("latin-1")
        if spec.format == "t":
            try:
                return chunk.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PySmiDisplayHintError(
                    f"segment {segment}: value octets are not valid UTF-8: {exc}",
                    segment=segment,
                ) from exc
        number = int.from_bytes(chunk, "big")
        if spec.format == "d":
            return str(number)
        width = _DIGITS_PER_OCTET[spec.format] * len(chunk)
        return f"{number:0{width}{spec.format}}"

    def parse(self, text: str) -> bytes:
        """Parse hint-formatted text back into an octet string."""
        if not self.reversible:
            raise PySmiDisplayHintError(
                f"display hint {self.hint!r} cannot be reversed: "
                f"{self.irreversible_reason}"
            )
        if not isinstance(text, str):
            raise PySmiDisplayHintError(
                f"text to parse is expected to be a string, got "
                f"{type(text).__name__}"
            )
        octets = bytearray()
        at, end = 0, len(text)
        index = 0
        while at < end:
            segment = index % len(self.specs)
            spec = self.specs[segment]
            index += 1
            start = at
            group, at = self._read_group(spec, text, at, segment)
            octets += self._decode_group(spec, group, start, segment)
        return self.check(bytes(octets))

    def _read_group(
        self, spec: OctetFormatSpec, text: str, at: int, segment: int
    ) -> "tuple[str, int]":
        """Read one group's text, returning it and the position past it."""
        end = len(text)

        if spec.format == "d" and spec.separator is not None:
            boundary = text.find(spec.separator, at)
            if boundary == -1:
                return text[at:], end
            if boundary + 1 == end:
                raise PySmiDisplayHintError(
                    f"position {boundary}: separator {spec.separator!r} is "
                    f"not followed by a group in {text!r}",
                    position=boundary,
                    segment=segment,
                )
            return text[at:boundary], boundary + 1

        if spec.format == "d":
            # The last specification of a hint the SIZE confines to a single
            # pass: the group runs to the end of the text.
            return text[at:], end

        if spec.format == "t":
            group, at = self._read_utf8_group(spec, text, at, segment)
        else:
            width = (
                spec.octets
                if spec.format == "a"
                else _DIGITS_PER_OCTET[spec.format] * spec.octets
            )
            group = text[at : at + width]
            if len(group) < width:
                raise PySmiDisplayHintError(
                    f"position {at}: a {width}-character group of segment "
                    f"{segment} is expected, found {group!r}",
                    position=at,
                    segment=segment,
                )
            at += width

        if at < end and spec.separator is not None:
            if text[at] != spec.separator:
                raise PySmiDisplayHintError(
                    f"position {at}: separator {spec.separator!r} is "
                    f"expected, found {text[at]!r}",
                    position=at,
                    segment=segment,
                )
            at += 1
            if at == end:
                raise PySmiDisplayHintError(
                    f"position {at - 1}: separator {spec.separator!r} is "
                    f"not followed by a group in {text!r}",
                    position=at - 1,
                    segment=segment,
                )

        return group, at

    def _read_utf8_group(
        self, spec: OctetFormatSpec, text: str, at: int, segment: int
    ) -> "tuple[str, int]":
        """Read characters until they encode to the group's octet count."""
        encoded = 0
        end = at
        while encoded < spec.octets:
            if end >= len(text):
                raise PySmiDisplayHintError(
                    f"position {end}: text ends before the {spec.octets}-octet "
                    f"UTF-8 group of segment {segment} is complete",
                    position=end,
                    segment=segment,
                )
            char_len = len(text[end].encode("utf-8"))
            if encoded + char_len > spec.octets:
                raise PySmiDisplayHintError(
                    f"position {end}: character {text[end]!r} does not fit "
                    f"the {spec.octets}-octet group of segment {segment}",
                    position=end,
                    segment=segment,
                )
            encoded += char_len
            end += 1
        return text[at:end], end

    def _decode_group(
        self, spec: OctetFormatSpec, group: str, position: int, segment: int
    ) -> bytes:
        """Turn one group's text back into its octets."""
        if spec.format == "a":
            try:
                return group.encode("latin-1")
            except UnicodeEncodeError as exc:
                raise PySmiDisplayHintError(
                    f"position {position}: {exc}",
                    position=position,
                    segment=segment,
                ) from exc
        if spec.format == "t":
            return group.encode("utf-8")

        if not group:
            raise PySmiDisplayHintError(
                f"position {position}: an empty group in segment {segment}",
                position=position,
                segment=segment,
            )
        alphabet = _GROUP_ALPHABETS[spec.format]
        for offset, char in enumerate(group):
            if char not in alphabet:
                raise PySmiDisplayHintError(
                    f"position {position + offset}: character {char!r} is "
                    f"not valid in a {spec.format!r}-formatted group",
                    position=position + offset,
                    segment=segment,
                )
        value = int(group, _INTEGER_BASES[spec.format])
        if value >= 256**spec.octets:
            raise PySmiDisplayHintError(
                f"position {position}: value {value} does not fit in "
                f"{spec.octets} octet(s)",
                position=position,
                segment=segment,
            )
        return value.to_bytes(spec.octets, "big")


def _parse_octet_specs(hint: str) -> "list[OctetFormatSpec]":
    """Break a hint into its octet-format specifications."""
    specs = []
    at, end = 0, len(hint)
    while at < end:
        segment = len(specs)

        repeat = hint[at] == "*"
        if repeat:
            at += 1

        digits = at
        while at < end and hint[at].isdigit():
            at += 1
        if at == digits:
            raise PySmiDisplayHintError(
                f"position {at}: an octet length is expected in display "
                f"hint {hint!r}",
                position=at,
                segment=segment,
            )
        octets = int(hint[digits:at])
        if not octets:
            raise PySmiDisplayHintError(
                f"position {digits}: an octet length of zero is meaningless "
                f"in display hint {hint!r}",
                position=digits,
                segment=segment,
            )

        if at >= end or hint[at] not in OCTET_FORMATS:
            raise PySmiDisplayHintError(
                f"position {at}: a display format (one of "
                f"{''.join(sorted(OCTET_FORMATS))}) is expected in display "
                f"hint {hint!r}",
                position=at,
                segment=segment,
            )
        display_format = hint[at]
        at += 1

        separator = terminator = None
        if at < end and not hint[at].isdigit() and hint[at] != "*":
            separator = hint[at]
            at += 1
        if at < end and not hint[at].isdigit() and hint[at] != "*":
            if not repeat:
                raise PySmiDisplayHintError(
                    f"position {at}: a repeat terminator requires the repeat "
                    f"indicator in display hint {hint!r}",
                    position=at,
                    segment=segment,
                )
            terminator = hint[at]
            at += 1

        specs.append(OctetFormatSpec(repeat, octets, display_format, separator,
                                     terminator))

    return specs


def _syntax_parts(syntax: Any) -> "tuple[str | None, dict]":
    """Extract the type name and constraints from a JSON model node."""
    if syntax is None:
        return None, {}
    if isinstance(syntax, str):
        return syntax, {}
    if isinstance(syntax, dict):
        node = syntax
        # A textual-convention node wraps its type node.
        if isinstance(node.get("type"), dict):
            node = node["type"]
        type_name = node.get("type")
        constraints = node.get("constraints") or {}
        return (type_name if isinstance(type_name, str) else None), constraints
    raise PySmiDisplayHintError(
        f"syntax is expected to be a JSON type node or a type name, got "
        f"{type(syntax).__name__}"
    )


def _bounds(constraints: dict, key: str, default: "tuple[tuple[int, int], ...]"):
    """Read range or size bounds out of a JSON constraints node."""
    bounds = constraints.get(key)
    if not bounds:
        return default
    return tuple((int(bound["min"]), int(bound["max"])) for bound in bounds)


def _check_type_match(type_name: "str | None", kind: str, hint: str) -> None:
    """Keep a hint from being compiled against the wrong kind of syntax."""
    if type_name is None:
        return
    if kind == "integer" and type_name in _OCTET_STRING_TYPES:
        raise PySmiDisplayHintError(
            f"display hint {hint!r} is an integer hint, but {type_name} "
            f"values are octet strings"
        )
    if kind == "octets" and type_name in _INTEGER_TYPES:
        raise PySmiDisplayHintError(
            f"display hint {hint!r} consists of octet-format specifications, "
            f"but {type_name} values are integers"
        )


def compile_display_hint(
    display_hint: str, syntax: Any = None
) -> DisplayHintConverter:
    """Compile a DISPLAY-HINT into a converter for the JSON model.

    Args:
        display_hint: the DISPLAY-HINT text, as carried by the JSON model's
            ``displayhint`` key
        syntax: the JSON model's ``type`` node (or the whole textual
            convention node, or a bare type name) supplying the underlying
            type and its constraints; when omitted, SMI defaults apply
            (``-2147483648..2147483647`` or ``SIZE (0..65535)``)

    Returns:
        A converter with ``format`` and ``check``, plus ``parse`` for hints
        that segment unambiguously (see ``reversible``).

    Raises:
        PySmiDisplayHintError: the hint does not follow the RFC 2579
            section 3.1 grammar, or it does not match the syntax kind
    """
    if not isinstance(display_hint, str) or not display_hint:
        raise PySmiDisplayHintError(
            f"a non-empty display hint string is expected, got "
            f"{display_hint!r}",
            position=0,
        )

    type_name, constraints = _syntax_parts(syntax)

    integer = _INTEGER_HINT_RE.fullmatch(display_hint)
    if integer:
        display_format, scale = integer.groups()
        if scale is not None and display_format != "d":
            raise PySmiDisplayHintError(
                f"decimal scaling in display hint {display_hint!r} only "
                f"applies to the 'd' format",
                position=1,
            )
        if scale is not None and not int(scale):
            raise PySmiDisplayHintError(
                f"the scale in display hint {display_hint!r} must be a "
                f"positive integer",
                position=2,
            )
        _check_type_match(type_name, "integer", display_hint)
        return _IntegerDisplayHint(
            display_hint,
            display_format,
            int(scale or 0),
            _bounds(constraints, "range", _DEFAULT_INTEGER_RANGE),
            type_name,
        )

    if not display_hint[0].isdigit() and display_hint[0] != "*":
        raise PySmiDisplayHintError(
            f"position 0: display hint {display_hint!r} is neither an "
            f"integer hint (a format letter with optional decimal scaling) "
            f"nor an octet-format specification (which starts with an octet "
            f"length or '*')",
            position=0,
        )

    specs = _parse_octet_specs(display_hint)
    _check_type_match(type_name, "octets", display_hint)
    return _OctetStringDisplayHint(
        display_hint,
        specs,
        _bounds(constraints, "size", _DEFAULT_SIZE),
        type_name,
    )
