"""A division or a modulo by zero that is in the text of a query: ``/ 0``, ``% (1 - 1)``, ``/ (2 * 0)``.

FalkorDB 4 refused such a query when it planned it. FalkorDB 6 plans it, and fails only when the query runs, so a query
that was generated with a divisor that is always zero would be valid. Only what is certain is reported: FalkorDB fails
on an *integer* divisor of zero (``1 / 0``, ``5 % 0``, ``1 / (5 / 10)``), and answers ``inf`` or ``nan`` when a decimal
number is in the division (``1.0 / 0``, ``1 / 0.0``, ``5 % (1 - 1.0)``), which is no error. A divisor that is not made
of numbers (``x - x``, ``abs(0)``) is not looked into: that would be running the query.
"""

import regex as re

_NUMBER = re.compile(r"(\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?")
_DECIMAL_BEFORE = re.compile(
    r"(?<![\w.])(?:\d+\.\d*|\.\d+|\d+[eE][+-]?\d+)(?:[eE][+-]?\d+)?\s*$"
)
_OPERATORS = "/%"


class _NotConstant(Exception):
    """The text is not an arithmetic expression of numbers."""


class _Expression:
    """The value of an arithmetic expression of numbers, in the integers and decimals of Cypher.

    A value is ``(number, is_decimal)``; a division by zero inside the expression, or anything that is not a number,
    raises :class:`_NotConstant` (the first is found when the scan reaches it as a divisor of its own).
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.at = 0

    def value(self) -> tuple[float, bool]:
        result = self._sum()
        if self._peek() != "":
            raise _NotConstant
        return result

    def _peek(self) -> str:
        while self.at < len(self.text) and self.text[self.at].isspace():
            self.at += 1
        return self.text[self.at] if self.at < len(self.text) else ""

    def _sum(self) -> tuple[float, bool]:
        total, decimal = self._product()
        while self._peek() in ("+", "-"):
            sign = 1 if self.text[self.at] == "+" else -1
            self.at += 1
            other, other_decimal = self._product()
            total, decimal = total + sign * other, decimal or other_decimal
        return total, decimal

    def _product(self) -> tuple[float, bool]:
        total, decimal = self._term()
        while self._peek() and self._peek() in "*" + _OPERATORS:
            operator = self.text[self.at]
            self.at += 1
            other, other_decimal = self._term()
            decimal = decimal or other_decimal
            total = _apply(operator, total, other, decimal)
        return total, decimal

    def _term(self) -> tuple[float, bool]:
        char = self._peek()
        if char in ("+", "-"):
            self.at += 1
            number, decimal = self._term()
            return (-number if char == "-" else number), decimal
        if char == "(":
            self.at += 1
            inner = self._sum()
            if self._peek() != ")":
                raise _NotConstant
            self.at += 1
            return inner
        match = _NUMBER.match(self.text, self.at)
        if not match:
            raise _NotConstant
        self.at = match.end()
        text = match.group()
        decimal = any(mark in text for mark in ".eE")
        return (float(text) if decimal else int(text)), decimal


def _apply(operator: str, left: float, right: float, decimal: bool) -> float:
    """``left`` ``operator`` ``right``: an integer division truncates, as it does in Cypher."""
    if operator == "*":
        return left * right
    if right == 0:
        if decimal:
            return 0.0  # inf or nan, which is a value and not an error; the divisor of the next one is what counts
        raise _NotConstant
    if decimal:
        return left / right if operator == "/" else left % right
    quotient = abs(int(left)) // abs(int(right))
    quotient = quotient if (left < 0) == (right < 0) else -quotient
    return quotient if operator == "/" else int(left) - quotient * int(right)


def _divisor_at(code: str, start: int) -> tuple[str, int] | None:
    """The divisor that starts at ``start`` (a number or a group in parentheses) and where it ends."""
    while start < len(code) and code[start].isspace():
        start += 1
    end = start
    if code.startswith(("-", "+"), end):
        end += 1
        while end < len(code) and code[end].isspace():
            end += 1
    if code.startswith("(", end):
        depth = 0
        for at in range(end, len(code)):
            depth += (code[at] == "(") - (code[at] == ")")
            if depth == 0:
                return code[start : at + 1], at + 1
        return None
    match = _NUMBER.match(code, end)
    return (code[start : match.end()], match.end()) if match else None


_PROPERTY_BEFORE = re.compile(r"(?<![\w.])(\w+)\s*\.\s*(\w+)\s*$")


def zero_divisions(code: str) -> list[tuple[str, str] | None]:
    """The divisions and the modulos by an integer zero of ``code`` (a query without its strings and comments), each
    as the ``(variable, property)`` that is divided, or None when the dividend is anything else.

    The type of the dividend is not known from the text. A decimal literal (``1.0 / 0``) is told by its writing; a
    property (``p.age / 0``) fails only when its values are integers, which the caller can ask.
    """
    found: list[tuple[str, str] | None] = []
    for operator in re.finditer(r"[/%]", code):
        divisor = _divisor_at(code, operator.end())
        if divisor is None:
            continue
        text, end = divisor
        # the number must be the whole divisor: not `0.5` or `0abc` or `0(`, and not the base of `0 ^ x`
        if code[end : end + 1] in (".", "(", "_") or code[end : end + 1].isalnum():
            continue
        if code[end:].lstrip().startswith(("^", "[")):
            continue
        before = code[: operator.start()]
        if _DECIMAL_BEFORE.search(before):
            continue
        try:
            number, decimal = _Expression(text).value()
        except _NotConstant:
            continue
        if number == 0 and not decimal:
            dividend = _PROPERTY_BEFORE.search(before)
            found.append((dividend[1], dividend[2]) if dividend else None)
    return found


def divides_by_zero(code: str) -> bool:
    """Whether ``code`` has a division or a modulo by an integer zero (see :func:`zero_divisions`)."""
    return bool(zero_divisions(code))
