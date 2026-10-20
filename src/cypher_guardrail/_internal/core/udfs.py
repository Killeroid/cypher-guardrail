"""The user-defined functions (UDFs) of a FalkorDB instance, as ``GRAPH.UDF LIST`` reports them.

UDFs belong to the instance, not to a graph, and FalkorDB tells their names only: no signatures. The JavaScript
source (``WITHCODE``) is never asked for, so it is never kept.
"""

from cypher_guardrail._internal.core.errors import CypherError

# What a server says when it has no UDFs to list: it does not know the command (an older FalkorDB), or the user may not
# run it. Either way there is no catalog to take, which is not an error of the capture.
_NO_CATALOG = ("unknown command", "noperm")

Libraries = tuple[tuple[str, tuple[str, ...]], ...]


def is_unsupported(error: CypherError) -> bool:
    """Whether ``error`` says that the server does not offer ``GRAPH.UDF LIST`` (to this user)."""
    message = error.message.lower()
    return any(text in message for text in _NO_CATALOG)


def _text(value: object) -> str | None:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value if isinstance(value, str) else None


def _fields(library: object) -> dict[str, object]:
    """The fields of one library of the reply: a map (RESP3, FalkorDBLite) or a flat list of keys and values (RESP2)."""
    if isinstance(library, list | tuple):
        library = dict(zip(library[0::2], library[1::2], strict=False))
    if not isinstance(library, dict):
        return {}
    return {key: value for raw, value in library.items() if (key := _text(raw))}


def parse_list(reply: object) -> Libraries:
    """The libraries of a ``GRAPH.UDF LIST`` reply, as ``((library, (function, ...)), ...)`` sorted by name.

    Tolerant of both protocols and of missing or extra fields: what is not recognised is left out.
    """
    found: dict[str, set[str]] = {}
    for entry in reply if isinstance(reply, list | tuple) else ():
        fields = _fields(entry)
        name = _text(fields.get("library_name"))
        functions = fields.get("functions")
        if name is None or not isinstance(functions, list | tuple):
            continue
        found.setdefault(name, set()).update(
            text for raw in functions if (text := _text(raw)) is not None
        )
    return tuple((name, tuple(sorted(found[name]))) for name in sorted(found))
