"""Version namespaces for incompatible MIB modules sharing a name."""

import re
from typing import Final

from pysmi import error

NAMESPACE_SEPARATOR: Final = "@"
_NAMESPACE_RE: Final = re.compile(r"^[A-Za-z0-9_.-]+$")


def namespaced_mib_name(module_name: str, namespace: str) -> str:
    """Return the generated MIB binding name for *module_name* in *namespace*."""
    validate_namespace(namespace)
    if not module_name:
        raise error.PySmiError("MIB module name is empty")

    return f"{namespace}{NAMESPACE_SEPARATOR}{module_name}"


def validate_namespace(namespace: str) -> str:
    """Validate a caller-supplied version namespace."""
    if not namespace or not _NAMESPACE_RE.fullmatch(namespace):
        raise error.PySmiError(
            f"invalid MIB namespace {namespace!r}: use letters, digits, "
            "underscore, dot or hyphen only"
        )
    return namespace
