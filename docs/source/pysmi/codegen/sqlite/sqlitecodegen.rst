.. _codegen.sqlite.SqliteCodeGen:

SQLite storage mapping generator
--------------------------------

.. autoclass:: pysmi.codegen.sqlite.SqliteCodeGen
   :members:

.. autofunction:: pysmi.codegen.sqlite.bind_parameters

.. autofunction:: pysmi.codegen.sqlite.decode_row

The generated mapping carries the reversible representation of every field:

* ``integer32`` and ``unsigned32`` use SQLite ``INTEGER``.
* ``counter64`` uses canonical decimal ``TEXT`` to preserve the unsigned 64-bit maximum.
* ``octet_string``, ``ip_address``, ``opaque`` and ``bits`` use ``BLOB``; no bytes are stripped, padded, or decoded as text.
* ``oid`` uses one canonical dot-decimal ``TEXT`` column per INDEX component. Components are never concatenated into a shared blob, so variable-length and IMPLIED boundaries remain explicit.

The backend emits mapping data only; it does not collect values or execute SQL.
