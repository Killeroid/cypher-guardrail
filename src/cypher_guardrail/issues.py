"""The issues the validators report, as typed dictionaries, and their codes.

Every item of the ``metadata`` that ``validate()`` returns is a dict with a ``code`` and a ``description`` (the
text for a person), and, for most codes, fields that say what the issue is about, so that nobody has to parse
the description. Which fields a code has is what the ``TypedDict`` of that code says; ``Issue`` is the union of
all of them, so a type checker can tell them apart by ``code``::

    for issue in metadata:
        if issue["code"] == UNKNOWN_LABEL:
            print(issue["label"])

The fields hold plain strings, lists and dicts, so an issue can be turned into JSON as it is. The three codes
for errors that the database itself raised (``SYNTAX_ERROR``, ``EXECUTION_FAILED`` and ``ARITHMETIC_ERROR``)
have no fields besides the two: the message is all there is, and so it is for the two codes that say a query can
write (``WRITE_QUERY_WARNING`` and ``WRITE_QUERY_ERROR``).
"""

from typing import Literal, TypedDict

from cypher_guardrail._internal.core.errors import (  # noqa: F401
    ARITHMETIC_ERROR,
    CONFLICTING_VARIABLES,
    EXECUTION_FAILED,
    INVALID_PROPERTY_ACCESS,
    PARAMETER_NOT_PROVIDED,
    PROPERTY_TYPE_MISMATCH,
    SYNTAX_ERROR,
    UNKNOWN_INDEX,
    UNKNOWN_LABEL,
    UNKNOWN_PATH,
    UNKNOWN_PROPERTY,
    UNKNOWN_REL_TYPE,
    VARIABLE_LENGTH_PATH,
    WRITE_QUERY_ERROR,
    WRITE_QUERY_WARNING,
    WRONG_DIRECTION_PATH,
    WRONG_DIRECTION_VARIABLE_LENGTH_PATH,
)


class UnknownLabelIssue(TypedDict):
    """A node label of the query that the graph does not have."""

    code: Literal["cypher_guardrail.Warning.UnknownLabel"]
    description: str
    label: str


class UnknownRelationshipTypeIssue(TypedDict):
    """A relationship type of the query that the graph does not have."""

    code: Literal["cypher_guardrail.Warning.UnknownRelationshipType"]
    description: str
    relationship_type: str


class UnknownPropertyKeyIssue(TypedDict):
    """A property name of the query that exists on no node or relationship of the graph."""

    code: Literal["cypher_guardrail.Warning.UnknownPropertyKey"]
    description: str
    property: str


class InvalidPropertyAccessIssue(TypedDict):
    """Properties the query reads from a label that does not have them.

    For alternatives (``Label1|Label2``) ``label`` is the first one.
    """

    code: Literal["cypher_guardrail.Warning.InvalidPropertyAccessByLabel"]
    description: str
    label: str
    properties: list[str]


class PropertyTypeMismatchIssue(TypedDict):
    """The query compares a property with a literal of a type that none of its values has (``check_types=True``).

    FalkorDB does not report it: a comparison of a property with a value of another type (``year = '2020'`` for an
    integer ``year``) finds nothing. ``expected_types`` are the types of the values the property has (``typeof``),
    several when the data is mixed; ``value`` is the literal as written (a string without its quotes) and
    ``value_type`` its type: ``String``, ``Integer``, ``Float`` or ``Boolean``.
    """

    code: Literal["cypher_guardrail.Warning.PropertyTypeMismatch"]
    description: str
    label: str
    property: str
    expected_types: list[str]
    value: str
    value_type: str


class ParameterNotProvidedIssue(TypedDict):
    """``$parameters`` of the query that no value was supplied for."""

    code: Literal["cypher_guardrail.Warning.ParameterNotProvided"]
    description: str
    parameters: list[str]


class UnknownIndexIssue(TypedDict):
    """A full-text or vector query procedure is called but the graph has no index for it.

    ``label`` is the node label, or the relationship type, of the missing index. ``attribute`` is the indexed
    attribute of a vector query, and None for a full-text query.
    """

    code: Literal["cypher_guardrail.Warning.UnknownIndex"]
    description: str
    index: Literal["fulltext", "vector"]
    on: Literal["nodes", "relationships"]
    label: str
    attribute: str | None


class ConflictingVariablesIssue(TypedDict):
    """Variables that are given labels that do not occur together on a node of the graph.

    ``variables`` maps each such variable to its labels, in the order of the query.
    """

    code: Literal["cypher_guardrail.Error.ConflictingVariables"]
    description: str
    variables: dict[str, list[str]]


class UnknownPathIssue(TypedDict):
    """A path of the query that the graph does not have, and no simple change makes it right.

    ``path`` is the path as extracted (labels and types only), ``original_path`` the path as written in the query.
    """

    code: Literal["cypher_guardrail.Warning.UnknownPath"]
    description: str
    path: str
    original_path: str


class VariableLengthPathIssue(TypedDict):
    """A path that the graph does not have, but does have once its ``*X..Y`` is replaced by ``*``.

    ``suggested_path`` is the original path with that change.
    """

    code: Literal["cypher_guardrail.Warning.VariableLengthPath"]
    description: str
    path: str
    original_path: str
    suggested_path: str


class WrongDirectionPathIssue(TypedDict):
    """A path that the graph has the other way round. ``suggested_path`` is the original path with its arrow flipped."""

    code: Literal["cypher_guardrail.Warning.WrongDirectionPath"]
    description: str
    path: str
    original_path: str
    suggested_path: str


class WrongDirectionVariableLengthPathIssue(TypedDict):
    """A path that the graph has the other way round and with any length. ``suggested_path`` is that path."""

    code: Literal["cypher_guardrail.Warning.WrongDirectionVariableLengthPath"]
    description: str
    path: str
    original_path: str
    suggested_path: str


class WriteQueryWarningIssue(TypedDict):
    """The query can modify the graph (it creates, changes or deletes data, or an index), and the validator was told to allow that."""

    code: Literal["cypher_guardrail.Warning.WriteQuery"]
    description: str


class WriteQueryErrorIssue(TypedDict):
    """The query can modify the graph, and the validator allows only reads (``read_only=True``, the default)."""

    code: Literal["cypher_guardrail.Error.WriteQuery"]
    description: str


class DatabaseErrorIssue(TypedDict):
    """The database rejected the query, or it divides by zero. ``description`` is the message."""

    code: Literal[
        "FalkorDB.ClientError.Statement.SyntaxError",
        "FalkorDB.DatabaseError.Statement.ExecutionFailed",
        "cypher_guardrail.Error.ArithmeticError",
    ]
    description: str


Issue = (
    UnknownLabelIssue
    | UnknownRelationshipTypeIssue
    | UnknownPropertyKeyIssue
    | InvalidPropertyAccessIssue
    | PropertyTypeMismatchIssue
    | ParameterNotProvidedIssue
    | UnknownIndexIssue
    | ConflictingVariablesIssue
    | UnknownPathIssue
    | VariableLengthPathIssue
    | WrongDirectionPathIssue
    | WrongDirectionVariableLengthPathIssue
    | WriteQueryWarningIssue
    | WriteQueryErrorIssue
    | DatabaseErrorIssue
)
