"""What cypher-guardrail asks FalkorDB, written as generators that yield requests (see ``core/io.py``).

Every function takes the resolved graph name and ``yield from`` is how one calls another. They are run by
``run_sync`` (``FalkorDBBackend``) or ``run_async``, so the logic here exists once for both APIs. Inside a
snapshot the driver keeps the answer of each request, so the lookups below can be repeated freely: they
cost one round-trip each per snapshot.

See the ``backend`` module docstring for the FalkorDB peculiarities these functions compensate for.
"""

from collections.abc import Generator, Iterable
from typing import Any

import pandas as pd
import regex as re

from cypher_guardrail._internal.core import builders as _issues
from cypher_guardrail._internal.core.errors import NOT_READ_ONLY, CypherError
from cypher_guardrail._internal.core.io import (
    Concurrently,
    Explain,
    Lookup,
    Request,
    RoQuery,
)
from cypher_guardrail.issues import (
    ParameterNotProvidedIssue,
    UnknownLabelIssue,
    UnknownRelationshipTypeIssue,
    WriteQueryErrorIssue,
    WriteQueryWarningIssue,
)
from cypher_guardrail.patterns import strip_literals_and_comments, unquote_name

_PARAMETER = re.compile(r"\$(\w+)")
# The start of a `CYPHER a=1 b='x' ...` prefix, and one `name=` in it
_CYPHER_PREFIX = re.compile(r"\s*CYPHER\b", re.IGNORECASE)
_PARAMETER_NAME = re.compile(r"\s*(\w+)\s*=\s*")
# (var:Label1:Label2 ...) -> ':Label1:Label2'
_NODE_LABELS = re.compile(r"\(\s*(?:[A-Za-z_]\w*)?((?:\s*:\s*(?:\w+|`[^`]*`))+)")
# [var:TYPE1|TYPE2*1..3 {props}] -> 'TYPE1|TYPE2'
_REL_TYPES = re.compile(
    r"\[\s*(?:[A-Za-z_]\w*)?\s*:\s*((?:[\w|:\s]|`[^`]*`)+?)\s*(?:\*[^\]{]*)?(?:\{[^}]*\})?\s*\]"
)
# a name in a list of them: a word, or what is between back-quotes (without them)
_NAME = re.compile(r"`([^`]*)`|\w+")


def _names(group: str) -> list[str]:
    return [
        m.group(1) if m.group(1) is not None else m.group()
        for m in _NAME.finditer(group)
    ]


def provided_parameters(query: str) -> set[str]:
    """The names of the parameters that a ``CYPHER a=1 b='x' ...`` prefix gives the query."""
    prefix = _CYPHER_PREFIX.match(query)
    if not prefix:
        return set()
    names = set()
    position = prefix.end()
    while name := _PARAMETER_NAME.match(query, position):
        names.add(name.group(1))
        position = _end_of_value(query, name.end())
    return names


def _end_of_value(text: str, start: int) -> int:
    """Where the value of a ``CYPHER`` parameter that starts at ``start`` ends: a string, a list or a map, or a word."""
    depth = 0
    quote = None
    position = start
    while position < len(text):
        char = text[position]
        if quote:
            if char == "\\":
                position += 1  # an escaped character, which may be the quote
            elif char == quote:
                quote = None
                if not depth:
                    return position + 1
        elif char in "'\"":
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if not depth:
                return position + 1
        elif char.isspace() and not depth:
            return position
        position += 1
    return position


def index_exists(
    graph: str, kind: str, on: str, label: str, attribute: str | None = None
) -> Generator[Request, Any, bool]:
    """Check whether the graph has an index that a full-text or vector query needs.

    Args:
        kind (str): 'fulltext' or 'vector'.
        on (str): 'nodes' or 'relationships'.
        label (str): The node label, or the relationship type, of the index.
        attribute (str | None): The attribute the index must cover. None: any attribute.
    """
    return (yield Lookup("index_exists", graph, (kind, on, label, attribute)))


def execute(graph: str, query: str) -> Generator[Request, Any, list[dict]]:
    """Run a read query and return one dict per row, keyed by column name.

    Raises:
        CypherError: If the database rejects the query.
    """
    result = yield RoQuery(graph, query)
    names = [column[1] for column in result.header]
    return [dict(zip(names, row, strict=False)) for row in result.result_set]


def parameter_names(parameters: Iterable[str] | None) -> frozenset[str]:
    """The names in ``parameters``: None, a collection of names, or a dict of the parameters a client is given."""
    if isinstance(parameters, str | bytes):
        raise TypeError(
            "parameters must be a mapping or a collection of parameter names, not a string"
        )
    return frozenset(parameters or ())


def explain(
    graph: str,
    query: str,
    read_only: bool | None = None,
    parameters: Iterable[str] | None = None,
) -> Generator[
    Request,
    Any,
    list[ParameterNotProvidedIssue | WriteQueryErrorIssue | WriteQueryWarningIssue],
]:
    """Check a query without executing it.

    Args:
        read_only: None does not look for writes. True or False does, and reports a query that can modify the
            graph as an error (True) or as a warning (False). FalkorDB decides what can write: it refuses such
            a query in ``GRAPH.RO_QUERY``, and ``EXPLAIN`` there still executes nothing.
        parameters: The names of the parameters the caller will send with the query (a dict of them will do), which
            are not reported as missing. The ones a ``CYPHER`` header gives are not either.

    Returns:
        The problems found: missing parameters, and a write when ``read_only`` is not None.

    Raises:
        TypeError: If ``parameters`` is a string.
        CypherError: If the database rejects the query.
    """
    given = parameter_names(parameters)
    writes = False
    if read_only is None:
        yield Explain(graph, query)
    else:
        try:
            yield Explain(graph, query, read_only=True)
        except CypherError as e:
            if e.code == NOT_READ_ONLY:
                writes = True
            else:
                # Not an answer about writes (a syntax error, or something this form of the request gets
                # wrong, such as the parameters of a ``CYPHER a=1 ...`` query): the plain EXPLAIN gives the
                # database's own answer, and raises the error if there is one
                yield Explain(graph, query)
    issues: list[
        ParameterNotProvidedIssue | WriteQueryErrorIssue | WriteQueryWarningIssue
    ] = []
    # FalkorDB only reports a missing parameter when the query runs, so detect it from the text.
    # (The caller gives some of them, and so does a ``CYPHER`` header.)
    provided = given | provided_parameters(query)
    absent = [
        name
        for name in dict.fromkeys(
            _PARAMETER.findall(strip_literals_and_comments(query))
        )
        if name not in provided
    ]
    if absent:
        issues.append(_issues.parameters_not_provided(absent))
    if writes and read_only is not None:
        issues.append(_issues.write_query(read_only))
    return issues


def query_to_df(graph: str, query: str) -> Generator[Request, Any, pd.DataFrame]:
    """Run a read query and return the rows as a DataFrame (columns = returned names)."""
    result = yield RoQuery(graph, query)
    return pd.DataFrame(
        result.result_set, columns=[column[1] for column in result.header]
    )


def _missing_names(
    graph: str, kind: str, names: list[str]
) -> Generator[Request, Any, tuple[str, ...]]:
    """The ``names`` (labels, relationship types or property keys) that the graph does not know."""
    return (yield Lookup("missing_names", graph, (kind, tuple(names))))


def unknown_schema_notifications(
    graph: str, pattern: str
) -> Generator[Request, Any, list[UnknownLabelIssue | UnknownRelationshipTypeIssue]]:
    """Unknown label / relationship type warnings for the labels used in ``pattern``."""
    pattern = strip_literals_and_comments(pattern)
    labels = {
        label for group in _NODE_LABELS.findall(pattern) for label in _names(group)
    }
    rel_types = {t for group in _REL_TYPES.findall(pattern) for t in _names(group)}
    # Both are asked whenever the pattern has labels and types, so they are asked together
    lookups = []
    if labels:
        lookups.append(_missing_names(graph, "labels", sorted(labels)))
    if rel_types:
        lookups.append(_missing_names(graph, "relationship_types", sorted(rel_types)))
    answers = iter((yield Concurrently(tuple(lookups))))
    notifications: list[UnknownLabelIssue | UnknownRelationshipTypeIssue] = []
    if labels:
        notifications += [_issues.unknown_label(label) for label in next(answers)]
    if rel_types:
        notifications += [
            _issues.unknown_relationship_type(rel_type) for rel_type in next(answers)
        ]
    return notifications


def path_exists(graph: str, path: str) -> Generator[Request, Any, tuple[bool, list]]:
    """Check whether a pattern (node, relationship or path) matches anything in the graph.

    Returns:
        tuple[bool, list[dict]]: Whether it exists, and warnings about unknown labels/types.
    """
    exists = yield Lookup("path_exists", graph, (path,))
    notifications = yield from unknown_schema_notifications(graph, path)
    return exists, notifications


def label_kind(graph: str, label: str) -> Generator[Request, Any, str]:
    """Return 'node' if ``label`` is a node label, 'relationship' if it is a relationship type, else 'none'.

    A schema that cannot be read counts as 'none'.
    """
    return (yield Lookup("label_kind", graph, (label,)))


def property_exists(
    graph: str, label: str, kind: str, prop: str
) -> Generator[Request, Any, tuple[bool, list]]:
    """Check whether any node/relationship with ``label`` has property ``prop``.

    Args:
        kind (str): 'node' or 'relationship'.

    Returns:
        tuple[bool, list[dict]]: Whether it exists, and a warning if the property key is unknown.
    """
    # The property keys of the graph do not depend on whether this label has the property, so they are
    # asked together with it
    exists, unknown = yield Concurrently(
        (
            _property_exists(graph, label, kind, prop),
            _missing_names(graph, "property_keys", [unquote_name(prop)]),
        )
    )
    notifications = [_issues.unknown_property(name) for name in unknown]
    return exists, notifications


def _property_exists(
    graph: str, label: str, kind: str, prop: str
) -> Generator[Request, Any, bool]:
    return (yield Lookup("property_exists", graph, (label, kind, prop)))


def property_types(
    graph: str, label: str, kind: str, prop: str, literal_type: str
) -> Generator[Request, Any, tuple[str, ...] | None]:
    """The types of the values of property ``prop`` of ``label`` (``kind`` 'node' or 'relationship'): the names
    ``typeof`` gives, sorted, several when the data is mixed.

    ``literal_type`` is the type of the value the property is compared with. The answer does not depend on it: a
    snapshot uses it to tell a type it has seen from one it has not, which the database is then asked to confirm.

    Returns:
        ``()`` when no value is known (the property has none, or the database cannot say), None when a snapshot
        was captured without the types.
    """
    return (yield Lookup("property_types", graph, (label, kind, prop, literal_type)))
