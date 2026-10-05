"""Symbol-level pruning of parsed MIB modules.

A full compile carries every symbol of every module it parses, which is more
than a deployment that polls a handful of OIDs wants to ship. This module
restricts a compiled module to a named set of *target* symbols and to the
symbol-level closure that lets those targets load and mean the same thing:

* every OID ancestor a kept symbol hangs its OID off;
* every type a kept object or textual convention derives from;
* the table row, its index columns, and the columns a kept target needs;
* every row related through an AUGMENTS clause, in both directions;
* the objects a kept notification carries in its OBJECTS clause;
* the objects a notification or object group names -- a group kept as a
  target is expanded to the closure of everything it references;
* the groups and objects a MODULE-COMPLIANCE names, and the groups and
  variations an AGENT-CAPABILITIES names;
* exactly the imports a kept symbol needs to load, and nothing else.

Module-level dependency handling is deliberately not enough here: pulling a
module because one symbol refers to it drags back every symbol the module
defines. The closure is computed on individual symbols, across every module
the compiler parsed, so a referred vendor module is reduced to what the
reference reaches rather than copied wholesale.

A target that names no symbol the module defines, or a dependency that
cannot be resolved to a kept symbol or an import, is a failure rather than a
guess.
"""

import logging
from collections import defaultdict
from keyword import iskeyword
from typing import Any, Final

from pysmi import error
from pysmi.codegen.base import AbstractCodeGen, REPAIRED_IMPORTS_KEY
from pysmi.codegen.symtable import SymtableCodeGen

logger = logging.getLogger(__name__)

#: Tag of an OBJECT-TYPE declaration, spelled as the parser tags it.
_OBJECT_TYPE: Final = "objectTypeClause"

#: Declarations that carry an OID and nothing the closure needs besides it.
_OID_ONLY_TAGS: Final[frozenset[str]] = frozenset(
    ("valueDeclaration", "objectIdentityClause", "moduleIdentityClause")
)

#: Construct tokens to keep imported per declaration tag. Each is the name as
#: it must appear on an IMPORTS line, or ``None`` for a tag whose construct is
#: always supplied as a constant import.
_CONSTRUCT_BY_TAG: Final[dict[str, str | None]] = {
    "moduleIdentityClause": "MODULE-IDENTITY",
    "objectIdentityClause": "OBJECT-IDENTITY",
    _OBJECT_TYPE: "OBJECT-TYPE",
    "notificationTypeClause": "NOTIFICATION-TYPE",
    "trapTypeClause": "TRAP-TYPE",
    "objectGroupClause": "OBJECT-GROUP",
    "notificationGroupClause": "NOTIFICATION-GROUP",
    "moduleComplianceClause": "MODULE-COMPLIANCE",
    "agentCapabilitiesClause": "AGENT-CAPABILITIES",
}

#: Tags that describe conformance rather than a managed object. A target
#: naming one expands through the objects it references, not just through its
#: OID ancestry.
_GROUP_TAGS: Final[frozenset[str]] = frozenset(
    ("objectGroupClause", "notificationGroupClause")
)

#: Tokens that name a built-in ASN.1 or SMI base type. A syntax that resolves
#: to one of these needs no local symbol and no vendor import; the code
#: generator already imports the implementation class through its constant
#: imports.
_BUILTIN_SYNTAX_NAMES: Final[frozenset[str]] = frozenset(
    (
        "INTEGER",
        "INTEGER32",
        "OCTET STRING",
        "OBJECT IDENTIFIER",
        "BITS",
        "COUNTER32",
        "COUNTER64",
        "GAUGE32",
        "UNSIGNED32",
        "TIMETICKS",
        "IPADDRESS",
        "OPAQUE",
        "NETWORKADDRESS",
        "Counter",
        "Gauge",
        # Python-side class names the resolved symbol table may already hold.
        "Integer",
        "Integer32",
        "Bits",
        "Counter32",
        "Counter64",
        "Gauge32",
        "Unsigned32",
        "TimeTicks",
        "IpAddress",
        "NetworkAddress",
        "Opaque",
        "OctetString",
        "ObjectIdentifier",
        "MibTable",
        "MibTableRow",
        "MibTableColumn",
    )
)


def _py_name(symbol: str) -> str:
    """Apply the symbol table's name rules without an instance.

    Args:
        symbol: symbol name as written in the MIB

    Returns:
        The Python-safe form the symbol table keys a symbol under.
    """
    if iskeyword(symbol):
        symbol = "pysmi_" + symbol

    return symbol.replace("-", "_")


def _oid_names(oid: Any) -> list[str]:
    """The named sub-identifiers of a parsed OID clause.

    Args:
        oid: an ``('objectIdentifier', [...])`` clause, or ``None``

    Returns:
        Every named sub-identifier, in OID order, including the root of an
        ``iso(1)`` form. Bare numbers contribute nothing.
    """
    if not oid:
        return []

    names = []
    for part in oid[1]:
        if isinstance(part, str):
            names.append(part)

        elif isinstance(part, tuple):
            names.append(part[0])

    return names


def _syntax_type_names(syntax: Any) -> list[str]:
    """The named types referenced inside a raw SYNTAX subtree.

    Restrictions (ranges, sizes, named numbers) carry no references; only the
    tags that name another type do. Conceptual tables and rows name an entry
    type rather than a syntax, so those are collected too -- the caller knows
    from the clause what each one means.

    Args:
        syntax: a raw syntax subtree, or ``None``

    Returns:
        Raw type names referenced by the subtree, in encounter order.
    """
    names: list[str] = []

    if not isinstance(syntax, tuple):
        return names

    tag = syntax[0]

    if tag == "SimpleSyntax":
        for el in syntax[1:]:
            if isinstance(el, str) and el not in (
                "INTEGER",
                "INTEGER32",
                "OCTET STRING",
                "OBJECT IDENTIFIER",
            ):
                names.append(el)

    elif tag == "ApplicationSyntax":
        for el in syntax[1:]:
            if isinstance(el, str):
                names.append(el)

    elif tag == "conceptualTable":
        names.extend(_syntax_type_names(syntax[1]))

    elif tag == "row":
        names.append(syntax[1])

    return names

#: The bare SMIv1 types an INDEX may name in place of a column. One of these
#: makes the code generator synthesize a column itself, so the name is a base
#: type rather than a symbol to keep or import.
_SMIV1_INDEX_TYPES: Final[frozenset[str]] = frozenset(
    ("INTEGER", "OCTET STRING", "IPADDRESS", "NETWORKADDRESS")
)


def _is_fake_column(pyname: str) -> bool:
    """Whether a symbol-table name is a column synthesized for an SMIv1 index."""
    return pyname.startswith("pysmiFakeCol")


def _constant_import_symbols() -> frozenset[tuple[str, str]]:
    """The (module, symbol) pairs the pysnmp generator always imports itself.

    These come from the backend's ``constImports`` and are re-added to every
    generated module whether or not the MIB named them, so a token whose only
    need is one of them does not have to survive on the rewritten IMPORTS
    line. A macro the generator does not supply (OBJECT-GROUP,
    NOTIFICATION-GROUP) is kept under the token the module imported it as.
    """
    from pysmi.codegen.pysnmp import PySnmpCodeGen

    pairs: set[tuple[str, str]] = set()
    for module, symbols in PySnmpCodeGen.constImports.items():
        for symbol in symbols:
            for expanded in SymtableCodeGen.symsTable.get(symbol, (symbol,)):
                pairs.add((module, expanded))

    return frozenset(pairs)


class _ModuleScope:
    """The parsed facts about one module the closure is computed across."""

    def __init__(self, name: str, ast: Any, table: dict[str, Any]) -> None:
        self.name = name
        self.ast = ast
        self.table = table

        #: Every declaration, indexed by the Python-safe name it defines. A
        #: SEQUENCE and the row object it describes share a name, and so may a
        #: handful of broken MIBs, hence the list.
        self.decls: dict[str, list[Any]] = defaultdict(list)

        for decl in ast[3] or []:
            if decl:
                self.decls[_py_name(decl[1])].append(decl)

        #: Rows (Python-safe names) another local or foreign row augments.
        self.augmentees: dict[str, list[tuple[str, str]]] = defaultdict(list)

        #: Construct tokens a kept declaration has to keep imported.
        self.required_constructs: set[str] = set()

        #: Foreign symbols the kept declarations load: raw symbol name per
        #: owning module, with the reason the import is needed.
        self.required_imports: dict[str, dict[str, str]] = defaultdict(dict)

        (
            self._effective_imports,
            self._import_sources,
            self._raw_import_tokens,
        ) = self._build_imports(ast)

    def _build_imports(
        self, ast: Any
    ) -> tuple[
        dict[str, str],
        dict[str, tuple[str, str]],
        dict[str, tuple[str, str]],
    ]:
        """Rewrite the module's own IMPORTS line as the symbol table does.

        The same SMIv1 conversions the symbol table applies are applied here,
        and the symbols the symbol table repaired onto the line are merged in,
        so a foreign name resolves against exactly what the module may
        reference. Constant imports are deliberately not part of this map: the
        generator re-adds them itself, so they never need to survive on the
        rewritten AST.

        Returns:
            A Python-safe symbol -> owner module map, and a map from that
            symbol to the raw name that must appear on the rewritten IMPORTS
            line and the module it comes from.
        """
        raw = {module: list(symbols) for module, symbols in (ast[2] or {}).items()}

        for symbol, module in self.table.get(REPAIRED_IMPORTS_KEY, {}).items():
            raw.setdefault(module, []).append(symbol)

        converted: dict[str, list[str]] = {}

        for module in list(raw):
            if module not in AbstractCodeGen.convertImportv2:
                converted.setdefault(module, []).extend(raw[module])
                continue

            table = AbstractCodeGen.convertImportv2[module]
            for symbol in raw[module]:
                if symbol in table:
                    for new_module, new_symbol in table[symbol]:
                        converted.setdefault(new_module, []).append(new_symbol)
                else:
                    converted.setdefault(module, []).append(symbol)

        effective: dict[str, str] = {}
        sources: dict[str, tuple[str, str]] = {}
        raw_tokens: dict[str, tuple[str, str]] = {}

        for module, symbols in converted.items():
            for symbol in symbols:
                raw_tokens[symbol] = (module, symbol)
                for expanded in self._sym_expansion(symbol):
                    pyname = _py_name(expanded)
                    effective[pyname] = module
                    sources.setdefault(pyname, (module, expanded))

        return effective, sources, raw_tokens

    @staticmethod
    def _sym_expansion(symbol: str) -> tuple[str, ...]:
        """The symbols one imported construct introduces, as the symbol table
        maps them."""
        return SymtableCodeGen.symsTable.get(symbol, (symbol,))

    @property
    def local_symbols(self) -> frozenset[str]:
        """Symbols this module defines, excluding bookkeeping keys."""
        return frozenset(
            key
            for key in self.table
            if not key.startswith("_") and not _is_fake_column(key)
        )

    def declaration(self, pyname: str) -> Any:
        """The non-SEQUENCE declaration that defines *pyname*, if any."""
        for decl in self.decls.get(pyname, ()):
            if decl[0] != "typeDeclaration" or not self._is_sequence_decl(decl):
                return decl

        return None

    @staticmethod
    def _is_sequence_decl(decl: Any) -> bool:
        """Whether a type declaration is a bare ``SEQUENCE``."""
        if decl[0] != "typeDeclaration":
            return False

        rhs = decl[2]
        return bool(rhs) and len(rhs) == 2 and rhs[1][0] == "SEQUENCE"

    def is_row(self, pyname: str) -> bool:
        """Whether *pyname* is a row the symbol table recorded."""
        # The symbol table keys a row object under the object's own name
        # (``expEntry``), but records the row under the SEQUENCE type's name
        # (``ExpEntry``). The two describe one row, so recognize both.
        rows = {_py_name(name) for name in self.table.get("_symtable_rows", ())}
        return pyname in rows


class SymbolPruner:
    """Compute symbol-level closures and reduce parsed modules to them.

    One instance carries the state of one prune run across all parsed
    modules: the per-module scopes, what has been kept in each module, and
    why. Use :py:meth:`prune` rather than constructing one directly.
    """

    def __init__(
        self,
        asts: dict[str, Any],
        tables: dict[str, dict[str, Any]],
        active_modules: frozenset[str],
    ) -> None:
        self._scopes = {
            name: _ModuleScope(name, asts[name], tables[name])
            for name in active_modules
            if name in asts and name in tables
        }
        self._tables = tables
        self._active = frozenset(self._scopes)

        #: Per-module kept symbol -> human-readable reason.
        self.reasons: dict[str, dict[str, str]] = {
            name: {} for name in self._scopes
        }

        #: Worklist of ``(module, pyname, reason)`` tuples still to close.
        self._pending: list[tuple[str, str, str]] = []

        #: Foreign symbols each module must import:
        #: ``module -> raw symbol -> owner module``.
        self._import_owners: dict[str, dict[str, str]] = defaultdict(dict)

    # -- public entry point -------------------------------------------------

    def run(
        self, targets: dict[str, list[str]]
    ) -> tuple[
        dict[str, dict[str, str]],
        dict[str, dict[str, str]],
    ]:
        """Seed the closure from *targets* and close it to a fixed point.

        Args:
            targets: raw target symbol names per module.

        Returns:
            A tuple of the per-module kept symbols with their reasons and the
            per-module imports the kept symbols need, each as
            ``raw symbol -> owner module``.

        Raises:
            PySmiPruneError: a target names no symbol the module defines, or a
                symbol that has to be kept cannot be resolved.
        """
        for module, raw_names in targets.items():
            scope = self._require_scope(module)

            for raw_name in raw_names:
                pyname = _py_name(raw_name)

                if pyname not in scope.local_symbols:
                    raise error.PySmiPruneError(
                        f'target symbol "{raw_name}" is not defined in module '
                        f'"{module}"',
                        mibname=module,
                        symbol=raw_name,
                    )

                self._seed(module, pyname, "target symbol")

        self._index_augmentees()
        self._close()

        imports = {
            module: dict(owners) for module, owners in self._import_owners.items()
        }
        return self.reasons, imports

    # -- closure -------------------------------------------------------------

    def _require_scope(self, module: str) -> _ModuleScope:
        """The scope of *module*, failing a target naming an unknown module."""
        try:
            return self._scopes[module]

        except KeyError:
            raise error.PySmiPruneError(
                f'cannot restrict to symbols: module "{module}" was not compiled',
                mibname=module,
            ) from None

    def _seed(self, module: str, pyname: str, reason: str) -> None:
        """Add *pyname* to a module's closure unless it is already there."""
        if pyname not in self.reasons[module]:
            scope = self._scopes[module]
            is_sequence = pyname in scope.decls and any(
                scope._is_sequence_decl(decl)
                for decl in scope.decls[pyname]
            )

            if pyname not in scope.table and not is_sequence:
                raise error.PySmiPruneError(
                    f'symbol "{pyname}" required by a kept clause is not '
                    f'defined in module "{module}"',
                    mibname=module,
                    symbol=pyname,
                )

            self.reasons[module][pyname] = reason
            self._pending.append((module, pyname, reason))

    def _index_augmentees(self) -> None:
        """Record, per row, which rows carry an ``AUGMENTS { row }`` clause.

        The relation has to be kept in both directions: a kept augmenting row
        keeps the row it augments, and a kept augmented row keeps the rows
        that augment it, because both copies of the index registration are
        what a loader replays.
        """
        for scope in self._scopes.values():
            for pyname in scope.local_symbols:
                decl = scope.declaration(pyname)
                if not decl or decl[0] != _OBJECT_TYPE or not decl[8]:
                    continue

                target_raw = decl[8]
                target_py = _py_name(target_raw)
                target_module = self._owner_of(scope, target_py)
                if target_module in self._active:
                    self._scopes[target_module].augmentees[target_py].append(
                        (scope.name, pyname)
                    )
                    # The import the OBJECT-TYPE's AUGMENTS renders is handled
                    # when the augmenting row itself is closed.

    def _close(self) -> None:
        """Drain the worklist, adding each dependency a kept symbol needs."""
        while self._pending:
            module, pyname, reason = self._pending.pop()
            scope = self._scopes[module]
            decl = scope.declaration(pyname)

            self._keep_row_sequence(scope, pyname)

            self._keep_oid_ancestors(scope, pyname, decl)

            if decl is None:
                # A fake column or a SEQUENCE type entry reached the list;
                # only real declarations carry further edges.
                continue

            tag = decl[0]
            construct = _CONSTRUCT_BY_TAG.get(tag)
            if construct is not None:
                scope.required_constructs.add(construct)

            handler = self._EDGE_HANDLERS.get(tag)
            if handler is not None:
                handler(self, scope, pyname, decl, reason)

    # -- shared edges --------------------------------------------------------

    def _keep_row_sequence(self, scope: _ModuleScope, pyname: str) -> None:
        """Keep the SEQUENCE type declaration of a kept table row.

        A row OBJECT-TYPE and the entry type it is declared with are two
        declarations for one row. The generated row class is the object, but
        the symbol table records the row under the SEQUENCE type's name and
        lists that name in ``_symtable_rows``; dropping it loses the row
        classification and every column check against it.
        """
        props = scope.table.get(pyname, {})
        if props.get("type") != "ObjectType":
            return

        syntax = props.get("syntax")
        if not syntax or syntax[0][0] != "MibTableRow":
            return

        for entry_py in scope.table.get("_symtable_rows", ()):
            if self._row_object_for_entry(scope, entry_py) == pyname:
                self._seed(scope.name, entry_py, "SEQUENCE entry of kept row")
                return

    def _keep_oid_ancestors(
        self, scope: _ModuleScope, pyname: str, decl: Any
    ) -> None:
        """Keep every named ancestor the symbol's OID is relative to."""
        if decl is None:
            props = scope.table.get(pyname)
            oid = props.get("oid") if props else None
            names = self._names_from_resolved_oid(oid)
        else:
            names = self._decl_oid_names(decl)

        for raw_name in names:
            self._reference(scope, raw_name, "OID ancestor")

    @staticmethod
    def _names_from_resolved_oid(oid: Any) -> list[str]:
        """Named ancestors from a symbol-table, already-resolved OID.

        A resolved OID holds ``(pyname, module)`` pairs rather than raw names;
        a synthetic column keyed by its table entry has no declaration but its
        OID still hangs off that row.
        """
        names = []
        for part in oid or ():
            if isinstance(part, tuple):
                names.append(part[0])

        return names

    @staticmethod
    def _decl_oid_names(decl: Any) -> list[str]:
        """The OID clause of a declaration, whatever the clause's shape."""
        tag = decl[0]

        if tag in (
            "valueDeclaration",
            "moduleIdentityClause",
            "objectIdentityClause",
            "objectGroupClause",
            "notificationGroupClause",
            "moduleComplianceClause",
            "agentCapabilitiesClause",
            "notificationTypeClause",
        ):
            oid = decl[-1]
            return _oid_names(oid)

        if tag == _OBJECT_TYPE:
            return _oid_names(decl[11])

        if tag == "trapTypeClause":
            return _oid_names(decl[2])

        return []

    # -- reference resolution ------------------------------------------------

    @staticmethod
    def _row_object_for_entry(scope: _ModuleScope, entry_py: str) -> str | None:
        """The row OBJECT-TYPE that uses entry type *entry_py*, if any.

        A SEQUENCE type and the row object typed with it are distinct
        declarations sharing one row. The table and the row's own SYNTAX name
        the entry type; this returns the row object's name so the closure
        keeps the object that carries the OID, index and AUGMENTS clauses.
        """
        for pyname in scope.local_symbols:
            decl = scope.declaration(pyname)
            if decl and decl[0] == _OBJECT_TYPE and decl[2] == ("row", entry_py):
                return pyname

        return None

    def _owner_of(self, scope: _ModuleScope, pyname: str) -> str:
        """The module that defines or exports *pyname* from *scope*'s view.

        Local definitions win over imports. Foreign names resolve against the
        module's effective imports -- its own IMPORTS line rewritten as the
        symbol table rewrites it, plus repaired imports. A name found nowhere
        is reported as ``""``, which the caller turns into a failure.
        """
        if pyname in scope.local_symbols:
            return scope.name

        return scope._effective_imports.get(pyname, "")

    def _raw_import_name(self, scope: _ModuleScope, raw_name: str) -> str:
        """The raw name to keep on an IMPORTS line for *raw_name*.

        A raw name converted from an SMIv1 spelling (``Counter`` ->
        ``Counter32`` and the rest) is exported under the converted name, so
        that is what the import names.
        """
        source = scope._import_sources.get(_py_name(raw_name))
        return source[1] if source else raw_name

    def _reference(self, scope: _ModuleScope, raw_name: str, reason: str) -> None:
        """Resolve and keep or import one symbol referenced by a kept clause.

        A name resolving to a local definition joins the closure; a name
        another parsed module defines joins that module's closure; a name only
        an imported module supplies is recorded as an import to keep. A name
        that resolves nowhere is a hard failure.

        Raises:
            PySmiPruneError: the referenced symbol cannot be resolved.
        """
        if raw_name in _BUILTIN_SYNTAX_NAMES or raw_name == "iso":
            return

        pyname = _py_name(raw_name)
        owner = self._owner_of(scope, pyname)

        if owner == scope.name:
            self._seed(scope.name, pyname, reason)
            return

        if owner in self._active:
            self._seed(owner, pyname, f"referenced by {scope.name}: {reason}")
            # Even when the referred module is generated in this same build,
            # the referring module still has to import the symbol from it to
            # load.
            import_name = self._raw_import_name(scope, raw_name)
            self._import_owners[scope.name][import_name] = owner
            scope.required_imports[owner][import_name] = reason
            return

        if owner:
            import_name = self._raw_import_name(scope, raw_name)
            self._import_owners[scope.name][import_name] = owner
            scope.required_imports[owner][import_name] = reason
            return

        raise error.PySmiPruneError(
            f'symbol "{raw_name}" required as {reason} by module '
            f'"{scope.name}" cannot be resolved to a definition or an import',
            mibname=scope.name,
            symbol=raw_name,
        )

    # -- clause handlers ------------------------------------------------------

    def _edges_object_type(
        self, scope: _ModuleScope, pyname: str, decl: Any, reason: str
    ) -> None:
        """Keep the type, row, columns and index of an OBJECT-TYPE."""
        syntax = decl[2]
        augmentation = decl[8]
        index = decl[9]

        # A table names its row through the row's SEQUENCE type name
        # (``SEQUENCE OF ExpEntry``), which is a separate declaration from the
        # row object (``expEntry``) but one row in the symbol table. Find the
        # row object that uses that entry type and keep the object; the
        # SEQUENCE declaration follows it.
        for raw_name in _syntax_type_names(syntax):
            if raw_name in _BUILTIN_SYNTAX_NAMES:
                continue
            ref_py = _py_name(raw_name)
            if scope.is_row(ref_py):
                row_object = self._row_object_for_entry(scope, ref_py)
                if row_object:
                    ref_py = row_object
            self._reference(scope, ref_py, "table row or referenced type")

        # The resolved type chain is what a constrained value is checked
        # against, so follow it through local and foreign textual conventions.
        props = scope.table.get(pyname, {})
        resolved = props.get("syntax")
        if resolved:
            type_py, type_module = resolved[0]
            if type_py not in _BUILTIN_SYNTAX_NAMES and type_py != pyname:
                self._reference(scope, type_py, "SYNTAX type")

        if index and index[1]:
            for implied, index_raw in index[1]:
                if index_raw in _SMIV1_INDEX_TYPES:
                    continue
                self._reference(scope, index_raw, "table index object")

        if augmentation:
            self._reference(scope, augmentation, "AUGMENTS row")

        # A row kept for any reason carries the AUGMENTS relation in both
        # directions, so keep the rows that augment it.
        for augmenting_module, augmenting_row in scope.augmentees.get(pyname, ()):
            if augmenting_module == scope.name:
                self._seed(
                    scope.name, augmenting_row, f"AUGMENTS row kept with {pyname}"
                )
            else:
                self._seed(
                    augmenting_module,
                    augmenting_row,
                    f"AUGMENTS row kept with {scope.name}:{pyname}",
                )

    def _edges_type_declaration(
        self, scope: _ModuleScope, pyname: str, decl: Any, reason: str
    ) -> None:
        """Keep the type a textual convention or plain type derives from."""
        rhs = decl[2]

        if not rhs:
            return

        # A bare SEQUENCE is a row definition, not a type to derive from.
        if len(rhs) == 2 and rhs[1][0] == "SEQUENCE":
            return

        syntax = rhs[-1]
        for raw_name in _syntax_type_names(syntax):
            if raw_name in _BUILTIN_SYNTAX_NAMES:
                continue
            self._reference(scope, raw_name, "referenced type")

        # Follow the resolved parent so a chain of textual conventions keeps
        # the conventions above it.
        props = scope.table.get(pyname, {})
        resolved = props.get("syntax")
        if resolved:
            type_py, type_module = resolved[0]
            if type_py not in _BUILTIN_SYNTAX_NAMES:
                self._reference(scope, type_py, "parent type")

    def _edges_notification_type(
        self, scope: _ModuleScope, pyname: str, decl: Any, reason: str
    ) -> None:
        """Keep the objects a NOTIFICATION-TYPE carries."""
        objects = decl[2]
        names = objects[1] if isinstance(objects, tuple) else (objects or ())
        for raw_name in names:
            self._reference(scope, raw_name, "notification OBJECTS member")

    def _edges_trap_type(
        self, scope: _ModuleScope, pyname: str, decl: Any, reason: str
    ) -> None:
        """Keep the enterprise and variables of an SMIv1 TRAP-TYPE."""
        for raw_name in _oid_names(decl[2]):
            self._reference(scope, raw_name, "TRAP-TYPE enterprise")

        for raw_name in decl[3] or ():
            self._reference(scope, raw_name, "TRAP-TYPE variable")

    def _edges_object_group(
        self, scope: _ModuleScope, pyname: str, decl: Any, reason: str
    ) -> None:
        """Expand an OBJECT-GROUP through every object it names."""
        objects = decl[2]
        names = objects[1] if isinstance(objects, tuple) else (objects or ())
        for raw_name in names:
            self._reference(scope, raw_name, "OBJECT-GROUP member")

    def _edges_notification_group(
        self, scope: _ModuleScope, pyname: str, decl: Any, reason: str
    ) -> None:
        """Expand a NOTIFICATION-GROUP through every notification it names."""
        notifications = decl[2]
        names = (
            notifications[1]
            if isinstance(notifications, tuple)
            else (notifications or ())
        )
        for raw_name in names:
            self._reference(scope, raw_name, "NOTIFICATION-GROUP member")

    def _edges_module_compliance(
        self, scope: _ModuleScope, pyname: str, decl: Any, reason: str
    ) -> None:
        """Keep the groups and objects a MODULE-COMPLIANCE names."""
        modules_clause = decl[5]
        if not modules_clause:
            return

        for module_name, flat_groups, (_mandatory, refinements) in modules_clause[1]:
            target_module = module_name or scope.name

            for raw_name in flat_groups:
                self._reference_in(scope, target_module, raw_name, "compliance group")

            for refinement in refinements:
                kind = refinement[0]

                if kind == "ComplianceGroup":
                    self._reference_in(
                        scope, target_module, refinement[1], "compliance group"
                    )

                elif kind == "ComplianceObject":
                    object_name = refinement[1][1][0]
                    self._reference_in(
                        scope, target_module, object_name, "compliance object"
                    )
                    self._edges_refinement_syntax(scope, refinement[2])
                    write_syntax = refinement[3]
                    if write_syntax:
                        self._edges_refinement_syntax(scope, write_syntax[1])

    def _edges_refinement_syntax(self, scope: _ModuleScope, syntax: Any) -> None:
        """Keep types named by a compliance SYNTAX/WRITE-SYNTAX refinement."""
        for raw_name in _syntax_type_names(syntax):
            if raw_name in _BUILTIN_SYNTAX_NAMES:
                continue
            self._reference(scope, raw_name, "compliance refined syntax")

    def _reference_in(
        self,
        scope: _ModuleScope,
        target_module: str,
        raw_name: str,
        reason: str,
    ) -> None:
        """Reference a symbol a conformance clause names in an explicit module.

        A MODULE-COMPLIANCE clause names the module each group belongs to,
        which overrides the referencing module's own import resolution.
        """
        if raw_name in _BUILTIN_SYNTAX_NAMES or raw_name == "iso":
            return

        pyname = _py_name(raw_name)

        if target_module in self._active:
            target_scope = self._scopes[target_module]
            if pyname in target_scope.local_symbols:
                self._seed(target_module, pyname, f"required by {scope.name}")
                return

            raise error.PySmiPruneError(
                f'symbol "{raw_name}" named by module "{scope.name}" is not '
                f'defined in module "{target_module}"',
                mibname=scope.name,
                symbol=raw_name,
            )

        owner = target_module
        self._import_owners[scope.name][raw_name] = owner
        scope.required_imports[owner][raw_name] = reason

    def _edges_agent_capabilities(
        self, scope: _ModuleScope, pyname: str, decl: Any, reason: str
    ) -> None:
        """Keep the groups and variations an AGENT-CAPABILITIES supports."""
        modules_clause = decl[6]
        if not modules_clause:
            return

        for module_name, groups, variations in modules_clause[1]:
            target_module = module_name or scope.name

            for raw_name in groups:
                self._reference_in(
                    scope, target_module, raw_name, "capabilities INCLUDES group"
                )

            for variation in variations:
                object_name = variation[0][1][0]
                self._reference_in(
                    scope, target_module, object_name, "capabilities VARIATION"
                )
                if variation[1]:
                    self._edges_refinement_syntax(scope, variation[1])
                if variation[2]:
                    self._edges_refinement_syntax(scope, variation[2][1])

                creation = variation[4]
                if creation:
                    for cell in creation[1][1]:
                        self._reference_in(
                            scope,
                            target_module,
                            cell[1][1][0],
                            "capabilities CREATION-REQUIRES",
                        )

    _EDGE_HANDLERS: Final = {
        _OBJECT_TYPE: _edges_object_type,
        "typeDeclaration": _edges_type_declaration,
        "notificationTypeClause": _edges_notification_type,
        "trapTypeClause": _edges_trap_type,
        "objectGroupClause": _edges_object_group,
        "notificationGroupClause": _edges_notification_group,
        "moduleComplianceClause": _edges_module_compliance,
        "agentCapabilitiesClause": _edges_agent_capabilities,
    }


def _rewrite_module(
    scope: _ModuleScope,
    kept: dict[str, str],
    imports_needed: dict[str, str],
) -> tuple[Any, dict[str, Any]]:
    """Reduce one module's AST and symbol table to its kept closure.

    Args:
        scope: parsed facts about the module
        kept: kept Python-safe symbol names with reasons
        imports_needed: raw foreign symbol -> owner module the closure needs

    Returns:
        A new module AST tuple and a new symbol table restricted to the kept
        symbols.

    Raises:
        PySmiPruneError: an AUGMENTS, INDEX or similar clause names a local
            symbol the closure did not keep -- an inconsistency that would
            make the rendered output unloadable.
    """
    kept_with_fakes = set(kept)

    # The columns the generator synthesizes for bare-type SMIv1 indexes ride
    # alongside the row, and are keyed in the symbol table rather than the
    # AST. Carry the ones belonging to kept rows.
    for pyname in list(kept):
        props = scope.table.get(pyname, {})
        if props.get("type") != "ObjectType":
            continue

        decl = scope.declaration(pyname)
        index = decl[9] if decl and decl[0] == _OBJECT_TYPE else None
        if not index or not index[1]:
            continue

        if any(idx[1] in _SMIV1_INDEX_TYPES for idx in index[1]):
            row_oid = props.get("oid", ())
            for key, value in scope.table.items():
                if _is_fake_column(key) and isinstance(value, dict):
                    fake_oid = value.get("oid", ())
                    if fake_oid[:-1] == row_oid:
                        kept_with_fakes.add(key)

    new_declarations = [
        decl
        for decl in scope.ast[3] or []
        if decl and _py_name(decl[1]) in kept_with_fakes
    ]

    order = [
        name
        for name in scope.table.get("_symtable_order", ())
        if name in kept_with_fakes
    ]

    new_table: dict[str, Any] = {
        name: scope.table[name] for name in order
    }
    new_table["_symtable_order"] = order
    new_table["_symtable_cols"] = [
        name
        for name in scope.table.get("_symtable_cols", ())
        if name in kept_with_fakes
    ]
    new_table["_symtable_rows"] = [
        name
        for name in scope.table.get("_symtable_rows", ())
        if name in kept
    ]
    new_table[REPAIRED_IMPORTS_KEY] = {
        symbol: module
        for symbol, module in scope.table.get(REPAIRED_IMPORTS_KEY, {}).items()
        if symbol in imports_needed
    }

    new_imports = _rewrite_imports(scope, imports_needed)
    new_ast = (scope.ast[0], scope.ast[1], new_imports, new_declarations)
    return new_ast, new_table


def _rewrite_imports(
    scope: _ModuleScope,
    imports_needed: dict[str, str],
) -> dict[str, list[str]]:
    """Restrict the module's IMPORTS line to what kept symbols load.

    Three kinds of entry survive: an originally imported construct a kept
    declaration uses (``OBJECT-TYPE`` and the other macros), a foreign symbol
    a kept declaration references, and a repaired import a kept symbol needs.
    Everything on the original line is dropped; the generator's constant
    imports put the base classes back by itself.

    Raises:
        PySmiPruneError: a foreign symbol the closure needs was neither named
            on the IMPORTS line nor repaired onto it, so the module could not
            load it.
    """
    # The classes a kept clause's macro expands to (ObjectGroup, ...). The
    # generator's constant imports already supply the common ones
    # (OBJECT-TYPE's MibScalar family and friends), so only an expansion the
    # generator does not itself import is kept on the rewritten line. The
    # expanded name is what must appear there: the generator applies
    # sym_trans to the IMPORTS line itself, so naming the raw macro token a
    # second time would expand it twice and desynchronise the assignment.
    constant_symbols = _constant_import_symbols()
    kept_construct_symbols: set[tuple[str, str]] = set()
    for construct in scope.required_constructs:
        raw_source = scope._raw_import_tokens.get(construct)
        if raw_source is None:
            # The module named the construct but did not import it; either a
            # repair supplied it (handled through the foreign-reference path)
            # or the generator's constant imports do.
            continue

        owner_module = raw_source[0]
        for expanded in SymtableCodeGen.symsTable.get(construct, (construct,)):
            if (owner_module, expanded) not in constant_symbols:
                kept_construct_symbols.add((owner_module, expanded))

    wanted: dict[str, set[str]] = defaultdict(set)

    for raw_name, owner in imports_needed.items():
        source = scope._import_sources.get(_py_name(raw_name))
        if source is None:
            raise error.PySmiPruneError(
                f'module "{scope.name}" keeps a reference to "{raw_name}" from '
                f'"{owner}" without an IMPORTS entry that supplies it',
                mibname=scope.name,
                symbol=raw_name,
            )

        source_module, source_name = source
        if source_module != owner:
            raise error.PySmiPruneError(
                f'module "{scope.name}" resolves "{raw_name}" to "{owner}" but '
                f'its IMPORTS line supplies it from "{source_module}"',
                mibname=scope.name,
                symbol=raw_name,
            )

        wanted[owner].add(source_name)

    for source_module, source_name in kept_construct_symbols:
        wanted[source_module].add(source_name)

    return {
        module: sorted(symbols)
        for module, symbols in sorted(wanted.items())
        if symbols
    }


def prune_modules(
    asts: dict[str, Any],
    tables: dict[str, dict[str, Any]],
    targets: dict[str, list[str]],
    generated_modules: frozenset[str] | None = None,
) -> tuple[
    dict[str, Any],
    dict[str, dict[str, Any]],
    dict[str, dict[str, str]],
    dict[str, dict[str, str]],
]:
    """Restrict parsed modules to the symbol-level closure of *targets*.

    Args:
        asts: parsed module ASTs keyed by canonical module name
        tables: symbol tables built by
            :py:class:`~pysmi.codegen.symtable.SymtableCodeGen`, keyed the
            same way
        targets: raw target symbol names per module. A module that appears as
            a target but defines none of the named symbols fails the run.
        generated_modules: the modules that will actually be rendered. A
            symbol in any other parsed module -- a base MIB reached through a
            stub searcher, or a dependency excluded with ``noDeps`` -- stays an
            import rather than joining the closure, so the reduced output does
            not silently re-emit a module the build does not generate.

    Returns:
        A tuple of:

        1. the rewritten ASTs for every module that ended up keeping symbols;
        2. the rewritten symbol tables, keyed the same way;
        3. per module, every symbol kept and the human-readable reason it was
           kept -- the targets themselves and every symbol pulled in for them;
        4. per module, every import kept and the reason it is required.

    Raises:
        PySmiPruneError: a target symbol is missing, or a dependency the
            closure needs cannot be resolved to a kept symbol or an import.
    """
    generated = (
        frozenset(generated_modules)
        if generated_modules is not None
        else frozenset(asts)
    )
    active = frozenset(asts) & generated
    pruner = SymbolPruner(asts, tables, active)
    kept_reasons, import_owners = pruner.run(targets)

    new_asts: dict[str, Any] = {}
    new_tables: dict[str, dict[str, Any]] = {}
    import_reasons: dict[str, dict[str, str]] = {}

    for module, scope in pruner._scopes.items():
        if not kept_reasons.get(module):
            continue

        imports_needed = import_owners.get(module, {})
        new_ast, new_table = _rewrite_module(
            scope, kept_reasons[module], imports_needed
        )
        new_asts[module] = new_ast
        new_tables[module] = new_table

        import_reasons[module] = {
            raw_name: scope.required_imports.get(owner, {}).get(raw_name)
            or (
                # An OBJECT-TYPE macro and the other SMI constructs are put
                # back by the generator's constant imports; record why the
                # entry is still on the rewritten IMPORTS line.
                "SMI construct used by a kept declaration"
            )
            for raw_name, owner in imports_needed.items()
        }

    return new_asts, new_tables, kept_reasons, import_reasons
