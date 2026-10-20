"""The functions that a query calls by a name with a dot (``library.function(...)``), and which of them are built in.

A user-defined function (UDF) is always called as ``library.function``. FalkorDB has few built-in functions with a
dot in their name (``vec.cosineDistance``, ``list.sort``, ``string.join``, ``date.transaction``, ...) and none of the
others needs a catalog to be told from a UDF, so only the calls by a dotted name are looked at: a name without a dot
(``toUpper``, ``foo``) is the business of the database, which refuses what it does not know.
"""

import regex as re

from cypher_guardrail import patterns

# The built-in functions of FalkorDB whose names have a dot, in lower case (FalkorDB reads them in any case). It is what
# ``CALL dbms.functions()`` lists: a test compares the two, so a new one in a new version of FalkorDB is not taken for a
# UDF that does not exist.
BUILTIN_DOTTED = frozenset(
    {
        "date.transaction",
        "list.dedup",
        "list.insert",
        "list.insertlistelements",
        "list.remove",
        "list.sort",
        "localdatetime.transaction",
        "localtime.transaction",
        "string.join",
        "string.matchregex",
        "string.replaceregex",
        "vec.cosinedistance",
        "vec.euclideandistance",
    }
)

_NAME = r"(?:\w+|`[^`]*`)"
# `library.function(`, also written `library . function (` or with back-quoted names, and not a property (`a.b.c(` is
# one name: the match starts at the first word, which nothing word-like or dotted precedes)
_CALL = re.compile(rf"(?<![\w.`$])({_NAME}(?:\s*\.\s*{_NAME})+)\s*\(")
_PROCEDURE = re.compile(r"\bCALL\s+$", re.IGNORECASE)


def _plain(name: str) -> str:
    """The parts of a dotted name, without spaces and back-quotes: ``` `my lib` . f ``` is ``my lib.f``."""
    return ".".join(
        patterns.unquote_name(part.strip()) for part in re.split(r"\s*\.\s*", name)
    )


def called(query: str) -> list[str]:
    """The functions with a dotted name that ``query`` calls and that are not built in: ``["library.function", ...]``.

    Each once, in the order of the query. A procedure (``CALL db.labels()``) is not a function, and what is in a
    string or a comment is not a call.
    """
    text = patterns.strip_literals_and_comments(query)
    found = dict.fromkeys(
        _plain(match[1])
        for match in _CALL.finditer(text)
        if not _PROCEDURE.search(text[: match.start()])
    )
    return [name for name in found if name.lower() not in BUILTIN_DOTTED]
