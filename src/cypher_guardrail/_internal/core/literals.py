"""Finding where a query compares a property with a literal value.

FalkorDB does not fail when a property is compared with a value of another type (``n.year = '2020'`` for an integer
``year``): it finds nothing. The type check of the properties validator needs the places where a property meets a
literal, and the type of the literal. It must not guess: a comparison it is not sure of is left out, so that what is
reported is a real mismatch.

What is found, in the clauses that read (``MATCH``, ``WHERE``, ``RETURN``, ``WITH``, ...):

* ``variable.property <op> literal`` and ``literal <op> variable.property``, with ``=``, ``<>``, ``!=``, ``<``,
  ``>``, ``<=`` and ``>=``;
* ``variable.property IN [literal, ...]`` (and ``NOT IN``), for each literal of the list;
* ``variable.property STARTS WITH | ENDS WITH | CONTAINS 'text'`` and ``variable.property =~ 'regex'``: the property
  has to be a string;
* ``(variable:Label {property: literal})`` and ``[variable:TYPE {property: literal}]``, with exactly one label.

What is left out: anything in a clause that writes (``CREATE``, ``MERGE``, ``SET``, ``DELETE``, ``REMOVE``,
``FOREACH``: there ``=`` assigns and a map creates), parameters, other properties and expressions on either side
(``n.year = 2019 + 1``, ``n.year + 1 = 2021``, ``toString(n.year) = '2020'``), and ``null``.

A literal is a string (``'..'`` or ``".."``), a number (an integer, or a float with a ``.`` or an exponent) or a boolean.
"""

import re
from dataclasses import dataclass

_TOKEN = re.compile(
    r"""
      (?P<space>\s+)
    | (?P<comment>//[^\n]*|/\*.*?(?:\*/|\Z))
    | (?P<str>'(?:\\.|[^'\\])*'?|"(?:\\.|[^"\\])*"?)
    | (?P<num>0[xX][0-9a-fA-F]+|(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)
    | (?P<id>[^\W\d]\w*)
    | (?P<qid>`[^`]*`?)
    | (?P<param>\$\w+)
    | (?P<op><>|<=|>=|=~|!=|\+=|[=<>])
    | (?P<punct>.)
    """,
    re.VERBOSE | re.DOTALL,
)

# The clauses that read, and the ones that write: in the second the "=" of a comparison assigns, and a {map} creates
_READ = frozenset(
    {
        "MATCH",
        "WHERE",
        "RETURN",
        "WITH",
        "UNWIND",
        "ORDER",
        "SKIP",
        "LIMIT",
        "CALL",
        "YIELD",
        "UNION",
    }
)
_WRITE = frozenset({"CREATE", "MERGE", "SET", "DELETE", "REMOVE", "FOREACH"})
_COMPARE = frozenset({"=", "<>", "!=", "<", ">", "<=", ">="})
_ARITHMETIC = frozenset({"+", "-", "*", "/", "%", "^"})
# After a property or a literal: the expression goes on (a call, an index, a member, more arithmetic)
_CONTINUES = _ARITHMETIC | {".", "(", "["}
# Before one: it is part of a bigger expression (`1 + n.a`, `a.b.c`). A bracket before it is not: `(n.a = 'x')`
_PART_OF_A_LARGER_EXPRESSION = _ARITHMETIC | {"."}


@dataclass(frozen=True)
class Comparison:
    """A property compared with a literal.

    Attributes:
        variable: The variable the property is read from (``n`` in ``n.year = 2020``); None for a ``{map}``.
        label: The label or relationship type the pattern gives (``Goal`` in ``(:Goal {year: 2020})``); None when the
            comparison names a variable, whose label the query says elsewhere.
        entity: ``"node"`` or ``"relationship"`` for a ``{map}`` (its brackets say which); None otherwise.
        property: The property.
        value: The literal as written, without the quotes of a string.
        value_type: ``"String"``, ``"Integer"``, ``"Float"`` or ``"Boolean"``: the names ``typeof`` uses.
    """

    variable: str | None
    label: str | None
    entity: str | None
    property: str
    value: str
    value_type: str


def _tokens(query: str) -> list[tuple[str, str]]:
    """``(kind, text)`` of every token of ``query`` that is not a space or a comment.

    ``kind`` is ``str`` (the text is the string without its quotes), ``num``, ``id``, ``qid`` (a back-quoted name,
    without the back-quotes), ``param``, ``op`` or ``punct`` (one character).
    """
    tokens = []
    for match in _TOKEN.finditer(query):
        kind = match.lastgroup
        text = match.group()
        if kind in ("space", "comment") or kind is None:
            continue
        if kind == "str":
            text = text[1:-1] if len(text) > 1 and text[-1] == text[0] else text[1:]
        elif kind == "qid":
            text = text.strip("`")
        tokens.append((kind, text))
    return tokens


def _name(token) -> bool:
    return token is not None and token[0] in ("id", "qid")


def _punct(token, *texts) -> bool:
    return token is not None and token[0] == "punct" and token[1] in texts


def _word(token, *words) -> bool:
    """Whether ``token`` is one of the keywords (written in any case, not back-quoted)."""
    return token is not None and token[0] == "id" and token[1].upper() in words


def _at(tokens, i):
    return tokens[i] if 0 <= i < len(tokens) else None


def _literal(tokens, i, signed=False):
    """The literal that starts at ``tokens[i]``: ``(value, type, index after it)``, or None."""
    token = _at(tokens, i)
    if token is None:
        return None
    kind, text = token
    if kind == "str":
        return text, "String", i + 1
    negative = signed and kind == "punct" and text == "-"
    if negative:
        token = _at(tokens, i + 1)
        if token is None or token[0] != "num":
            return None
        kind, text = token
        i += 1
        text = f"-{text}"
    if kind == "num":
        bare = text.lstrip("-")
        is_float = not bare.lower().startswith("0x") and any(c in bare for c in ".eE")
        return text, "Float" if is_float else "Integer", i + 1
    if _word(token, "TRUE", "FALSE"):
        return text, "Boolean", i + 1
    return None


def _ends_the_value(tokens, i) -> bool:
    """Whether the literal before ``tokens[i]`` stands alone: nothing goes on with it (``1 + 2``, ``'a'.size``)."""
    token = _at(tokens, i)
    return token is None or not (token[0] == "punct" and token[1] in _CONTINUES)


def _property_at(tokens, i):
    """``(variable, property, index after it)`` if ``tokens[i:]`` is ``variable.property`` that stands alone."""
    if not (
        _name(_at(tokens, i))
        and _punct(_at(tokens, i + 1), ".")
        and _name(_at(tokens, i + 2))
    ):
        return None
    before = _at(tokens, i - 1)
    # `a.b.c` is a member of a member; `1 + n.a` and `-n.a` are part of an expression
    if (
        before is not None
        and before[0] == "punct"
        and before[1] in _PART_OF_A_LARGER_EXPRESSION
    ):
        return None
    if not _ends_the_value(tokens, i + 3):
        return None
    return tokens[i][1], tokens[i + 2][1], i + 3


def _variable_comparisons(tokens, i):
    """``variable.property`` followed by an operator and a literal."""
    found = _property_at(tokens, i)
    if found is None:
        return
    variable, prop, k = found
    token = _at(tokens, k)
    if token is None:
        return
    if token[0] == "op" and token[1] in _COMPARE:
        literal = _literal(tokens, k + 1, signed=True)
        if literal and _ends_the_value(tokens, literal[2]):
            yield Comparison(variable, None, None, prop, literal[0], literal[1])
    elif token[0] == "op" and token[1] == "=~":
        literal = _literal(tokens, k + 1)
        if literal and literal[1] == "String" and _ends_the_value(tokens, literal[2]):
            yield Comparison(variable, None, None, prop, literal[0], "String")
    elif _word(token, "STARTS", "ENDS") and _word(_at(tokens, k + 1), "WITH"):
        yield from _string_operand(tokens, variable, prop, k + 2)
    elif _word(token, "CONTAINS"):
        yield from _string_operand(tokens, variable, prop, k + 1)
    elif _word(token, "IN") or (
        _word(token, "NOT") and _word(_at(tokens, k + 1), "IN")
    ):
        start = k + (2 if _word(token, "NOT") else 1)
        yield from _members(tokens, variable, prop, start)


def _string_operand(tokens, variable, prop, k):
    literal = _literal(tokens, k)
    if literal and literal[1] == "String" and _ends_the_value(tokens, literal[2]):
        yield Comparison(variable, None, None, prop, literal[0], "String")


def _members(tokens, variable, prop, k):
    """The literals of ``IN [a, b, ...]``; an element that is not just a literal is skipped."""
    if not _punct(_at(tokens, k), "["):
        return
    k += 1
    while k < len(tokens):
        literal = _literal(tokens, k, signed=True)
        if literal and _punct(_at(tokens, literal[2]), ",", "]"):
            value, value_type, k = literal
            yield Comparison(variable, None, None, prop, value, value_type)
        else:
            # a parameter, an expression (`1 + 2`), a nested list: leave it, up to the next element of the list
            depth = 0
            while k < len(tokens):
                token = tokens[k]
                if _punct(token, "[", "(", "{"):
                    depth += 1
                elif _punct(token, ")", "}") or (_punct(token, "]") and depth):
                    depth -= 1
                elif depth == 0 and _punct(token, ",", "]"):
                    break
                k += 1
        if not _punct(_at(tokens, k), ","):
            return  # the end of the list (or of the query)
        k += 1


def _reversed_comparison(tokens, i):
    """``literal <op> variable.property``."""
    literal = _literal(tokens, i)
    if literal is None:
        return
    value, value_type, k = literal
    before = _at(tokens, i - 1)
    if (
        before is not None
        and before[0] == "punct"
        and before[1] in _PART_OF_A_LARGER_EXPRESSION
    ):
        return  # part of an expression: `1 + 2 = n.a`, `x.y = 1`
    token = _at(tokens, k)
    if token is None or token[0] != "op" or token[1] not in _COMPARE:
        return
    found = _property_at(tokens, k + 1)
    if found is not None:
        variable, prop, _ = found
        yield Comparison(variable, None, None, prop, value, value_type)


def _map_comparisons(tokens, i):
    """``(variable:Label {property: literal, ...})`` and the same in square brackets, for one label."""
    opening = _at(tokens, i)
    if not _punct(opening, "(", "["):
        return
    entity = "node" if opening[1] == "(" else "relationship"
    j = i + 1
    variable = None
    if _name(_at(tokens, j)) and _punct(_at(tokens, j + 1), ":"):
        variable = tokens[j][1]
        j += 1
    if not (_punct(_at(tokens, j), ":") and _name(_at(tokens, j + 1))):
        return
    label = tokens[j + 1][1]
    if not _punct(_at(tokens, j + 2), "{"):
        return  # no map, or more labels / alternatives / a length first
    k = j + 3
    while _name(_at(tokens, k)) and _punct(_at(tokens, k + 1), ":"):
        literal = _literal(tokens, k + 2, signed=True)
        if literal is None or not _punct(_at(tokens, literal[2]), ",", "}"):
            return
        yield Comparison(variable, label, entity, tokens[k][1], literal[0], literal[1])
        k = literal[2]
        if _punct(_at(tokens, k), ","):
            k += 1
        else:
            return


def comparisons(query: str) -> list[Comparison]:
    """The comparisons of a property with a literal in ``query``, as the module docstring says, each once, in order."""
    tokens = _tokens(query)
    found: list[Comparison] = []
    writing = False
    for i, token in enumerate(tokens):
        before, after = _at(tokens, i - 1), _at(tokens, i + 1)
        # A keyword, not a property (`n.set`), a label (`:Match`) or the key of a map (`{match: 1}`)
        if token[0] == "id" and not _punct(before, ".", ":") and not _punct(after, ":"):
            word = token[1].upper()
            if word in _READ:
                writing = False
            elif word in _WRITE:
                writing = True
        if writing:
            continue
        for comparison in (
            *_variable_comparisons(tokens, i),
            *_reversed_comparison(tokens, i),
            *_map_comparisons(tokens, i),
        ):
            if comparison not in found:
                found.append(comparison)
    return found
