"""The codes cypher-guardrail reports, and the error it raises when FalkorDB rejects a query.

Kept apart from ``backend`` so the I/O layer (``core/io.py``) and the query logic (``core/queries.py``) can use them
without importing the backend, which imports both.
"""

import re
from typing import Final

from cypher_guardrail.issues import (
    ARITHMETIC_ERROR,
    EXECUTION_FAILED,
    INVALID_LIMIT_ERROR,
    SYNTAX_ERROR,
    UNBOUND_VARIABLE_ERROR,
    UNKNOWN_FUNCTION_ERROR,
)

# Not reported: the code of the CypherError for "GRAPH.RO_QUERY refuses this query because it can write"
NOT_READ_ONLY: Final = "cypher_guardrail.Error.NotReadOnly"


class CypherError(Exception):
    """A query was rejected by the database (syntax error, execution failure, ...).

    Attributes:
        code (str): Error code, e.g. ``FalkorDB.ClientError.Statement.SyntaxError``.
        message (str): Human readable description from the database.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class GraphNotFoundError(CypherError, ValueError):
    """The graph that was asked about does not exist: FalkorDB creates a graph with its first write, and answers "empty key"
    to everything until then. It is a mistake of the caller (usually the name of the graph), not an answer about the query.

    It is a :class:`CypherError` too, because ``is_write`` raised that for such a graph before.

    Attributes:
        graph (str): The name of the graph.
    """

    def __init__(self, graph: str) -> None:
        super().__init__(
            EXECUTION_FAILED,
            f"The graph {graph!r} does not exist (FalkorDB creates a graph with its first write): check its name.",
        )
        self.graph = graph


def missing_graph_key(graph):
    """The key under which a run remembers that ``graph`` does not exist (see ``queries.graph_checked``)."""
    return ("graph_missing", graph)


def to_cypher_error(error):
    """Turn a redis ``ResponseError`` into a :class:`CypherError` with one of cypher-guardrail's codes.

    FalkorDB reports every failure as a plain error message, so the code is chosen from the
    wording: syntax errors, a constant division by zero, a query that is not read-only, or else a failed
    execution.
    """
    message = str(error)
    # FalkorDB rejects a constant `/ 0` while planning; for anything it does not catch, cypher-guardrail reports
    # an explicit division by zero itself (see SyntaxValidator). Use the same code and text for both.
    if message.strip() == "Division by zero":
        return CypherError(ARITHMETIC_ERROR, "Division by zero.")
    if "only on read-only queries" in message:
        return CypherError(NOT_READ_ONLY, message)
    if (
        "Invalid input" in message
        or "errMsg:" in message
        or message.startswith("Error: empty query")
    ):
        return CypherError(SYNTAX_ERROR, message)
    return CypherError(_code_of_execution_failure(message), message)


# How FalkorDB words the failures that have a code of their own (the first word of each is the field the issue has)
_WORDED = (
    (UNBOUND_VARIABLE_ERROR, re.compile(r"^'([^']*)' not defined")),
    (UNKNOWN_FUNCTION_ERROR, re.compile(r"^Unknown function '([^']*)'")),
    (INVALID_LIMIT_ERROR, re.compile(r"^(LIMIT|SKIP) specified value of invalid type")),
)


def _code_of_execution_failure(message: str) -> str:
    """The code of a failure that is not a syntax error: its own when FalkorDB words it in one of the known ways."""
    return next(
        (code for code, pattern in _WORDED if pattern.search(message.strip())),
        EXECUTION_FAILED,
    )


def failure_field(code: str, message: str) -> str | None:
    """The field that an issue of ``code`` has, read from ``message``: the variable, the function or the clause."""
    for known, pattern in _WORDED:
        if known == code and (match := pattern.search(message.strip())):
            return match[1]
    return None


# How FalkorDB words a query that was stopped for lack of time or memory, or refused because the server is busy
_RESOURCE_FAILURE = re.compile(
    r"timed out|timeout|mem(?:ory)? consumption|out of memory|max.{0,20}queued|too many",
    re.IGNORECASE,
)


def is_resource_failure(error: CypherError) -> bool:
    """Whether the database could not answer for lack of time or memory (or because it is busy), which says nothing
    about the query or the graph. An existence check that fails so must not be taken for "it does not exist"; one that
    fails because the pattern is wrong (a syntax error, an alias used twice) does say that."""
    return bool(_RESOURCE_FAILURE.search(error.message))


def is_missing_graph(error: CypherError) -> bool:
    """Whether FalkorDB refused a query because the graph does not exist: it creates a graph with its first write, so
    until then it answers "empty key". Such a graph has no schema, no indexes and no data, which is no failure."""
    return "empty key" in error.message
