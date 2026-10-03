import ast
import math
import re


class var:
    """A linear calibration: value = multiplier * x + offset."""

    def __init__(self, name, multiplier=1, offset=0, exponent=1):
        self.name = name
        self.multiplier = multiplier
        self.offset = offset
        self.exponent = exponent

    def equation(self):
        if self.offset >= 0:
            sign = "+"
        else:
            sign = ""
        return "{}{}{}{}".format(self.multiplier, self.name, sign, self.offset)

    def apply(self, val):
        if type(val) != list:
            return val * self.multiplier + self.offset
        else:
            return [v * self.multiplier + self.offset for v in val]


def _linear(node):
    """Evaluate an expression node as (m, b), meaning m * x + b."""
    if isinstance(node, ast.Expression):
        return _linear(node.body)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return 0.0, float(node.value)
    if isinstance(node, ast.Name) and node.id == "x":
        return 1.0, 0.0
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        m, b = _linear(node.operand)
        return (-m, -b) if isinstance(node.op, ast.USub) else (m, b)
    if isinstance(node, ast.BinOp):
        (m1, b1), (m2, b2) = _linear(node.left), _linear(node.right)
        if isinstance(node.op, ast.Add):
            return m1 + m2, b1 + b2
        if isinstance(node.op, ast.Sub):
            return m1 - m2, b1 - b2
        if isinstance(node.op, ast.Mult) and m1 == 0:
            return b1 * m2, b1 * b2
        if isinstance(node.op, ast.Mult) and m2 == 0:
            return m1 * b2, b1 * b2
        if isinstance(node.op, ast.Div) and m2 == 0 and b2 != 0:
            return m1 / b2, b1 / b2
    raise ValueError("not a linear equation in x")


def parse(string) -> var:
    """
    Parses a calibration equation such as "2x+1", "1.5(x-3)" or "-0.5x+7".

    Numbers, x, + - * /, unary minus and brackets are allowed, with
    implicit multiplication (2x, 2(x+1), x(2), (x+1)2). Raises ValueError if
    the equation is not linear in x.
    """
    s = "".join(string.lower().split())
    # Short and arithmetic-only before ast.parse, which can crash on huge input
    if len(s) > 100 or not re.fullmatch(r"[0-9x.e+\-*/()]+", s):
        raise ValueError("cannot parse equation")
    s = re.sub(r"(?<=[0-9.)x])(?=[x(])", "*", s)  # 2x, 2(, )(, x(, )x
    s = re.sub(r"(?<=\))(?=[0-9.])", "*", s)  # )2
    try:
        tree = ast.parse(s, mode="eval")
    except SyntaxError as e:
        raise ValueError("cannot parse equation") from e
    m, b = _linear(tree)
    if not (math.isfinite(m) and math.isfinite(b)):
        raise ValueError("equation is not finite")
    return var("x", m, b)
