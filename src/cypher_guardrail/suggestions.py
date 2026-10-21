"""What :class:`~cypher_guardrail.Advisor` suggests, as typed dictionaries, and the codes.

A suggestion is not an issue: the query is valid, and a suggestion says what a person can do to make it cheaper.
It is never applied for them, because each of them can change what the query returns (``changes_results``).
Every suggestion is a dict with these fields:

* ``code``: one of the codes below.
* ``description``: the text for a person.
* ``fragment``: the exact text of the query that the suggestion is about.
* ``changes_results``: whether following it may change what the query returns (a bound on a path, a label on a node).
* ``example`` (optional): the query with ``fragment`` replaced, as it could be written. Only given when the
  replacement is certain (the graph leaves one answer) and ``fragment`` is written once in the query.
* ``statement`` (``MISSING_INDEX``): the command that creates the index. It is not a rewrite of the query, so there is
  no ``example``, and ``changes_results`` is False: an index never changes what a query returns.
* ``labels`` (``ADD_LABEL``): the labels that fit.
* ``relationship_types`` (``CONNECT_PATTERNS``): the types that connect the patterns, written from the first pattern
  to the second.
"""

from typing import Final, Literal, NotRequired, TypedDict

BOUND_PATH: Final = "cypher_guardrail.Suggestion.BoundPath"
ADD_LABEL: Final = "cypher_guardrail.Suggestion.AddLabel"
CONNECT_PATTERNS: Final = "cypher_guardrail.Suggestion.ConnectPatterns"
USE_INDEX: Final = "cypher_guardrail.Suggestion.UseIndex"
MISSING_INDEX: Final = "cypher_guardrail.Suggestion.MissingIndex"
ADD_LIMIT: Final = "cypher_guardrail.Suggestion.AddLimit"


class Suggestion(TypedDict):
    """One thing a person can do to make a query cheaper (see the module)."""

    code: Literal[
        "cypher_guardrail.Suggestion.BoundPath",
        "cypher_guardrail.Suggestion.AddLabel",
        "cypher_guardrail.Suggestion.ConnectPatterns",
        "cypher_guardrail.Suggestion.UseIndex",
        "cypher_guardrail.Suggestion.MissingIndex",
        "cypher_guardrail.Suggestion.AddLimit",
    ]
    description: str
    fragment: str
    changes_results: bool
    example: NotRequired[str]
    statement: NotRequired[str]
    labels: NotRequired[list[str]]
    relationship_types: NotRequired[list[str]]
