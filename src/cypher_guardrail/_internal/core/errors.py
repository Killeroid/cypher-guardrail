"""The codes cypher-guardrail reports, and the error it raises when FalkorDB rejects a query.

Kept apart from ``backend`` so the I/O layer (``core/io.py``) and the query logic (``core/queries.py``) can use them
without importing the backend, which imports both.
"""

from typing import Final

# The codes cypher-guardrail reports in its metadata. FalkorDB has no notifications of its own, so cypher-guardrail produces these itself.
UNKNOWN_LABEL: Final = "cypher_guardrail.Warning.UnknownLabel"
UNKNOWN_REL_TYPE: Final = "cypher_guardrail.Warning.UnknownRelationshipType"
UNKNOWN_PROPERTY: Final = "cypher_guardrail.Warning.UnknownPropertyKey"
PARAMETER_NOT_PROVIDED: Final = "cypher_guardrail.Warning.ParameterNotProvided"
UNKNOWN_INDEX: Final = "cypher_guardrail.Warning.UnknownIndex"
# Errors raised by FalkorDB itself
SYNTAX_ERROR: Final = "FalkorDB.ClientError.Statement.SyntaxError"
EXECUTION_FAILED: Final = "FalkorDB.DatabaseError.Statement.ExecutionFailed"
# Reported by cypher-guardrail: FalkorDB only catches a constant division by zero (see ``to_cypher_error``)
ARITHMETIC_ERROR: Final = "cypher_guardrail.Error.ArithmeticError"
# The query can modify the graph: a warning, or an error when the validator was told to allow only reads
WRITE_QUERY_WARNING: Final = "cypher_guardrail.Warning.WriteQuery"
WRITE_QUERY_ERROR: Final = "cypher_guardrail.Error.WriteQuery"
# Not reported: the code of the CypherError for "GRAPH.RO_QUERY refuses this query because it can write"
NOT_READ_ONLY: Final = "cypher_guardrail.Error.NotReadOnly"
# More codes cypher-guardrail reports.
CONFLICTING_VARIABLES: Final = "cypher_guardrail.Error.ConflictingVariables"
INVALID_PROPERTY_ACCESS: Final = "cypher_guardrail.Warning.InvalidPropertyAccessByLabel"
PROPERTY_TYPE_MISMATCH: Final = "cypher_guardrail.Warning.PropertyTypeMismatch"
UNKNOWN_PATH: Final = "cypher_guardrail.Warning.UnknownPath"
VARIABLE_LENGTH_PATH: Final = "cypher_guardrail.Warning.VariableLengthPath"
WRONG_DIRECTION_PATH: Final = "cypher_guardrail.Warning.WrongDirectionPath"
WRONG_DIRECTION_VARIABLE_LENGTH_PATH: Final = (
    "cypher_guardrail.Warning.WrongDirectionVariableLengthPath"
)


class CypherError(Exception):
    """A query was rejected by the database (syntax error, execution failure, ...).

    Attributes:
        code (str): Error code, e.g. ``FalkorDB.ClientError.Statement.SyntaxError``.
        message (str): Human readable description from the database.
    """

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


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
    return CypherError(EXECUTION_FAILED, message)


def is_missing_graph(error: CypherError) -> bool:
    """Whether FalkorDB refused a query because the graph does not exist: it creates a graph with its first write, so
    until then it answers "empty key". Such a graph has no schema, no indexes and no data, which is no failure."""
    return "empty key" in error.message
