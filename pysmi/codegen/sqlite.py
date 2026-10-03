#
# This file is part of pysmi software.
#
# Copyright (c) 2015-2019, Ilya Etingof <etingof@gmail.com>
# License: https://github.com/pysnmp/pysmi/blob/main/LICENSE.rst
#
"""Generate reversible SQLite storage mappings for conceptual MIB tables."""

from collections import OrderedDict
from collections.abc import Mapping, Sequence
from typing import Any, Final

from pysmi import error
from pysmi import jsonio
from pysmi._aliases import deprecated_camel_case
from pysmi.codegen.base import ValueRanges
from pysmi.codegen.jsondoc import JsonCodeGen
from pysmi.mibinfo import MibInfo

SCHEMA_VERSION: Final = 1

_INTEGER_TYPES: frozenset[str] = frozenset({"INTEGER", "INTEGER32", "INTEGER_32"})
_UNSIGNED_32_TYPES: frozenset[str] = frozenset(
    {
        "COUNTER32",
        "GAUGE32",
        "UNSIGNED32",
        "TIMETICKS",
        "TIME_TICKS",
    }
)
_OCTET_TYPES: frozenset[str] = frozenset({"OCTETSTRING", "OCTET_STRING"})
_OID_TYPES: frozenset[str] = frozenset(
    {"OBJECTIDENTIFIER", "OBJECT_IDENTIFIER", "OID"}
)
_MAX_UINT32: Final = 4_294_967_295
_MAX_UINT64: Final = 18_446_744_073_709_551_615
_MIN_INT32: Final = -2_147_483_648
_MAX_INT32: Final = 2_147_483_647
_MAX_OID_ARCS: Final = 128
_MAX_OID_SUBID: Final = _MAX_UINT32 - 1

_ENCODING_RULES: Final[dict[str, str]] = OrderedDict(
    [
        (
            "integer32",
            "Python int in [-2147483648, 2147483647] is bound to SQLite INTEGER; "
            "decode returns int.",
        ),
        (
            "unsigned32",
            "Python int in [0, 4294967295] is bound to SQLite INTEGER; "
            "decode returns int.",
        ),
        (
            "counter64",
            "Python int in [0, 18446744073709551615] is emitted as canonical "
            "decimal TEXT so all 64 unsigned bits survive SQLite's signed "
            "INTEGER; decode returns int.",
        ),
        (
            "octet_string",
            "bytes is bound to SQLite BLOB without changing, padding, or "
            "text-decoding it; b'' is a real zero-length value; decode "
            "returns bytes.",
        ),
        (
            "ip_address",
            "Exactly four bytes are stored as BLOB; decode returns bytes.",
        ),
        (
            "opaque",
            "The complete ASN.1 Opaque value bytes are stored as BLOB; "
            "decode returns bytes.",
        ),
        (
            "oid",
            "A sequence of 1..128 non-negative 32-bit arcs is emitted as "
            "canonical dot-decimal TEXT, one generated column per INDEX "
            "component. Arcs are restricted to [0, 4294967294] because "
            "4294967295 is reserved by the SMI. No concatenation or length "
            "prefix is shared with another value, so component boundaries "
            "remain explicit; decode returns tuple[int, ...].",
        ),
        (
            "bits",
            "The BITS value is stored as its content-octet bytes; callers "
            "supply the exact bytes their ASN.1 codec produced, including "
            "trailing zero bits and any empty value, and decode returns "
            "those bytes unchanged.",
        ),
        (
            "enumeration",
            "The numeric enumeration value is bound to its integer storage "
            "representation; labels remain in the mapping; decode returns the "
            "numeric int.",
        ),
    ]
)


def _normal_name(name: str) -> str:
    """Return the case-insensitive physical spelling of a MIB descriptor."""
    return name.replace("-", "_").lower()


def _quote_identifier(name: str) -> str:
    """Quote a SQLite identifier, including names that are SQL keywords."""
    return '"' + name.replace('"', '""') + '"'


def _is_int(value: Any) -> bool:
    """Accept Python integers, excluding bool, but not numeric strings."""
    return isinstance(value, int) and not isinstance(value, bool)


def _bounded_integer(value: Any, minimum: int, maximum: int) -> int:
    """Validate and return an integer accepted by *minimum* and *maximum*."""
    if not _is_int(value):
        raise TypeError("an integer is required")

    if not minimum <= value <= maximum:
        raise ValueError("integer is outside the SMI range")

    return value


def encode_integer32(value: Any) -> int:
    """Bind a signed 32-bit SMI integer to SQLite INTEGER."""
    return _bounded_integer(value, _MIN_INT32, _MAX_INT32)


def decode_integer32(value: Any) -> int:
    """Restore a signed 32-bit value from SQLite INTEGER."""
    return _bounded_integer(value, _MIN_INT32, _MAX_INT32)


def encode_unsigned32(value: Any) -> int:
    """Bind an unsigned 32-bit SMI integer to SQLite INTEGER."""
    return _bounded_integer(value, 0, _MAX_UINT32)


def decode_unsigned32(value: Any) -> int:
    """Restore an unsigned 32-bit value from SQLite INTEGER."""
    return _bounded_integer(value, 0, _MAX_UINT32)


def encode_counter64(value: Any) -> str:
    """Emit all 64 unsigned bits as canonical decimal TEXT."""
    return str(_bounded_integer(value, 0, _MAX_UINT64))


def decode_counter64(value: Any) -> int:
    """Parse canonical decimal TEXT back to a full-range Python int."""
    if isinstance(value, bytes):
        value = value.decode("ascii")

    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        raise ValueError("counter64 storage must be decimal text")

    result = int(value)
    if not 0 <= result <= _MAX_UINT64:
        raise ValueError("counter64 value is outside its SMI range")

    return result


def encode_octet_string(value: Any) -> bytes:
    """Bind raw OCTET STRING bytes to SQLite BLOB unchanged."""
    if not isinstance(value, bytes):
        raise TypeError("bytes is required for an OCTET STRING value")

    return value


def decode_octet_string(value: Any) -> bytes:
    """Restore raw OCTET STRING bytes from SQLite BLOB."""
    if not isinstance(value, bytes):
        raise TypeError("SQLite did not return a BLOB")

    return value


def encode_ip_address(value: Any) -> bytes:
    """Bind the four bytes of an IpAddress to BLOB."""
    value = encode_octet_string(value)
    if len(value) != 4:
        raise ValueError("IpAddress must contain exactly four bytes")
    return value


def decode_ip_address(value: Any) -> bytes:
    """Restore the four-byte IpAddress."""
    value = decode_octet_string(value)
    if len(value) != 4:
        raise ValueError("stored IpAddress does not contain four bytes")
    return value


def encode_opaque(value: Any) -> bytes:
    """Bind complete Opaque content bytes to BLOB."""
    return encode_octet_string(value)


def decode_opaque(value: Any) -> bytes:
    """Restore complete Opaque content bytes."""
    return decode_octet_string(value)


def encode_oid(value: Any) -> str:
    """Emit an OID tuple as canonical dot-decimal TEXT."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("an OID must be a tuple or list of integer arcs")

    if not 1 <= len(value) <= _MAX_OID_ARCS:
        raise ValueError("an OID must contain 1..128 arcs")

    arcs: list[str] = []
    for arc in value:
        if not _is_int(arc) or not 0 <= arc <= _MAX_OID_SUBID:
            raise ValueError("every OID arc must be a 32-bit non-negative integer")
        arcs.append(str(arc))

    return ".".join(arcs)


def decode_oid(value: Any) -> tuple[int, ...]:
    """Parse canonical dot-decimal TEXT back to an OID tuple."""
    if isinstance(value, bytes):
        value = value.decode("ascii")

    if not isinstance(value, str):
        raise TypeError("SQLite did not return OID text")

    parts = value.split(".")
    if not 1 <= len(parts) <= _MAX_OID_ARCS:
        raise ValueError("stored OID must contain 1..128 arcs")

    arcs: list[int] = []
    for part in parts:
        if not part.isascii() or not part.isdigit() or (
            len(part) > 1 and part[0] == "0"
        ):
            raise ValueError("stored OID contains a non-canonical arc")
        arc = int(part)
        if arc > _MAX_OID_SUBID:
            raise ValueError("stored OID arc exceeds 32 bits")
        arcs.append(arc)

    return tuple(arcs)


def encode_bits(value: Any) -> bytes:
    """Bind ASN.1 BITS content octets to SQLite BLOB."""
    return encode_octet_string(value)


def decode_bits(value: Any) -> bytes:
    """Restore ASN.1 BITS content octets."""
    return decode_octet_string(value)


_ENCODERS: Final = {
    "integer32": encode_integer32,
    "unsigned32": encode_unsigned32,
    "counter64": encode_counter64,
    "octet_string": encode_octet_string,
    "ip_address": encode_ip_address,
    "opaque": encode_opaque,
    "oid": encode_oid,
    "bits": encode_bits,
    "enumeration": encode_integer32,
}

_DECODERS: Final = {
    "integer32": decode_integer32,
    "unsigned32": decode_unsigned32,
    "counter64": decode_counter64,
    "octet_string": decode_octet_string,
    "ip_address": decode_ip_address,
    "opaque": decode_opaque,
    "oid": decode_oid,
    "bits": decode_bits,
    "enumeration": decode_integer32,
}


def bind_parameters(mapping: Mapping[str, Any], values: Mapping[str, Any]) -> list[Any]:
    """Convert one logical row to SQLite parameters in ``write_columns`` order."""
    fields = mapping["fields"]
    by_column = {field["column"]: dict(field) for field in fields.values()}
    by_identity = {
        (field["module"], field["object"]): field for field in fields.values()
    }

    for key, value in values.items():
        if isinstance(key, str):
            if key not in fields:
                raise KeyError(f"unknown field {key!r}")
            field = fields[key]
        else:
            identity = tuple(key)
            if identity not in by_identity:
                raise KeyError(f"unknown field {identity!r}")
            field = by_identity[identity]

        by_column[field["column"]]["value"] = _ENCODERS[field["representation"]](
            value
        )

    return [by_column[column].get("value") for column in mapping["write_columns"]]


def decode_row(mapping: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    """Restore every selected column of one stored row to its logical value."""
    result: dict[str, Any] = {}

    for logical_name, field in mapping["fields"].items():
        column = field["column"]
        try:
            stored_value = row[column]
        except (KeyError, IndexError):
            continue
        result[logical_name] = _DECODERS[field["representation"]](stored_value)

    return result


class _IdentifierFactory:
    """Allocate collision-free, stable physical identifiers."""

    def __init__(self) -> None:
        """Start with no physical name allocated."""
        self._used: dict[str, tuple[tuple[str, str], str]] = {}

    def allocate(self, identity: tuple[str, str], requested: str) -> str:
        """Return the stable physical name for one named identity.

        The same identity always receives the name it first asked for. A
        different identity whose descriptor normalizes to the same spelling,
        including a reserved word, receives a deterministic ``_2`` suffix.
        """
        requested_key = _normal_name(requested)
        existing = self._used.get(requested_key)
        if existing is not None and existing[0] == identity:
            return requested

        if existing is None:
            self._used[requested_key] = identity, requested
            return requested

        counter = 2
        while True:
            candidate = f"{requested}_{counter}"
            candidate_key = _normal_name(candidate)
            if candidate_key not in self._used:
                self._used[candidate_key] = identity, candidate
                return candidate
            counter += 1


class _TypeDescription:
    """The storage facts resolved for one SMI syntax."""

    def __init__(
        self,
        representation: str,
        sqlite_type: str,
        semantic_type: str,
        *,
        ranges: Sequence[ValueRanges] = (),
        labels: Mapping[str, int] | None = None,
    ) -> None:
        """Carry the representation, SQL type and original constraints."""
        self.representation = representation
        self.sqlite_type = sqlite_type
        self.semantic_type = semantic_type
        self.ranges = tuple(ranges)
        self.labels = dict(labels or {})


@deprecated_camel_case
class SqliteCodeGen(JsonCodeGen):
    """Generate SQLite DDL and reversible parameter-write field mappings.

    The backend only describes storage. It neither performs SNMP collection
    nor emits an ORM: the generated JSON contains ``CREATE TABLE`` statements
    and the metadata needed to bind logical Python values to ``?`` parameters
    and to restore them after a ``SELECT``.
    """

    def __init__(self) -> None:
        """Initialize the JSON backend state used for resolving the module."""
        super().__init__()
        self._module = ""
        self._resolved_oids: dict[tuple[str, str], tuple[int, ...]] = {}
        self._table_names = _IdentifierFactory()
        self._table_mappings: "OrderedDict[str, dict[str, Any]]" = OrderedDict()

    def _symbol(self, module: str, name: str) -> dict[str, Any]:
        """Return a symbol table entry or report the missing reference."""
        if module not in self.symbolTable or name not in self.symbolTable[module]:
            raise error.PySmiCodegenError(
                f'cannot resolve MIB symbol "{name}" from module "{module}"'
            )

        return self.symbolTable[module][name]

    def _numeric_oid(self, module: str, name: str) -> tuple[int, ...]:
        """Resolve and cache one symbol's fully numeric OID."""
        key = (module, name)
        if key not in self._resolved_oids:
            symbol = self._symbol(module, name)
            oid = symbol.get("oid")
            if not isinstance(oid, tuple):
                raise error.PySmiCodegenError(f"symbol {module}::{name} has no OID")
            self._resolved_oids[key] = tuple(
                int(x) for x in self.gen_numeric_oid(oid)
            )

        return self._resolved_oids[key]

    @staticmethod
    def _kind(name: str) -> str:
        """Normalize an SMI type spelling for kind comparison."""
        return (
            name.replace("-", "")
            .replace("_", "")
            .replace(" ", "")
            .upper()
        )

    @staticmethod
    def _constraints(
        subtype: Any,
        ranges: list[ValueRanges],
        labels: dict[str, int],
    ) -> tuple[list[ValueRanges], dict[str, int]]:
        """Collect one link's ranges or named values without mutation."""
        if isinstance(subtype, ValueRanges):
            ranges.append(subtype)
        elif isinstance(subtype, list) and subtype and all(
            isinstance(item, tuple) and len(item) == 2 for item in subtype
        ):
            pairs = [(str(label), int(value)) for label, value in subtype]
            if all(_MIN_INT32 <= value <= _MAX_INT32 for _label, value in pairs):
                labels.update(pairs)
        return ranges, labels

    def _resolve_type(self, syntax: tuple[Any, Any]) -> _TypeDescription:
        """Resolve a column syntax to its reversible SQLite representation."""
        (type_name, module), subtype = syntax
        original = str(type_name)
        original_kind = self._kind(original)
        ranges: list[ValueRanges] = []
        labels: dict[str, int] = {}

        if original_kind in _INTEGER_TYPES and not module:
            ranges, labels = self._constraints(subtype, ranges, labels)
            representation = "enumeration" if labels else "integer32"
            return _TypeDescription(
                representation,
                "INTEGER",
                original,
                ranges=ranges,
                labels=labels,
            )

        if original_kind in _OCTET_TYPES and not module:
            ranges, labels = self._constraints(subtype, ranges, labels)
            return _TypeDescription(
                "octet_string", "BLOB", original, ranges=ranges
            )

        if original_kind in _OID_TYPES and not module:
            return _TypeDescription("oid", "TEXT", original)

        if original_kind == "IPADDRESS" and not module:
            return _TypeDescription("ip_address", "BLOB", original)

        if original_kind == "NETWORKADDRESS":
            raise error.PySmiCodegenError(
                "NetworkAddress indexes need their SMIv1 address-family "
                "discriminator and have no reversible scalar storage mapping"
            )

        if original_kind == "BITS" and not module:
            ranges, labels = self._constraints(subtype, ranges, labels)
            return _TypeDescription(
                "bits",
                "BLOB",
                original,
                ranges=ranges,
                labels=labels,
            )

        current_name, current_module = str(type_name), str(module)
        chain: set[tuple[str, str]] = set()
        first_kind = original_kind

        while (current_module, current_name) not in chain:
            chain.add((current_module, current_name))
            symbol_type, symbol_subtype = self._symbol(
                current_module, current_name
            ).get("syntax", (("", ""), ""))
            ranges, labels = self._constraints(symbol_subtype, ranges, labels)

            base_name = str(symbol_type[0])
            base_module = str(symbol_type[1])
            base_kind = self._kind(base_name)

            if not base_module and base_kind in _INTEGER_TYPES:
                counter64 = any(
                    restriction.kind == "range"
                    and restriction.bounds == ((0, _MAX_UINT64),)
                    for restriction in ranges
                )
                if counter64:
                    return _TypeDescription(
                        "counter64", "TEXT", original, ranges=ranges
                    )

                unsigned = first_kind in _UNSIGNED_32_TYPES or any(
                    restriction.kind == "range"
                    and restriction.bounds
                    and restriction.bounds[0][0] >= 0
                    and restriction.bounds[-1][1] > _MAX_INT32
                    for restriction in ranges
                )
                representation = (
                    "enumeration"
                    if labels
                    else "unsigned32"
                    if unsigned
                    else "integer32"
                )
                return _TypeDescription(
                    representation,
                    "INTEGER",
                    original,
                    ranges=ranges,
                    labels=labels,
                )

            if not base_module and base_kind in _OCTET_TYPES:
                representation = (
                    "ip_address"
                    if first_kind == "IPADDRESS"
                    else "opaque"
                    if first_kind == "OPAQUE"
                    else "octet_string"
                )
                return _TypeDescription(
                    representation,
                    "BLOB",
                    original,
                    ranges=ranges,
                )

            if not base_module and base_kind in _OID_TYPES:
                return _TypeDescription("oid", "TEXT", original)

            if not base_module and base_kind == "BITS":
                return _TypeDescription(
                    "bits", "BLOB", original, ranges=ranges, labels=labels
                )

            current_name, current_module = base_name, base_module

        raise error.PySmiCodegenError(
            f"type chain for {module}::{type_name} "
            "does not reach a supported base type"
        )

    @staticmethod
    def _constraint_metadata(description: _TypeDescription) -> dict[str, Any]:
        """Serialize the SMI restrictions kept alongside a storage type."""
        metadata: dict[str, Any] = {}
        ranges = [
            {
                "kind": restriction.kind,
                "min": low,
                "max": high,
            }
            for restriction in description.ranges
            for low, high in restriction.bounds
        ]
        if ranges:
            metadata["ranges"] = ranges
        if description.labels:
            metadata["labels"] = OrderedDict(sorted(description.labels.items()))
        return metadata

    @staticmethod
    def _requested_name(name: str) -> str:
        """Turn a MIB descriptor into a requested SQLite identifier."""
        return name.replace("-", "_")

    @staticmethod
    def _is_under(child: tuple[int, ...], parent: tuple[int, ...]) -> bool:
        """Tell whether *child* is exactly one SMI sub-identifier below *parent*."""
        return len(child) == len(parent) + 1 and child[: len(parent)] == parent

    def _local_objects(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Return this module's tables, rows and immediate row columns."""
        symbols = self.symbolTable[self._module]
        tables = {
            name: symbol
            for name, symbol in symbols.items()
            if isinstance(symbol, dict)
            and symbol.get("type") == "ObjectType"
            and symbol.get("syntax", (("", ""), ""))[0] == ("MibTable", "")
        }
        rows = {
            name: symbol
            for name, symbol in symbols.items()
            if isinstance(symbol, dict)
            and symbol.get("type") == "ObjectType"
            and symbol.get("syntax", (("", ""), ""))[0] == ("MibTableRow", "")
        }

        row_oids = {
            (self._module, name): self._numeric_oid(self._module, name)
            for name in rows
        }
        columns: dict[tuple[str, str], dict[str, Any]] = {}

        for name, symbol in symbols.items():
            if not isinstance(symbol, dict) or symbol.get("type") not in (
                "ObjectType",
                "fakeColumn",
            ):
                continue
            if symbol.get("syntax", (("", ""), ""))[0] in (
                ("MibTable", ""),
                ("MibTableRow", ""),
            ):
                continue

            oid = self._numeric_oid(self._module, name)
            for row_key, row_oid in row_oids.items():
                if self._is_under(oid, row_oid):
                    columns[row_key] = columns.get(row_key, {})
                    columns[row_key][name] = {
                        "symbol": symbol,
                        "oid": oid,
                        "subidentifier": oid[-1],
                    }

        return tables, rows, columns

    def _row_indexes(
        self,
        row_key: tuple[str, str],
        _seen: frozenset[tuple[str, str]] = frozenset(),
    ) -> tuple[tuple[dict[str, Any], ...], tuple[str, str] | None]:
        """Resolve a row's INDEX, following AUGMENTS to the indexed row."""
        if row_key in _seen:
            raise error.PySmiCodegenError(f"AUGMENTS cycle through {row_key!r}")

        row = self._symbol(*row_key)
        indices = row.get("indices")
        if indices:
            return tuple(
                {
                    "position": position,
                    "implied": bool(implied),
                    "module": module,
                    "object": object_name,
                }
                for position, (implied, module, object_name) in enumerate(indices)
            ), row_key

        augmentation = row.get("augmentation")
        if augmentation:
            target = (augmentation["module"], augmentation["object"])
            return self._row_indexes(target, _seen | {row_key})

        raise error.PySmiCodegenError(
            f"row {row_key[0]}::{row_key[1]} declares neither INDEX nor AUGMENTS"
        )

    def _field_identity_name(
        self, module: str, object_name: str, role: str
    ) -> str:
        """Return the stable logical field name exposed to write callers."""
        if role == "index" and module != self._module:
            return f"{module}.{object_name}"
        return object_name

    def _add_field(
        self,
        *,
        fields: "OrderedDict[str, dict[str, Any]]",
        factory: _IdentifierFactory,
        module: str,
        object_name: str,
        role: str,
        description: _TypeDescription,
        index: dict[str, Any] | None = None,
    ) -> str:
        """Allocate one physical column and record its reversible mapping."""
        identity = (module, object_name)
        requested = self._requested_name(object_name)
        physical = factory.allocate(identity, requested)
        logical = self._field_identity_name(module, object_name, role)

        if logical in fields and fields[logical]["column"] != physical:
            logical = f"{module}.{object_name}"
            counter = 2
            while logical in fields:
                logical = f"{module}.{object_name}_{counter}"
                counter += 1

        field = OrderedDict(
            [
                ("name", logical),
                ("module", module),
                ("object", object_name),
                ("role", role),
                ("column", physical),
                ("representation", description.representation),
                ("sqlite_type", description.sqlite_type),
                ("smi_type", description.semantic_type),
            ]
        )
        constraints = self._constraint_metadata(description)
        if constraints:
            field["constraints"] = constraints
        if index is not None:
            field["index"] = OrderedDict(
                [
                    ("position", index["position"]),
                    ("implied", int(index["implied"])),
                ]
            )

        fields[logical] = field
        return physical

    def _table_for_row(
        self,
        row_name: str,
        row: dict[str, Any],
        tables: dict[str, Any],
    ) -> str:
        """Find the conceptual table whose OID immediately contains a row."""
        row_oid = self._numeric_oid(self._module, row_name)
        for table_name in tables:
            table_oid = self._numeric_oid(self._module, table_name)
            if self._is_under(row_oid, table_oid):
                return table_name
        raise error.PySmiCodegenError(f"row {row_name} has no containing table")

    def _build_table(
        self,
        table_name: str,
        row_name: str,
        columns: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Build one physical table's DDL and parameter write mapping."""
        row_key = (self._module, row_name)
        indexes, indexed_row = self._row_indexes(row_key)
        factory = _IdentifierFactory()
        fields: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        primary_key: list[str] = []

        for index in indexes:
            index_key = (index["module"], index["object"])
            index_symbol = self._symbol(*index_key)
            syntax = index_symbol.get("syntax", (("", ""), ""))
            description = self._resolve_type(syntax)
            physical = self._add_field(
                fields=fields,
                factory=factory,
                module=index_key[0],
                object_name=index_key[1],
                role="index",
                description=description,
                index=index,
            )
            primary_key.append(physical)

        local_column_objects = {
            name: details
            for name, details in columns.items()
            if (self._module, name)
            not in {
                (field["module"], field["object"]) for field in fields.values()
            }
        }
        for object_name, details in sorted(
            local_column_objects.items(), key=lambda item: item[1]["subidentifier"]
        ):
            syntax = details["symbol"].get("syntax", (("", ""), ""))
            description = self._resolve_type(syntax)
            self._add_field(
                fields=fields,
                factory=factory,
                module=self._module,
                object_name=object_name,
                role="column",
                description=description,
            )

        physical_table = self._table_names.allocate(
            (self._module, table_name),
            self._requested_name(f"{self._module}_{table_name}"),
        )
        definitions = []
        for field in fields.values():
            nullable = " NOT NULL" if field["column"] in primary_key else ""
            definitions.append(
                f"{_quote_identifier(field['column'])} {field['sqlite_type']}"
                f"{nullable}"
            )
        definitions.append(
            "PRIMARY KEY ("
            + ", ".join(_quote_identifier(name) for name in primary_key)
            + ")"
        )
        ddl = (
            f"CREATE TABLE {_quote_identifier(physical_table)} (\n  "
            + ",\n  ".join(definitions)
            + "\n)"
        )
        write_columns = [field["column"] for field in fields.values()]
        insert_sql = (
            f"INSERT INTO {_quote_identifier(physical_table)} ("
            + ", ".join(_quote_identifier(name) for name in write_columns)
            + ") VALUES ("
            + ", ".join("?" for _name in write_columns)
            + ")"
        )

        return OrderedDict(
            [
                ("module", self._module),
                (
                    "table",
                    OrderedDict(
                        [("object", table_name), ("name", physical_table)]
                    ),
                ),
                (
                    "row",
                    OrderedDict(
                        [
                            ("object", row_name),
                            ("indexed_by", f"{indexed_row[0]}::{indexed_row[1]}"),
                        ]
                    ),
                ),
                ("primary_key", primary_key),
                ("write_columns", write_columns),
                ("insert_sql", insert_sql),
                ("ddl", ddl),
                ("fields", fields),
            ]
        )

    def _build_document(self) -> dict[str, Any]:
        """Build the complete SQLite storage mapping for one module."""
        self._module = self.moduleName[0]
        self._resolved_oids.clear()
        self._table_names = _IdentifierFactory()
        self._table_mappings.clear()

        tables, rows, columns = self._local_objects()
        row_by_table: dict[str, str] = {}
        for row_name in rows:
            table_name = self._table_for_row(row_name, rows[row_name], tables)
            row_by_table[table_name] = row_name

        for table_name in sorted(
            row_by_table,
            key=lambda name: (self._numeric_oid(self._module, name), name),
        ):
            row_name = row_by_table[table_name]
            mapping = self._build_table(
                table_name, row_name, columns.get((self._module, row_name), {})
            )
            self._table_mappings[table_name] = mapping

        document = OrderedDict(
            [
                (
                    "meta",
                    OrderedDict(
                        [
                            ("schema", SCHEMA_VERSION),
                            ("module", self._module),
                            ("kind", "sqlite-storage"),
                        ]
                    ),
                ),
                ("representations", OrderedDict(_ENCODING_RULES)),
                (
                    "ddl",
                    [
                        table_mapping["ddl"]
                        for table_mapping in self._table_mappings.values()
                    ],
                ),
                (
                    "tables",
                    list(self._table_mappings.values()),
                ),
            ]
        )

        return document

    def gen_code(
        self, ast: Any, symbolTable: dict[str, Any], **kwargs: Any
    ) -> tuple[MibInfo, str]:
        """Generate SQLite storage mappings for one parsed MIB module.

        The inherited JSON walk resolves and validates the same parse tree;
        its emitted JSON document is intentionally discarded. This method
        emits storage mapping JSON only, and never performs collection or
        database access.
        """
        mib_info, _document = super().gen_code(ast, symbolTable, **kwargs)
        document = self._build_document()
        return mib_info, jsonio.dumps(document)

    def gen_index(self, processed: dict[str, Any], **kwargs: Any) -> str:
        """Storage mappings have no cross-module generated index."""
        return ""
