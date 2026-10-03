import re


class var:
    def __init__(self, name, multiplier=1, offset=0, exponent=1):
        self.name = name
        self.multiplier = multiplier
        self.offset = offset
        self.exponent = exponent

    def __add__(self, num):
        self.offset += num
        return self

    __radd__ = __add__

    def __sub__(self, num):
        self.offset -= num
        return self

    __rsub__ = __sub__

    def __mul__(self, num):
        self.multiplier = self.multiplier * num
        self.offset = self.offset * num
        return self

    __rmul__ = __mul__

    def __truediv__(self, num):
        self.multiplier = self.multiplier / num
        self.offset = self.offset / num
        return self

    __rtruediv__ = __truediv__

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


def checkFloat(string):
    try:
        float(string)
        return True
    except:
        return False


def mathParse(arr):
    for i, statement in enumerate(arr):
        if type(statement) == str:
            if statement.isalpha():
                statement = var(statement)
            if checkFloat(statement):
                statement = float(statement)
        arr[i] = statement
    return arr


def mathSolve(eq):
    while eq.count("*") > 0:
        before = list(eq)
        iMult = (i for i, v in enumerate(eq) if v == "*")
        for v in iMult:
            if type(eq[v - 1]) != str and type(eq[v + 1]) != str:
                an = genEq(eq[v - 1], eq[v], eq[v + 1])
                eq.pop(v - 1)
                eq.pop(v - 1)
                eq[v - 1] = an
            elif eq[v + 1] == "(":
                eq = handleBrackets(eq, v)
        if eq == before:
            raise ValueError("cannot parse equation")
    while eq.count("/") > 0:
        before = list(eq)
        iMult = (i for i, v in enumerate(eq) if v == "/")
        for v in iMult:
            if type(eq[v - 1]) != str and type(eq[v + 1]) != str:
                an = genEq(eq[v - 1], eq[v], eq[v + 1])
                eq.pop(v - 1)
                eq.pop(v - 1)
                eq[v - 1] = an
            elif eq[v - 1] == ")":
                eq = handleBrackets(eq, v, reverse=True)
            elif eq[v + 1] == "(":
                eq = handleBrackets(eq, v, reverse=False)
        if eq == before:
            raise ValueError("cannot parse equation")

    while eq.count("+") > 0:
        before = list(eq)
        iMult = (i for i, v in enumerate(eq) if v == "+")
        for v in iMult:
            if type(eq[v - 1]) != str and type(eq[v + 1]) != str:
                an = genEq(eq[v - 1], eq[v], eq[v + 1])
                eq.pop(v - 1)
                eq.pop(v - 1)
                eq[v - 1] = an
            elif eq[v + 1] == "(":
                eq = handleBrackets(eq, v)
        if eq == before:
            raise ValueError("cannot parse equation")
    while eq.count("-") > 0:
        before = list(eq)
        iMult = (i for i, v in enumerate(eq) if v == "-")
        for v in iMult:
            if type(eq[v - 1]) != str and type(eq[v + 1]) != str:
                an = genEq(eq[v - 1], eq[v], eq[v + 1])
                eq.pop(v - 1)
                eq.pop(v - 1)
                eq[v - 1] = an
            elif eq[v + 1] == "(":
                eq = handleBrackets(eq, v)
        if eq == before:
            raise ValueError("cannot parse equation")
    return eq


def genEq(val1, symbol, val2):
    if symbol == "*":
        ans = val1 * val2
    elif symbol == "/":
        ans = val1 / val2
    elif symbol == "+":
        ans = val1 + val2
    elif symbol == "-":
        ans = val1 - val2
    return ans


def handleBrackets(eq, v, reverse=False):
    if not reverse:
        o, k = findPar(eq, v + 1)
    else:
        o, k = rfindPar(eq, v - 1)

    m = mapInner(eq[o:k])
    uM = list(set(m))
    uM.sort()
    uM = uM[::-1]
    for u in uM:
        i = m.index(u)
        i += o
        k1, o1 = findPar(eq, i)
        s = mathSolve(eq[k1 + 1 : o1])
        rm = o1 - k1 + 1
        for _ in range(rm):
            eq.pop(k1)
        for n in s[::-1]:
            eq.insert(k1, n)
        # eq[k1]=s[0]
        if "(" in eq:
            if not reverse:
                o, k = findPar(eq, v + 1)
            else:
                o, k = rfindPar(eq, v - 1)
            m = mapInner(eq[o:k])

    return eq


def findPar(L, j):

    i = j
    if L[i] == "(":
        n = 1
    else:
        n = 0
    while True:
        j += 1
        if L[j] == "(":
            n += 1
        elif L[j] == ")":
            n -= 1

        if not n > 0:
            break
    return i, j


def rfindPar(L, j):
    i = j
    if L[i] == ")":
        n = 1
    else:
        n = 0
    while True:
        j -= 1
        if L[j] == ")":
            n += 1
        elif L[j] == "(":
            n -= 1

        if not n > 0:
            break
    return j, i


def mapInner(L):
    n = 0
    m = [0] * len(L)
    for i, v in enumerate(L):
        if v == "(":
            n += 1
        elif v == ")":
            n -= 1
        m[i] = n
    return m


def clearBrackets(eq):
    for i, c in enumerate(eq):
        if eq[i] == "(" and eq[i + 2] == ")":
            eq.pop(i)
            eq.pop(i + 1)
    return eq


def parse(string) -> var:
    string = "".join(string.lower().split())

    b = re.sub(r"(?<=[0-9])[\(]", "*(", string)  # replace implicit mult brackets
    b = re.sub(r"(?<=[0-9])[x]", "*x", b)  # replace implicit mult x
    b = re.split(r"([\*|\/|\+|\-|\(|\)])", b)

    b = [i for i in b if i]
    b = mathParse(b)
    a = mathSolve(b)
    return a[0]
