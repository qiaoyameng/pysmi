
.. _displayhint:

DISPLAY-HINT converters
=======================

A textual convention's DISPLAY-HINT arrives in the JSON model verbatim, as
the ``displayhint`` key. Rendering a value with it -- or reading a rendered
value back -- means implementing the RFC 2579 section 3.1 grammar, which
every management interface would otherwise do on its own.

:py:mod:`pysmi.displayhint` compiles the hint once, against the type node of
the same JSON document, and hands back a converter:

.. code-block:: python

   from pysmi.displayhint import compile_display_hint

   node = json_document["MacAddress"]
   mac = compile_display_hint(node["displayhint"], node["type"])

   mac.format(bytes.fromhex("001b44113ab7"))   # '00:1b:44:11:3a:b7'
   mac.parse("00:1b:44:11:3a:b7")              # b'\\x00\\x1bD\\x11:\\xb7'

Integer hints cover the radix (``d``, ``x``, ``o``, ``b``) and decimal
scaling (``d-2`` renders ``12345`` as ``123.45``). Octet-string hints cover
the octet length, the display format, the separator, the repeat terminator
and the ``*`` repeat indicator, whose count is an octet of the value itself.

Formatting works for every well-formed hint. Parsing is only offered for
hints whose output segments unambiguously: a repeated specification cannot
be reversed, because the repetition count never appears in the text, and a
bare decimal group cannot be told from its neighbour without a separator.
:py:attr:`~pysmi.displayhint.DisplayHintConverter.reversible` says which
kind a hint is, and ``parse`` raises with the reason rather than guessing.

Integer ranges and OCTET STRING sizes from the JSON type node are honoured
in both directions, and every error carries the offending input position or
the hint segment it came from.

.. automodule:: pysmi.displayhint

.. autofunction:: pysmi.displayhint.compile_display_hint

.. autoclass:: pysmi.displayhint.DisplayHintConverter
  :members:

.. autoclass:: pysmi.displayhint.OctetFormatSpec
  :members:
