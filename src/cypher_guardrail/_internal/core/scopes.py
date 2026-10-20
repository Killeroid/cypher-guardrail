"""Scopes of variables in a Cypher query.

The validators pool the variables of a query by name: ``n`` is one variable wherever it is written. Cypher does not: a
``WITH`` hands on only the names it lists, and a ``CALL { ... }`` subquery sees only the names its leading ``WITH``
imports, so ``MATCH (n:Person) WITH n.name AS name MATCH (n:Movie) ...`` has two variables called ``n``.

:func:`separate_variables` gives a variable that is declared again in another scope a name of its own (``n__v1``), so
that code that pools by name sees two variables, and :func:`restore_variables` gives the names back where text leaves
the validators. It renames only those: a query with one variable of each name (and one with no ``WITH`` or ``CALL``) is
not touched, and in the one scope of a query a name that is used twice (``MATCH (n:A) MATCH (n:B)``) is still one variable.

What is a variable is read from the text, as everywhere in this library. A word is not one if it is a keyword, a property
(after a dot), a label or a type (after a colon, or a ``|`` in a list of them), the key of a map, a function or a
procedure (before a parenthesis, qualified or not), or a parameter. A subquery is a ``{`` after ``CALL``; any other
brace is a map.
"""

import re
from typing import Any, NamedTuple

# Strings, comments and quoted names are not scanned (they have no tokens): what is left is words, parameters, numbers
# and every other character one by one
_SCAN = re.compile(
    r"""
      '(?:\\.|[^'\\])*+'
    | "(?:\\.|[^"\\])*+"
    | `[^`]*+`
    | //[^\n]*+
    | /\*.*?\*/
    | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<dollar>\$[A-Za-z0-9_]+)
    | (?P<number>\d+(?:\.\d+)?)
    | (?P<punct>\S)
    """,
    re.VERBOSE | re.DOTALL,
)

_NEEDS_A_SCAN = re.compile(r"\b(?:WITH|CALL)\b", re.IGNORECASE)

KEYWORDS = frozenset(
    [
        "ALL",
        "AND",
        "ANY",
        "AS",
        "ASC",
        "ASCENDING",
        "BY",
        "CALL",
        "CASE",
        "CONSTRAINT",
        "CONTAINS",
        "CREATE",
        "CSV",
        "DELETE",
        "DESC",
        "DESCENDING",
        "DETACH",
        "DISTINCT",
        "DROP",
        "ELSE",
        "END",
        "ENDS",
        "EXISTS",
        "FALSE",
        "FIELDTERMINATOR",
        "FOREACH",
        "FROM",
        "HEADERS",
        "IN",
        "INDEX",
        "IS",
        "LIMIT",
        "LOAD",
        "MATCH",
        "MERGE",
        "NONE",
        "NOT",
        "NULL",
        "ON",
        "OPTIONAL",
        "OR",
        "ORDER",
        "REMOVE",
        "RETURN",
        "SET",
        "SINGLE",
        "SKIP",
        "STARTS",
        "THEN",
        "TRUE",
        "UNION",
        "UNWIND",
        "USING",
        "WHEN",
        "WHERE",
        "WITH",
        "XOR",
        "YIELD",
    ]
)

# What ends the items of a WITH or a RETURN
_CLAUSES = frozenset(
    [
        "MATCH",
        "OPTIONAL",
        "WHERE",
        "ORDER",
        "SKIP",
        "LIMIT",
        "RETURN",
        "UNWIND",
        "CALL",
        "CREATE",
        "MERGE",
        "SET",
        "DELETE",
        "DETACH",
        "REMOVE",
        "FOREACH",
        "UNION",
        "WITH",
        "LOAD",
        "USING",
    ]
)

_OPENING = {"(": ")", "[": "]", "{": "}"}
# The tokens of an alias in a WITH or a RETURN: AS and the name
_TOKENS_OF_AN_ALIAS = 2


class _Token(NamedTuple):
    kind: str  # "word", "dollar", "number" or "punct"
    text: str
    start: int
    end: int


class _Scopes:
    def __init__(self, query: str):
        self.query = query
        self.tokens = [
            _Token(match.lastgroup, match.group(), match.start(), match.end())
            for match in _SCAN.finditer(query)
            if match.lastgroup
        ]
        self.parent, self.match = self._brackets()
        self.seen: set[str] = (
            set()
        )  # every name used as a variable so far, in any scope
        self.generation: dict[str, int] = {}
        self.renames: dict[str, str] = {}  # new name -> the name it replaces
        self.replacements: list[tuple[int, int, str]] = []

    # ---------------------------------------------------------------------------------------- the text
    def _brackets(self):
        """For each token, the index of the bracket it is in (-1: none), and for each opening bracket its closing one."""
        parent, match, stack = [], {}, []
        for index, token in enumerate(self.tokens):
            if token.kind == "punct" and token.text in ")]}" and stack:
                opened = stack.pop()
                match[opened] = index
            parent.append(stack[-1] if stack else -1)
            if token.kind == "punct" and token.text in _OPENING:
                stack.append(index)
        return parent, match

    def _is(self, index: int, kind: str, *texts: str) -> bool:
        if not 0 <= index < len(self.tokens):
            return False
        token = self.tokens[index]
        return token.kind == kind and (not texts or token.text in texts)

    def _word(self, index: int, *words: str) -> bool:
        """Whether the token is one of the words (in any case)."""
        return self._is(index, "word") and self.tokens[index].text.upper() in words

    def _opener(self, index: int) -> str:
        parent = self.parent[index]
        return self.tokens[parent].text if parent >= 0 else ""

    # ------------------------------------------------------------------------------ what is a variable
    def _is_variable(self, k: int) -> bool:
        token = self.tokens[k]
        if token.text.upper() in KEYWORDS:
            return False
        if self._is(k - 1, "punct", "."):  # a property
            return False
        if self._is(k + 1, "punct", "("):  # a function
            return False
        if self._is(
            k + 1, "punct", "."
        ):  # a procedure or function with a namespace: a.b.c(
            end = k
            while self._is(end + 1, "punct", ".") and self._is(end + 2, "word"):
                end += 2
            if self._is(end + 1, "punct", "("):
                return False
        opener = self._opener(k)
        if (
            self._is(k - 1, "punct", ":") and opener != "{"
        ):  # a label or a type (in a map, a value)
            return False
        if self._is(k - 1, "punct", "|") and opener in ("(", "["):  # the next of A|B
            return self._has_no_colon_before(k)
        # the key of a map is not one
        return not (
            opener == "{"
            and self._is(k + 1, "punct", ":")
            and self._is(k - 1, "punct", "{", ",")
        )

    def _has_no_colon_before(self, k: int) -> bool:
        """Whether no ':' comes before token k in its bracket (if one does, a '|' there separates labels or types)."""
        for index in range(self.parent[k] + 1, k):
            if self.parent[index] == self.parent[k] and self._is(index, "punct", ":"):
                return False
        return True

    # ---------------------------------------------------------------------------------------- naming
    def _fresh(self, name: str) -> str:
        number = self.generation.get(name, 0)
        while True:
            number += 1
            candidate = f"{name}__v{number}"
            if candidate not in self.query and candidate not in self.renames:
                break
        self.generation[name] = number
        self.renames[candidate] = name
        return candidate

    def _use(self, k: int, visible: dict[str, str]) -> None:
        """A variable is used at token k: the variable of this scope, or a new one if the name was used in another."""
        token = self.tokens[k]
        name = token.text
        if name in visible:
            text = visible[name]
        elif name in self.seen:
            text = visible[name] = self._fresh(name)
        else:
            text = visible[name] = name
        self.seen.add(name)
        if text != name:
            self.replacements.append((token.start, token.end, text))

    # -------------------------------------------------------------------------------- the clauses
    def _items(self, start: int, end: int, parent: int) -> list[tuple[int, int]]:
        """The ranges of the items of a WITH or a RETURN, between the commas that are at the level of the clause."""
        items, first = [], start
        for index in range(start, end):
            if self._is(index, "punct", ",") and self.parent[index] == parent:
                items.append((first, index))
                first = index + 1
        items.append((first, end))
        return items

    def _clause_end(self, k: int, hi: int) -> int:
        """Where the items of the clause that starts at token k end: at the next clause word of the same level."""
        for index in range(k + 1, hi):
            if self.parent[index] == self.parent[k] and self._word(index, *_CLAUSES):
                return index
        return hi

    def _carried(
        self, k: int, hi: int, visible: dict[str, str]
    ) -> tuple[int, dict[str, str]]:
        """What a WITH (or the names a RETURN gives) hands on: where its items end, and the names with their variables."""
        end = self._clause_end(k, hi)
        carried: dict[str, str] = {}
        for first, last in self._items(k + 1, end, self.parent[k]):
            if self._word(first, "DISTINCT"):
                first += 1
            if last - first == 1 and self._is(first, "punct", "*"):
                carried.update(visible)
            elif (
                last - first >= _TOKENS_OF_AN_ALIAS
                and self._word(last - 2, "AS")
                and self._is(last - 1, "word")
            ):
                alias = self.tokens[last - 1].text
                carried[alias] = alias
            elif (
                last - first == 1
                and self._is(first, "word")
                and self.tokens[first].text in visible
            ):
                carried[self.tokens[first].text] = visible[self.tokens[first].text]
        return end, carried

    def _level(
        self, lo: int, hi: int, parent: int, imported: dict[str, str]
    ) -> dict[str, str]:
        """Scan the tokens of one scope (the query, or the body of a subquery); what its last RETURN gives."""
        initial = dict(imported)
        visible = dict(imported)
        boundary: tuple[int, dict[str, str]] | None = (
            None  # where the items of a WITH end, and what it carries
        )
        last_return = -1
        k = lo
        while k < hi:
            if boundary is not None and k == boundary[0]:
                visible, boundary = boundary[1], None
            token = self.tokens[k]
            if token.kind != "word":
                k += 1
                continue
            if (
                self._word(k, "CALL")
                and self._is(k + 1, "punct", "{")
                and (k + 1) in self.match
            ):
                k = self._subquery(k + 1, visible)
                continue
            if self.parent[k] == parent:
                if self._word(k, "WITH") and not self._word(k - 1, "STARTS", "ENDS"):
                    boundary = self._carried(k, hi, visible)
                    k += 1
                    continue
                if self._word(k, "UNION"):
                    visible = dict(initial)
                elif self._word(k, "RETURN"):
                    last_return = k
            if self._word(k - 1, "AS") and self._is(k, "word"):
                # an alias is new where it is defined: it is not renamed, and the scope has it from here
                visible[token.text] = token.text
                self.seen.add(token.text)
            elif self._is_variable(k):
                self._use(k, visible)
            k += 1
        if last_return < 0:
            return {}
        return self._carried(last_return, hi, visible)[1]

    def _subquery(self, open_index: int, visible: dict[str, str]) -> int:
        """A ``CALL { ... }``: its body is a scope that sees only what its leading WITH imports. Returns the index after
        it, and the names it returns are variables of the scope that called it."""
        close_index = self.match[open_index]
        imported: dict[str, str] = {}
        first = open_index + 1
        if self._word(first, "WITH"):
            imported = self._carried(first, close_index, visible)[1]
        returned = self._level(open_index + 1, close_index, open_index, imported)
        visible.update(returned)
        return close_index + 1

    # ---------------------------------------------------------------------------------------- result
    def separated(self) -> str:
        self._level(0, len(self.tokens), -1, {})
        text = self.query
        for start, end, new in sorted(self.replacements, reverse=True):
            text = text[:start] + new + text[end:]
        return text


def separate_variables(query: str) -> tuple[str, dict[str, str]]:
    """``query`` with a name of its own for each variable that is declared again in another scope, and the names it was
    given (new name -> old name). A query that does not need it is returned as it is, with no names."""
    if not _NEEDS_A_SCAN.search(query):
        return query, {}
    scopes = _Scopes(query)
    text = scopes.separated()
    if not scopes.replacements:
        return query, {}
    return text, scopes.renames


def restore_variables(value: Any, renames: dict[str, str]) -> Any:
    """Give back the names that :func:`separate_variables` changed, in a text or in lists, tuples and dicts of texts.

    The keys of a dict are given back too; two keys that become the same have their lists or dicts merged.
    """
    if not renames:
        return value
    pattern = re.compile(
        r"\b(?:"
        + "|".join(re.escape(new) for new in sorted(renames, key=len, reverse=True))
        + r")\b"
    )
    return _restore(value, renames, pattern)


def _restore(value: Any, renames: dict[str, str], pattern: re.Pattern) -> Any:
    if isinstance(value, str):
        return pattern.sub(lambda match: renames[match.group()], value)
    if isinstance(value, list):
        return [_restore(item, renames, pattern) for item in value]
    if isinstance(value, tuple):
        return tuple(_restore(item, renames, pattern) for item in value)
    if isinstance(value, dict):
        restored: dict[Any, Any] = {}
        for key, item in value.items():
            key, item = (
                _restore(key, renames, pattern),
                _restore(item, renames, pattern),
            )
            restored[key] = _merge(restored[key], item) if key in restored else item
        return restored
    return value


def _merge(first: Any, second: Any) -> Any:
    if isinstance(first, list) and isinstance(second, list):
        return first + [item for item in second if item not in first]
    if isinstance(first, dict) and isinstance(second, dict):
        return {**first, **second}
    return second
