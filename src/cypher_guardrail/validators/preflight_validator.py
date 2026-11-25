from collections.abc import Iterator

import regex as re

from cypher_guardrail._internal.core import builders as _issues
from cypher_guardrail.issues import WARNINGS, Issue
from cypher_guardrail.patterns import strip_literals_and_comments

# The clauses that make a text a Cypher query
_CLAUSES = frozenset(
    {
        "MATCH",
        "CREATE",
        "MERGE",
        "DELETE",
        "SET",
        "REMOVE",
        "RETURN",
        "WITH",
        "UNWIND",
        "CALL",
        "DROP",
        "FOREACH",
        "LOAD",
    }
)
# The ones that modify the graph (``DETACH DELETE`` is a ``DELETE``, ``CREATE INDEX`` a ``CREATE``)
_WRITES = frozenset({"CREATE", "MERGE", "SET", "DELETE", "REMOVE", "DROP", "FOREACH"})
# A query that starts with one of these needs no MATCH
_NEEDS_NO_MATCH = frozenset({"CREATE", "MERGE", "CALL", "UNWIND", "WITH", "RETURN"})

# A word that is not part of a longer name, a property (``n.delete``), a label or relationship type (``:Drop``) or a
# parameter (``$set``), and not a map key (``{drop: 1}``)
_WORD = re.compile(r"(?<![\w$.:])[A-Za-z]+\b(?!\s*:)")
# The start of a list of relationship types, up to a ``|`` (``[:KEEP|``, ``[r:A|:B|``): the word after it is a type.
# A ``|`` anywhere else, as in ``FOREACH (n IN ns | DELETE n)``, is not one.
_TYPE_LIST = re.compile(r"\[\s*\w*\s*:\s*\w+(?:\s*\|\s*:?\s*\w+)*\s*\|\s*\Z")
# The procedures that write. FalkorDB decides what else can, so a procedure that is not here is not found.
_WRITING_PROCEDURE = re.compile(
    r"(?<![\w$.])db\s*\.\s*idx\s*\.\s*fulltext\s*\.\s*(?:createNodeIndex|drop)\b",
    re.IGNORECASE,
)
_QUOTED_NAME = re.compile(r"`[^`]*+`")
_OPENING = {"(": ")", "[": "]", "{": "}"}


def _clause_words(code: str) -> Iterator[str]:
    """The clause keywords of ``code`` (a query without strings and comments), in order, upper case."""
    for match in _WORD.finditer(code):
        word = match.group().upper()
        if word not in _CLAUSES:
            continue
        before = code[: match.start()].rstrip()
        if before.endswith("|") and _TYPE_LIST.search(before):
            continue  # [:KEEP|DELETE]: a relationship type
        yield word


def _balanced(code: str) -> bool:
    """Whether every bracket of ``code`` is closed by the same kind, in order."""
    expected: list[str] = []
    for char in code:
        if char in _OPENING:
            expected.append(_OPENING[char])
        elif char in ")]}" and (not expected or expected.pop() != char):
            return False
    return not expected


class PreflightValidator:
    """
    A quick check of a query that needs no database: is it a query, are its brackets balanced, does it write, does it
    read from the graph. It reads the text of the query only, so it is a first filter, not a replacement for
    :class:`~cypher_guardrail.SyntaxValidator`: FalkorDB decides what is valid Cypher and what writes.

    * It can miss a write: it knows the keywords (``CREATE``, ``MERGE``, ``SET``, ``DELETE``, ``REMOVE``, ``DROP``,
      ``FOREACH``) and the procedures of the full-text index, not the other procedures that modify the graph.
    * It does not call a read a write when the word is a name (``n.delete``, ``:Drop``, ``$set``, ``{drop: 1}``,
      ``[:KEEP|DELETE]``) or is inside a string, a quoted name or a comment. A reserved word used as a variable
      (``MATCH (set) ...``) is the one thing it takes for a write.

    The result is the one of the other validators: ``(is_valid, issues)``, and a warning leaves the query valid.
    """

    def __init__(self, read_only: bool = True) -> None:
        """
        Args:
            read_only (bool): Whether the queries may only read (default: True). A query that can modify the graph
                is then an error (``WRITE_QUERY_ERROR``); with False it is only a warning (``WRITE_QUERY_WARNING``)
                and the query is still valid.
        """
        self.read_only = read_only

    def validate(self, query: str) -> tuple[bool, list[Issue]]:
        """
        Check ``query`` without asking anything.

        Args:
            query (str): The Cypher query.

        Returns:
            tuple[bool, list[Issue]]: Whether the query is valid (it has no issue that is not a warning), and the
                issues found, in this order: empty, not a query, unbalanced brackets, writes, no ``MATCH``. An empty
                query has no other issue.
        """
        # The text of a quoted name is not Cypher either, so it is replaced by a name that is no keyword
        code = _QUOTED_NAME.sub("_q", strip_literals_and_comments(query))
        if not code.strip():
            return False, [_issues.empty_query()]

        issues: list[Issue] = []
        clauses = list(_clause_words(code))
        if not clauses:
            issues.append(_issues.no_cypher_clause())
        if not _balanced(code):
            issues.append(_issues.unbalanced_brackets())
        if _WRITES.intersection(clauses) or _WRITING_PROCEDURE.search(code):
            issues.append(_issues.write_query(self.read_only))
        if clauses and "MATCH" not in clauses and clauses[0] not in _NEEDS_NO_MATCH:
            issues.append(_issues.no_match_clause())

        return all(issue["code"] in WARNINGS for issue in issues), issues
