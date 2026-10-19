"""Builders of the issues the validators report (see ``cypher_guardrail.issues`` for what each one holds).

Each builder makes the ``description`` from the fields it returns, so the text and the data cannot disagree.
The sentences are the ones the validators have always reported: consumers may match on them.
"""

from typing import Literal, cast

from cypher_guardrail._internal.core.errors import (
    CONFLICTING_VARIABLES,
    INVALID_PROPERTY_ACCESS,
    PARAMETER_NOT_PROVIDED,
    PROPERTY_TYPE_MISMATCH,
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
from cypher_guardrail.issues import (
    ConflictingVariablesIssue,
    DatabaseErrorIssue,
    InvalidPropertyAccessIssue,
    ParameterNotProvidedIssue,
    PropertyTypeMismatchIssue,
    UnknownIndexIssue,
    UnknownLabelIssue,
    UnknownPathIssue,
    UnknownPropertyKeyIssue,
    UnknownRelationshipTypeIssue,
    VariableLengthPathIssue,
    WriteQueryErrorIssue,
    WriteQueryWarningIssue,
    WrongDirectionPathIssue,
    WrongDirectionVariableLengthPathIssue,
)


def database_error(code: str, message: str) -> DatabaseErrorIssue:
    """The database rejected the query, or it divides by zero. ``code`` is SYNTAX_ERROR, EXECUTION_FAILED or ARITHMETIC_ERROR."""
    return cast(DatabaseErrorIssue, {"code": code, "description": message})


def write_query(read_only: bool) -> WriteQueryErrorIssue | WriteQueryWarningIssue:
    """The query can modify the graph: an error when only reads are allowed, else a warning."""
    description = "The query can modify the graph (it is not read-only)."
    if read_only:
        return {
            "code": WRITE_QUERY_ERROR,
            "description": f"{description} Only reads are allowed: create the validator with read_only=False to allow writes.",
        }
    return {"code": WRITE_QUERY_WARNING, "description": description}


def unknown_label(label: str) -> UnknownLabelIssue:
    return {
        "code": UNKNOWN_LABEL,
        "description": "One of the labels in your query is not available in the database, make sure you "
        "didn't misspell it or that the label is available when you run this statement in your "
        f"application (the missing label name is: {label})",
        "label": label,
    }


def unknown_relationship_type(relationship_type: str) -> UnknownRelationshipTypeIssue:
    return {
        "code": UNKNOWN_REL_TYPE,
        "description": "One of the relationship types in your query is not available in the database, "
        "make sure you didn't misspell it or that it is available when you run this statement in your "
        f"application (the missing relationship type is: {relationship_type})",
        "relationship_type": relationship_type,
    }


def unknown_property(property: str) -> UnknownPropertyKeyIssue:
    return {
        "code": UNKNOWN_PROPERTY,
        "description": "One of the property names in your query is not available in the database, make "
        "sure you didn't misspell it or that it is available when you run this statement in your "
        f"application (the missing property name is: {property})",
        "property": property,
    }


def invalid_property_access(
    kind: Literal["node", "relationship"], name: str, properties: list[str]
) -> InvalidPropertyAccessIssue:
    what = "label" if kind == "node" else "relationship type"
    return {
        "code": INVALID_PROPERTY_ACCESS,
        "description": f"The {what} {name} does not have the following properties:  {', '.join(properties)}.",
        "kind": kind,
        "name": name,
        "properties": list(properties),
    }


def property_type_mismatch(
    kind: Literal["node", "relationship"],
    name: str,
    property: str,
    expected_types: list[str],
    value: str,
    value_type: str,
) -> PropertyTypeMismatchIssue:
    shown = f"'{value}'" if value_type == "String" else value
    return {
        "code": PROPERTY_TYPE_MISMATCH,
        "description": (
            f"The property {property} of {name} holds {' or '.join(expected_types)} values, but the query "
            f"compares it with the {value_type} {shown}: FalkorDB does not report it, it finds nothing when the "
            "types differ."
        ),
        "kind": kind,
        "name": name,
        "property": property,
        "expected_types": list(expected_types),
        "value": value,
        "value_type": value_type,
    }


def parameters_not_provided(parameters: list[str]) -> ParameterNotProvidedIssue:
    return {
        "code": PARAMETER_NOT_PROVIDED,
        "description": "Did not supply query with enough parameters. "
        f"Missing parameters: {', '.join(parameters)}",
        "parameters": list(parameters),
    }


def unknown_index(
    index: str, on: str, label: str, attribute: str | None
) -> UnknownIndexIssue:
    """``index`` is 'fulltext' or 'vector', ``on`` 'nodes' or 'relationships'."""
    target = "label" if on == "nodes" else "relationship type"
    wanted = f"{index} index on the {target} {label}"
    if attribute:
        wanted += f", attribute {attribute}"
    return {
        "code": UNKNOWN_INDEX,
        "description": f"The query needs a {wanted}, but the graph has none. FalkorDB answers a full-text "
        "query without an index with an empty result, and a vector query with an error.",
        "index": "vector" if index == "vector" else "fulltext",
        "on": "nodes" if on == "nodes" else "relationships",
        "label": label,
        "attribute": attribute,
    }


def conflicting_variables(
    variables: dict[str, list[str]],
) -> ConflictingVariablesIssue:
    """``variables`` maps each variable to its labels."""
    messages = [
        f"Variable '{variable}' has conflicting labels: {', '.join(labels)}"
        for variable, labels in variables.items()
    ]
    return {
        "code": CONFLICTING_VARIABLES,
        "description": " and ".join(messages),
        "variables": {variable: list(labels) for variable, labels in variables.items()},
    }


def unknown_path(path: str, original_path: str) -> UnknownPathIssue:
    return {
        "code": UNKNOWN_PATH,
        "description": f"The specified path {path} does not exist in the knowledge graph schema. The path {original_path} in the Cypher statement is wrong.",
        "path": path,
        "original_path": original_path,
    }


def variable_length_path(
    path: str, original_path: str, suggested_path: str
) -> VariableLengthPathIssue:
    return {
        "code": VARIABLE_LENGTH_PATH,
        "description": f"The specified path {path} does not exist in the knowledge graph schema. The path {original_path} in the Cypher statement should be changed to {suggested_path}.",
        "path": path,
        "original_path": original_path,
        "suggested_path": suggested_path,
    }


def wrong_direction_path(
    path: str, original_path: str, suggested_path: str
) -> WrongDirectionPathIssue:
    return {
        "code": WRONG_DIRECTION_PATH,
        "description": f"The specified path {path} has the opposite direction in the knowledge graph schema. The path {original_path} in the Cypher statement should be changed to  {suggested_path}.",
        "path": path,
        "original_path": original_path,
        "suggested_path": suggested_path,
    }


def wrong_direction_variable_length_path(
    path: str, original_path: str, suggested_path: str
) -> WrongDirectionVariableLengthPathIssue:
    return {
        "code": WRONG_DIRECTION_VARIABLE_LENGTH_PATH,
        "description": f"The specified path {path} does not exist in the knowledge graph schema. The path {original_path} in the Cypher statement should be changed to {suggested_path}.",
        "path": path,
        "original_path": original_path,
        "suggested_path": suggested_path,
    }
