"""A bounded field-calculation grammar.

A field calculator is the last obvious gap in ordinary GIS work, and it is also
the one place where the easy implementation is indefensible. `eval()` on a
model-supplied string, or handing that string to a general expression engine,
gives whatever produced it the run of the process. A prompt instruction is not a
security boundary, so the boundary is the grammar: this parser can only express
arithmetic over the layer's own fields.

What exists:

    field references      "area_m2", "population"
    numeric literals      12, 3.5, -2
    string literals       'residential'
    arithmetic            + - * / % and parentheses
    comparison            = <> < <= > >=  (yielding 1 or 0)
    a closed function set round, abs, min, max, floor, ceil, length, upper,
                          lower, concat, coalesce

What does not, and cannot be added by accident: attribute access, indexing,
calls to anything outside the table below, names that are not fields of the
layer, and any statement at all. Unknown syntax is a parse error rather than a
fallback, because a calculator that silently ignores the part it did not
understand writes a wrong number into someone's data.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Mapping, Sequence

MAX_EXPRESSION_LENGTH = 500
MAX_NODES = 200


class ExpressionError(Exception):
    """The expression is not something this grammar can express."""


# The closed function set. Each entry is (minimum arity, maximum arity, impl).
# Nothing here touches the filesystem, the network, the process or the clock:
# a field calculation that is not a pure function of the row is not
# reproducible, and two runs over the same data must agree.
_FUNCTIONS: dict[str, tuple[int, int, Callable[..., Any]]] = {
    "round": (1, 2, lambda value, digits=0: round(_number(value), int(_number(digits)))),
    "abs": (1, 1, lambda value: abs(_number(value))),
    "min": (2, 8, lambda *values: min(_number(v) for v in values)),
    "max": (2, 8, lambda *values: max(_number(v) for v in values)),
    "floor": (1, 1, lambda value: float(math.floor(_number(value)))),
    "ceil": (1, 1, lambda value: float(math.ceil(_number(value)))),
    "length": (1, 1, lambda value: float(len(_text(value)))),
    "upper": (1, 1, lambda value: _text(value).upper()),
    "lower": (1, 1, lambda value: _text(value).lower()),
    "concat": (2, 8, lambda *values: "".join(_text(v) for v in values)),
    # coalesce takes the first value that is not null, which is the one place
    # this grammar has an opinion about missing data.
    "coalesce": (2, 8, lambda *values: next((v for v in values if v is not None), None)),
}

_COMPARISONS = {"=": lambda a, b: a == b, "<>": lambda a, b: a != b,
                "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
                ">": lambda a, b: a > b, ">=": lambda a, b: a >= b}


def _number(value: Any) -> float:
    if value is None:
        raise ExpressionError("a null value cannot be used in arithmetic")
    if isinstance(value, bool):
        raise ExpressionError("a boolean cannot be used in arithmetic")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ExpressionError("{!r} is not a number".format(value)) from None


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class _Token:
    __slots__ = ("kind", "value")

    def __init__(self, kind: str, value: Any):
        self.kind = kind
        self.value = value

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<{} {!r}>".format(self.kind, self.value)


def _tokenize(source: str) -> list[_Token]:
    tokens: list[_Token] = []
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if char.isspace():
            index += 1
            continue
        if char.isdigit() or (char == "." and index + 1 < length and source[index + 1].isdigit()):
            start = index
            seen_dot = False
            while index < length and (source[index].isdigit() or (source[index] == "." and not seen_dot)):
                seen_dot = seen_dot or source[index] == "."
                index += 1
            tokens.append(_Token("number", float(source[start:index])))
            continue
        if char == "'":
            index += 1
            start = index
            while index < length and source[index] != "'":
                index += 1
            if index >= length:
                raise ExpressionError("a string literal is not closed")
            tokens.append(_Token("string", source[start:index]))
            index += 1
            continue
        if char.isalpha() or char == "_":
            start = index
            while index < length and (source[index].isalnum() or source[index] == "_"):
                index += 1
            tokens.append(_Token("name", source[start:index]))
            continue
        two = source[index:index + 2]
        if two in ("<=", ">=", "<>"):
            tokens.append(_Token("op", two))
            index += 2
            continue
        if char in "+-*/%(),=<>":
            tokens.append(_Token("op", char))
            index += 1
            continue
        # Anything else is refused by name, so the user learns what was wrong
        # rather than seeing a generic parse failure.
        raise ExpressionError("{!r} is not allowed in a field calculation".format(char))
    return tokens


class _Parser:
    """Recursive descent over the token stream.

    The node budget is a guard against an expression that parses but is
    pathological to evaluate over every row of a large layer.
    """

    def __init__(self, tokens: Sequence[_Token], fields: Sequence[str]):
        self.tokens = list(tokens)
        self.position = 0
        self.fields = {name.lower(): name for name in fields}
        self.nodes = 0

    def peek(self) -> _Token | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def take(self) -> _Token:
        token = self.peek()
        if token is None:
            raise ExpressionError("the expression ends unexpectedly")
        self.position += 1
        return token

    def expect_op(self, op: str) -> None:
        token = self.take()
        if token.kind != "op" or token.value != op:
            raise ExpressionError("expected {!r}".format(op))

    def count(self) -> None:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise ExpressionError("the expression is too complex")

    def parse(self):
        node = self.comparison()
        if self.peek() is not None:
            raise ExpressionError("unexpected trailing input")
        return node

    def comparison(self):
        left = self.additive()
        token = self.peek()
        if token is not None and token.kind == "op" and token.value in _COMPARISONS:
            self.take()
            right = self.additive()
            self.count()
            return ("compare", token.value, left, right)
        return left

    def additive(self):
        node = self.multiplicative()
        while True:
            token = self.peek()
            if token is None or token.kind != "op" or token.value not in ("+", "-"):
                return node
            self.take()
            self.count()
            node = ("binary", token.value, node, self.multiplicative())

    def multiplicative(self):
        node = self.unary()
        while True:
            token = self.peek()
            if token is None or token.kind != "op" or token.value not in ("*", "/", "%"):
                return node
            self.take()
            self.count()
            node = ("binary", token.value, node, self.unary())

    def unary(self):
        token = self.peek()
        if token is not None and token.kind == "op" and token.value == "-":
            self.take()
            self.count()
            return ("negate", self.unary())
        return self.primary()

    def primary(self):
        token = self.take()
        self.count()
        if token.kind == "number":
            return ("literal", token.value)
        if token.kind == "string":
            return ("literal", token.value)
        if token.kind == "op" and token.value == "(":
            node = self.comparison()
            self.expect_op(")")
            return node
        if token.kind == "name":
            lowered = token.value.lower()
            following = self.peek()
            if following is not None and following.kind == "op" and following.value == "(":
                if lowered not in _FUNCTIONS:
                    raise ExpressionError("{} is not a function this calculator knows".format(token.value))
                self.take()
                arguments = []
                if not (self.peek() and self.peek().kind == "op" and self.peek().value == ")"):
                    arguments.append(self.comparison())
                    while self.peek() and self.peek().kind == "op" and self.peek().value == ",":
                        self.take()
                        arguments.append(self.comparison())
                self.expect_op(")")
                minimum, maximum, _ = _FUNCTIONS[lowered]
                if not minimum <= len(arguments) <= maximum:
                    raise ExpressionError("{} takes between {} and {} arguments".format(
                        token.value, minimum, maximum))
                return ("call", lowered, arguments)
            if lowered not in self.fields:
                # The decisive check. A name that is not a field of this layer
                # cannot become one, which is what stops an expression from
                # reaching anything outside the row.
                raise ExpressionError("{} is not a field of this layer".format(token.value))
            return ("field", self.fields[lowered])
        raise ExpressionError("unexpected {!r}".format(token.value))


def compile_expression(source: str, fields: Sequence[str]):
    """Parse an expression against a layer's field list.

    Parsing is separate from evaluation so an expression is validated once and
    rejected before it touches a single row, rather than failing partway through
    a layer it has already half-modified.
    """
    text = str(source or "").strip()
    if not text:
        raise ExpressionError("the expression is empty")
    if len(text) > MAX_EXPRESSION_LENGTH:
        raise ExpressionError("the expression is too long")
    return _Parser(_tokenize(text), fields).parse()


def evaluate(node, row: Mapping[str, Any]) -> Any:
    """Evaluate a compiled expression against one row."""
    kind = node[0]
    if kind == "literal":
        return node[1]
    if kind == "field":
        return row.get(node[1])
    if kind == "negate":
        return -_number(evaluate(node[1], row))
    if kind == "binary":
        _, operator, left_node, right_node = node
        left = evaluate(left_node, row)
        right = evaluate(right_node, row)
        if operator == "+" and (isinstance(left, str) or isinstance(right, str)):
            return _text(left) + _text(right)
        left_value, right_value = _number(left), _number(right)
        if operator == "+":
            return left_value + right_value
        if operator == "-":
            return left_value - right_value
        if operator == "*":
            return left_value * right_value
        if operator in ("/", "%"):
            if right_value == 0:
                # Returning null rather than raising: one bad row must not
                # abandon a calculation over the whole layer, and a null is
                # visibly missing where a zero would be silently wrong.
                return None
            return left_value / right_value if operator == "/" else math.fmod(left_value, right_value)
    if kind == "compare":
        _, operator, left_node, right_node = node
        left = evaluate(left_node, row)
        right = evaluate(right_node, row)
        if left is None or right is None:
            return None
        if isinstance(left, str) or isinstance(right, str):
            left, right = _text(left), _text(right)
        else:
            left, right = _number(left), _number(right)
        return 1.0 if _COMPARISONS[operator](left, right) else 0.0
    if kind == "call":
        _, name, argument_nodes = node
        arguments = [evaluate(argument, row) for argument in argument_nodes]
        _, _, implementation = _FUNCTIONS[name]
        if name == "coalesce":
            return implementation(*arguments)
        try:
            return implementation(*arguments)
        except ExpressionError:
            raise
        except Exception as error:  # noqa: BLE001 - one row must not stop the layer
            raise ExpressionError("{} failed: {}".format(name, error)) from None
    raise ExpressionError("unknown expression node")


def referenced_fields(node) -> set:
    """Fields the expression reads, so a caller can check them before running."""
    kind = node[0]
    if kind == "field":
        return {node[1]}
    if kind == "literal":
        return set()
    if kind == "negate":
        return referenced_fields(node[1])
    if kind in ("binary", "compare"):
        return referenced_fields(node[2]) | referenced_fields(node[3])
    if kind == "call":
        found = set()
        for argument in node[2]:
            found |= referenced_fields(argument)
        return found
    return set()
