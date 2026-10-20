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
write (``WRITE_QUERY_WARNING`` and ``WRITE_QUERY_ERROR``). Three failures that FalkorDB words the same way every time
have a code and a field of their own: ``UNBOUND_VARIABLE_ERROR`` (``variable``), ``UNKNOWN_FUNCTION_ERROR`` (``name``)
and ``INVALID_LIMIT_ERROR`` (``clause``).
"""

from typing import Final, Literal, TypedDict

# The codes cypher-guardrail reports in its metadata. FalkorDB has no notifications of its own, so cypher-guardrail produces these itself.
UNKNOWN_LABEL: Final = "cypher_guardrail.Warning.UnknownLabel"
UNKNOWN_REL_TYPE: Final = "cypher_guardrail.Warning.UnknownRelationshipType"
UNKNOWN_PROPERTY: Final = "cypher_guardrail.Warning.UnknownPropertyKey"
PARAMETER_NOT_PROVIDED: Final = "cypher_guardrail.Warning.ParameterNotProvided"
UNKNOWN_INDEX: Final = "cypher_guardrail.Warning.UnknownIndex"
UNKNOWN_FUNCTION: Final = "cypher_guardrail.Warning.UnknownFunction"
# Valid queries that may be expensive (``SyntaxValidator(check_performance=True)``)
CARTESIAN_PRODUCT: Final = "cypher_guardrail.Warning.CartesianProduct"
UNLABELED_NODE_SCAN: Final = "cypher_guardrail.Warning.UnlabeledNodeScan"
UNBOUNDED_PATH: Final = "cypher_guardrail.Warning.UnboundedPath"
# Errors raised by FalkorDB itself
SYNTAX_ERROR: Final = "FalkorDB.ClientError.Statement.SyntaxError"
EXECUTION_FAILED: Final = "FalkorDB.DatabaseError.Statement.ExecutionFailed"
# Failures that the database reports in a few words, and that cypher-guardrail gives a code and fields of their own
UNBOUND_VARIABLE_ERROR: Final = "cypher_guardrail.Error.UnboundVariable"
UNKNOWN_FUNCTION_ERROR: Final = "cypher_guardrail.Error.UnknownFunction"
INVALID_LIMIT_ERROR: Final = "cypher_guardrail.Error.InvalidLimit"
# Reported by cypher-guardrail: FalkorDB only catches a constant division by zero (see ``to_cypher_error``)
ARITHMETIC_ERROR: Final = "cypher_guardrail.Error.ArithmeticError"
# The query can modify the graph: a warning, or an error when the validator was told to allow only reads
WRITE_QUERY_WARNING: Final = "cypher_guardrail.Warning.WriteQuery"
WRITE_QUERY_ERROR: Final = "cypher_guardrail.Error.WriteQuery"
# More codes cypher-guardrail reports.
CONFLICTING_VARIABLES: Final = "cypher_guardrail.Error.ConflictingVariables"
INVALID_PROPERTY_ACCESS: Final = "cypher_guardrail.Warning.InvalidPropertyAccess"
PROPERTY_TYPE_MISMATCH: Final = "cypher_guardrail.Warning.PropertyTypeMismatch"
AGGREGATE_TYPE_MISMATCH: Final = "cypher_guardrail.Warning.AggregateTypeMismatch"
UNKNOWN_PATH: Final = "cypher_guardrail.Warning.UnknownPath"
VARIABLE_LENGTH_PATH: Final = "cypher_guardrail.Warning.VariableLengthPath"
WRONG_DIRECTION_PATH: Final = "cypher_guardrail.Warning.WrongDirectionPath"
WRONG_DIRECTION_VARIABLE_LENGTH_PATH: Final = (
    "cypher_guardrail.Warning.WrongDirectionVariableLengthPath"
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
    """Properties the query reads from a node label or a relationship type that does not have them.

    ``kind`` says which of the two ``name`` is, as the query writes it: ``(n:Movie)`` is a ``"node"`` and
    ``[r:ACTED_IN]`` a ``"relationship"``. A node label and a relationship type can have the same name, so the name
    alone is not enough to tell whether the issue repeats an ``UnknownLabel`` (a ``"node"``) or an
    ``UnknownRelationshipType`` (a ``"relationship"``). For alternatives (``TYPE1|TYPE2``) ``name`` is the first one.
    """

    code: Literal["cypher_guardrail.Warning.InvalidPropertyAccess"]
    description: str
    kind: Literal["node", "relationship"]
    name: str
    properties: list[str]


class PropertyTypeMismatchIssue(TypedDict):
    """The query compares a property with a literal of a type that none of its values has (``check_types=True``).

    FalkorDB does not report it: a comparison of a property with a value of another type (``year = '2020'`` for an
    integer ``year``) finds nothing. ``kind`` and ``name`` are the node label or relationship type the property is read from (see
    :class:`InvalidPropertyAccessIssue`). ``expected_types`` are the types of the values the property has (``typeof``),
    several when the data is mixed; ``value`` is the literal as written (a string without its quotes) and
    ``value_type`` its type: ``String``, ``Integer``, ``Float`` or ``Boolean``.
    """

    code: Literal["cypher_guardrail.Warning.PropertyTypeMismatch"]
    description: str
    kind: Literal["node", "relationship"]
    name: str
    property: str
    expected_types: list[str]
    value: str
    value_type: str


class AggregateTypeMismatchIssue(TypedDict):
    """The query calls an aggregate that needs numbers (``sum``, ``avg``, ``stDev``, ``stDevP``, ``percentileCont`` or
    ``percentileDisc``) on a property none of whose values is a number (``check_types=True``).

    Unlike a comparison, FalkorDB fails when it runs such a query: ``Type mismatch: expected Integer, Float, or Null
    but was String``. ``function`` is the aggregate, ``property`` the property it is called on, ``kind`` and ``name``
    the node label or relationship type (see :class:`InvalidPropertyAccessIssue`), and ``types`` the types of the
    values the property has (``typeof``: ``String``, ``Boolean``, ...).
    """

    code: Literal["cypher_guardrail.Warning.AggregateTypeMismatch"]
    description: str
    function: str
    kind: Literal["node", "relationship"]
    name: str
    property: str
    types: list[str]


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


class UnknownFunctionIssue(TypedDict):
    """A function with a dotted name (``library.function``) is called but the FalkorDB instance has no such
    user-defined function.

    ``name`` is the name as the query writes it (without spaces or back-quotes). User-defined functions belong to the
    instance, not to the graph. FalkorDB refuses the query when it runs (``Unknown function``).
    """

    code: Literal["cypher_guardrail.Warning.UnknownFunction"]
    description: str
    name: str


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


class CartesianProductIssue(TypedDict):
    """The query matches patterns that have nothing in common, so FalkorDB pairs every row of one with every row of
    the others: the work grows as the product of their sizes. ``variables`` holds the named variables of each pattern
    (anonymous elements have none). Not reported for a product of index lookups."""

    code: Literal["cypher_guardrail.Warning.CartesianProduct"]
    description: str
    variables: list[list[str]]


class UnlabeledNodeScanIssue(TypedDict):
    """A node with no label is looked for among all the nodes of the graph and filtered on a property, so no index
    can be used. ``variable`` is the node."""

    code: Literal["cypher_guardrail.Warning.UnlabeledNodeScan"]
    description: str
    variable: str


class UnboundedPathIssue(TypedDict):
    """A variable-length path has no upper limit (``*``, ``*2..``): it follows relationships as far as they go.
    ``path`` is the path as the query writes it."""

    code: Literal["cypher_guardrail.Warning.UnboundedPath"]
    description: str
    path: str


class UnboundVariableIssue(TypedDict):
    """The query uses a variable that no clause before it defines (FalkorDB: ``'x' not defined``)."""

    code: Literal["cypher_guardrail.Error.UnboundVariable"]
    description: str
    variable: str


class UnknownFunctionErrorIssue(TypedDict):
    """The database does not know a function the query calls (FalkorDB: ``Unknown function 'f'``).

    The warning of the schema validator (``UnknownFunctionIssue``) says the same of a function with a dotted name,
    without a database.
    """

    code: Literal["cypher_guardrail.Error.UnknownFunction"]
    description: str
    name: str


class InvalidLimitIssue(TypedDict):
    """``LIMIT`` or ``SKIP`` has a value that is not a positive integer (a string, a negative number, a property)."""

    code: Literal["cypher_guardrail.Error.InvalidLimit"]
    description: str
    clause: Literal["LIMIT", "SKIP"]


Issue = (
    UnknownLabelIssue
    | UnknownRelationshipTypeIssue
    | UnknownPropertyKeyIssue
    | InvalidPropertyAccessIssue
    | PropertyTypeMismatchIssue
    | AggregateTypeMismatchIssue
    | ParameterNotProvidedIssue
    | UnknownIndexIssue
    | UnknownFunctionIssue
    | ConflictingVariablesIssue
    | UnknownPathIssue
    | VariableLengthPathIssue
    | WrongDirectionPathIssue
    | WrongDirectionVariableLengthPathIssue
    | WriteQueryWarningIssue
    | WriteQueryErrorIssue
    | DatabaseErrorIssue
    | UnboundVariableIssue
    | UnknownFunctionErrorIssue
    | InvalidLimitIssue
    | CartesianProductIssue
    | UnlabeledNodeScanIssue
    | UnboundedPathIssue
)
