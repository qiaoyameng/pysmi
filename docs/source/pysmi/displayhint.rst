.. _displayhint:

DISPLAY-HINT converters
=======================

A compiled MIB carries a textual convention's ``DISPLAY-HINT`` verbatim --
the ``displayhint`` field of the :doc:`JSON model </jsondoc-schema>` -- but
says nothing about how to apply it. :py:mod:`pysmi.displayhint` compiles that
string into a small bidirectional converter for whatever consumes the model:

.. code-block:: python

   from pysmi.displayhint import compile_display_hint

   mac = compile_display_hint(
       "1x:", "OCTET STRING", {"size": [{"min": 6, "max": 6}]}
   )

   mac.format(bytes.fromhex("00000c123456"))  # '00:00:0c:12:34:56'
   mac.parse("00:00:0c:12:34:56")             # b'\x00\x00\x0c\x124V'

   tenths = compile_display_hint("d-1", "Integer32")
   tenths.format(-481)   # '-48.1'
   tenths.parse("-48.1")  # -481

The converter is built from the hint string alone -- no MIB table, no second
parser. ``kind`` names the base SMI type the hint belongs to (``INTEGER`` and
its numeric peers, or ``OCTET STRING``), and ``constraints`` is the
``range``/``size`` object the JSON model attaches to the type. Both directions
honour those bounds, and :py:func:`compile_json_type` takes the model's
``type`` object directly.

What is covered
---------------

Integer hints
   Radix (``d`` ``x`` ``o`` ``b``), the optional leading sign, and decimal
   scaling (``d-1``, ``d-2`` ...).

Octet string hints
   Octet length segments, every display format (``x`` ``d`` ``o`` ``a``
   ``t``), display separators, repeat terminators, and the repeating
   specification (``*``): ``1x:``, ``1d.1d.1d.1d``,
   ``2d-1d-1d,1d:1d:1d.1d,1a1d:1d`` and ``*1x:/1x:``. A hint of one fixed
   specification repeats implicitly over a longer value, which is how
   ``PhysAddress`` and ``MacAddress`` render six octets under ``1x:``.

When reverse parsing is refused
-------------------------------

Formatting is offered whenever the hint is grammatical. Parsing is offered
only when the text has an unambiguous segmentation. A hint that can render
but cannot be reversed compiles with :attr:`~pysmi.displayhint.DisplayHintConverter.parseable`
false and a human-readable :attr:`~pysmi.displayhint.DisplayHintConverter.unparseable_reason`;
calling ``parse`` raises
:class:`~pysmi.displayhint.DisplayHintUnparseableError`. Parsing never
guesses a value. It is refused when:

* a repeated segment renders ASCII (``a``) or UTF-8 (``t``), neither of
  which carries field boundaries;
* a repeated number has no separator, or a separator whose first character
  is itself a digit of that radix (``*2d``, ``*1x1x``);
* a multi-octet repeated decimal/octal field renders without its width, so
  the byte length of each field is lost;
* an explicit repeat (``*``) names no terminator to close the run and let
  the count octet be recovered (``*1x:``).

A fixed (non-repeating) field in the middle of a hint needs a separator to
end on as well.

Errors
------

Every error derives from :class:`~pysmi.displayhint.DisplayHintError` (and
therefore :class:`~pysmi.error.PySmiError`). Errors on input text carry a
zero-based ``position`` and, when one applies, a zero-based ``segment``
naming the offending hint specification:

* :class:`~pysmi.displayhint.DisplayHintSyntaxError` -- malformed hint;
* :class:`~pysmi.displayhint.DisplayHintTypeError` -- hint/type mismatch;
* :class:`~pysmi.displayhint.DisplayHintConstraintError` -- range or SIZE
  violation in either direction;
* :class:`~pysmi.displayhint.DisplayHintValueError` -- a value or input
  text that does not fit the hint.

.. automodule:: pysmi.displayhint
   :members:
   :member-order: bysource
