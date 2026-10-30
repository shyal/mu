"""Python to mu: a LeetCode Python solution written as mu, in as few lines
as the language allows.

python -m mulang.py2mu file.py          print the Solution class as mu
python -m mulang.py2mu --corpus [N ...] translate the cached solutions in
                                        the cache (MU_CACHE, else
                                        ./.prepare_cache), run each against
                                        its asserts, print the line counts

Every expression is checked as it is written: mu's own parser reads it
back, and the Python it gives must have the same syntax tree as the
original. A construct mu cannot say raises Untranslatable. Statements are
checked by the corpus run: the transpiled file must pass the asserts.
"""

import ast
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

from . import HELPERS, MuError, Parser, difftest, fmt, mu_type, profile, transpile
from .idioms import FoldBlock, Rules

# the cached problems of the project: <num>.json, each with "solution"
# and its asserts; the interpreter that runs the asserts
CACHE = Path(os.environ.get("MU_CACHE", ".prepare_cache"))
PYTHON = sys.executable


class Untranslatable(Exception):
    pass


# precedence, lowest first; an operand is bracketed when its own
# precedence is below what its place needs
LAMBDA, IFEXP, OR, AND, NOT, CMP, RANGE = 0, 1, 2, 3, 4, 5, 6
BOR, BXOR, BAND, SHIFT, ARITH, TERM, UNARY, POWER, POSTFIX, ATOM = range(7, 17)
FOLD = IFEXP  # a fold's value runs to the end of the expression

BINOPS = {
    ast.BitOr: ("|", BOR),
    ast.BitXor: ("^", BXOR),
    ast.BitAnd: ("&", BAND),
    ast.LShift: ("<<", SHIFT),
    ast.RShift: (">>", SHIFT),
    ast.Add: ("+", ARITH),
    ast.Sub: ("-", ARITH),
    ast.Mult: ("*", TERM),
    ast.Div: ("/", TERM),
    ast.FloorDiv: ("//", TERM),
    ast.Mod: ("%", TERM),
    ast.Pow: ("**", POWER),
}
CMPOPS = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.In: "in",
    ast.NotIn: "not in",
    ast.Is: "is",
    ast.IsNot: "is not",
}
UNARY_OPS = {ast.USub: "-", ast.Invert: "~", ast.Not: "not "}
CONSTANTS = {True: "true", False: "false", None: "none"}
CACHE_DECORATORS = {"cache", "lru_cache", "functools.cache", "functools.lru_cache"}
# decorators whose function takes no self
NO_SELF = {"staticmethod", "classmethod", "property"}


def call_name(node):
    return ast.unparse(node.func) if isinstance(node, ast.Call) else None


KEYED = ("sorted", "max", "min", "nlargest", "nsmallest", "groupby")


def indexed_by(key):
    """`values.__getitem__`, `lambda i: values[i]` and `_by(values)` are
    `by=values` in mu; any other key is itself."""
    if isinstance(key, ast.Attribute) and key.attr == "__getitem__":
        return key.value
    if (
        isinstance(key, ast.Lambda)
        and len(key.args.args) == 1
        and isinstance(key.body, ast.Subscript)
        and isinstance(key.body.slice, ast.Name)
        and key.body.slice.id == key.args.args[0].arg
    ):
        return key.body.value
    if isinstance(key, ast.Call) and call_name(key) == "_by" and len(key.args) == 1:
        return key.args[0]
    return key


def is_const(node, value):
    """True for the literal `value`; a negative one is written -1, which the
    parser reads as the negation of 1."""
    neg = isinstance(value, int) and value < 0
    if neg and isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return is_const(node.operand, -value)
    return isinstance(node, ast.Constant) and node.value == value


class Normalize(ast.NodeTransformer):
    """The Python rewrites mu makes, applied to the original before it is
    printed and compared: range(n) is range(0, n), float('inf') and math.inf
    are inf, self.name is name, ceil(a / b) is -(-a // b). self.f stays
    for an entry f, which remains a method."""

    def __init__(self, keep=()):
        self.keep = set(keep)
        self.attrs = set()  # the names self.name became

    def visit_Call(self, node):
        self.generic_visit(node)
        name = call_name(node)
        if name == "range" and len(node.args) == 1 and not node.keywords:
            node.args.insert(0, ast.Constant(0))
        if name == "float" and len(node.args) == 1:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and arg.value in ("inf", "-inf"):
                inf = ast.Name("inf", ast.Load())
                return inf if arg.value == "inf" else ast.UnaryOp(ast.USub(), inf)
        if name in ("ceil", "math.ceil") and len(node.args) == 1:
            arg = node.args[0]
            if isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Div):
                neg = ast.UnaryOp(ast.USub(), arg.left)
                div = ast.BinOp(neg, ast.FloorDiv(), arg.right)
                return ast.UnaryOp(ast.USub(), div)
        return node

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if (
            isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr not in self.keep
        ):
            self.attrs.add(node.attr)
            return ast.Name(node.attr, node.ctx)
        if ast.unparse(node) == "math.inf":
            return ast.Name("inf", node.ctx)
        return node


class Canon(ast.NodeTransformer):
    """What the comparison ignores: load or store, keyword order, the
    wrapper that lets mu's (a, b) -> e also take one pair, the 0 that
    visit_Call puts in front of range(n), which `for >i in xs` writes
    back as range(len(xs)), the list a fold hands to sum, max or min
    where the Python passed a generator, and the three spellings of a key
    that indexes (`_by(xs)`, `xs.__getitem__`, `lambda i: xs[i]`), which
    mu writes as `by=xs`."""

    @staticmethod
    def pair_lambda(node):
        """lambda *_a: (lambda a, b: e)(*(...)) is lambda a, b: e."""
        if not (isinstance(node, ast.Lambda) and node.args.vararg):
            return None
        if node.args.vararg.arg != "_a" or node.args.args:
            return None
        body = node.body
        if isinstance(body, ast.Call) and isinstance(body.func, ast.Lambda):
            return body.func
        return None

    @staticmethod
    def or_if(node):
        """The three spellings of `x or d if v`, as `d if x == v else x`
        with the walrus that mu writes removed."""
        if not isinstance(node, ast.IfExp):
            return None
        t = node.test
        if not isinstance(t, ast.Compare) or len(t.ops) != 1:
            return None
        op, left, right, body, orelse = (
            t.ops[0],
            t.left,
            t.comparators[0],
            node.body,
            node.orelse,
        )
        if isinstance(left, ast.NamedExpr) and left.target.id == "_v":
            left = left.value
            orelse = (
                left if isinstance(orelse, ast.Name) and orelse.id == "_v" else orelse
            )
        if isinstance(op, ast.Eq) and ast.dump(left) == ast.dump(orelse):
            x, d, v = left, body, right
        elif isinstance(op, ast.NotEq) and ast.dump(left) == ast.dump(body):
            x, d, v = left, orelse, right
        elif (
            isinstance(op, ast.Lt)
            and ast.dump(left) == ast.dump(body)
            and ast.dump(right) == "Name(id='inf', ctx=Load())"
        ):
            x, d, v = left, orelse, right
        else:
            return None
        return ast.IfExp(ast.Compare(x, [ast.Eq()], [v]), d, x)

    def generic_visit(self, node):
        node = super().generic_visit(node)
        inner = self.pair_lambda(node) or self.or_if(node)
        if inner:
            return inner
        if hasattr(node, "ctx"):
            node.ctx = ast.Load()
        if isinstance(node, ast.Call):
            node.keywords.sort(key=lambda k: k.arg or "")
            if call_name(node) == "range" and len(node.args) == 1:
                node.args.insert(0, ast.Constant(0))
            if call_name(node) in ("sum", "max", "min"):
                node.args = [
                    ast.GeneratorExp(a.elt, a.generators)
                    if isinstance(a, ast.ListComp)
                    else a
                    for a in node.args
                ]
            if call_name(node) in KEYED:
                for k in node.keywords:
                    if k.arg == "key":
                        k.value = indexed_by(k.value)
        return node


def canon(node):
    node = Canon().visit(ast.parse(ast.unparse(node), mode="eval").body)
    return ast.dump(node)


def mu_to_py(src):
    """The Python for one mu expression list, as mu's parser writes it."""
    p = Parser(src)
    out = p.exprlist()
    if p.peek()[0] != "NEWLINE":
        raise MuError(f"trailing {p.peek()[1]!r}")
    return out


def preloaded(s):
    """Every name the import binds is already a builtin, the same object:
    LeetCode preloads it (the repo's sitecustomize mirrors LeetCode)."""
    import builtins
    import importlib

    try:
        for a in s.names:
            if isinstance(s, ast.Import):
                got, bound = importlib.import_module(a.name), (a.asname or a.name)
                if a.asname is None and "." in a.name:
                    return False
            else:
                if a.name == "*" or s.level:
                    return False
                mod = importlib.import_module(s.module)
                got, bound = getattr(mod, a.name), a.asname or a.name
            if getattr(builtins, bound, None) is not got:
                return False
    except (ImportError, AttributeError):
        return False
    return True


class Printer:
    """Writes one method's body as mu lines."""

    def __init__(self):
        self.no_fold = 0  # inside a comprehension element: no bare fold

    # expressions

    def check(self, node, text, bare_tuple=False):
        """text must read back, through mu, as node."""
        try:
            py = mu_to_py(text)
        except MuError as e:
            raise Untranslatable(f"mu cannot read {text!r}: {e}") from None
        want = ast.Tuple(node.elts, ast.Load()) if bare_tuple else node
        got = ast.parse(f"({py})", mode="eval").body
        if canon(got) != canon(want):
            raise Untranslatable(f"{text!r} reads back as {py!r}")
        return text

    def top(self, node):
        """A whole expression: a statement's value, a return, an argument."""
        return self.check(node, self.expr(node, LAMBDA + 1))

    def bare(self, node):
        """A tuple without brackets, as after `return` or `=`."""
        if isinstance(node, ast.Tuple) and node.elts:
            # a fold or a conditional before the last item is bracketed
            last = len(node.elts) - 1
            text = ", ".join(
                self.expr(e, IFEXP if k == last else OR)
                for k, e in enumerate(node.elts)
            )
            return self.check(node, text, bare_tuple=True)
        return self.top(node)

    def expr(self, node, need):
        text, prec = self.form(node)
        return text if prec >= need else f"({text})"

    def form(self, node):
        """(mu text, precedence) for one Python expression."""
        method = getattr(self, "e_" + type(node).__name__, None)
        if method is None:
            raise Untranslatable(f"no mu form for {type(node).__name__}")
        return method(node)

    def e_Name(self, node):
        return node.id, ATOM

    def e_Constant(self, node):
        if node.value in CONSTANTS and type(node.value) in (bool, type(None)):
            return CONSTANTS[node.value], ATOM
        if node.value is Ellipsis:
            raise Untranslatable("no mu form for ...")
        text = repr(node.value)
        prec = UNARY if text.startswith("-") else ATOM
        return text, prec

    def e_JoinedStr(self, node):
        return ast.unparse(node), ATOM

    def e_BinOp(self, node):
        first = self.first(node)
        if first:
            return first, FOLD
        op, prec = BINOPS.get(type(node.op), (None, None))
        if op is None:
            raise Untranslatable(f"no mu form for {type(node.op).__name__}")
        if op == "**":  # right-associative: base is a postfix, exponent a unary
            left, right = self.expr(node.left, POSTFIX), self.expr(node.right, UNARY)
        else:
            left, right = self.expr(node.left, prec), self.expr(node.right, prec + 1)
        return f"{left} {op} {right}", prec

    def e_UnaryOp(self, node):
        if isinstance(node.op, ast.UAdd):
            raise Untranslatable("no mu form for unary +")
        if isinstance(node.op, ast.Not):
            return f"not {self.expr(node.operand, NOT)}", NOT
        # -(-a // b) is ceil(a / b)
        inner = node.operand
        if (
            isinstance(node.op, ast.USub)
            and isinstance(inner, ast.BinOp)
            and isinstance(inner.op, ast.FloorDiv)
            and isinstance(inner.left, ast.UnaryOp)
            and isinstance(inner.left.op, ast.USub)
        ):
            a = self.expr(inner.left.operand, TERM)
            b = self.expr(inner.right, UNARY)
            return f"ceil({a} / {b})", POSTFIX
        return UNARY_OPS[type(node.op)] + self.expr(inner, UNARY), UNARY

    def e_BoolOp(self, node):
        op, prec = ("and", AND) if isinstance(node.op, ast.And) else ("or", OR)
        return f" {op} ".join(self.expr(v, prec + 1) for v in node.values), prec

    def e_Compare(self, node):
        parts = [self.expr(node.left, RANGE)]
        for op, right in zip(node.ops, node.comparators):
            parts += [CMPOPS[type(op)], self.expr(right, RANGE)]
        return " ".join(parts), CMP

    def e_IfExp(self, node):
        found = self.or_if(node)
        if found:
            x, d, v = found
            return (
                f"{self.expr(x, AND)} or {self.expr(d, AND)} if {self.expr(v, OR)}",
                IFEXP,
            )
        test = self.expr(node.test, OR)
        body = self.expr(node.body, IFEXP)
        return f"{test} ? {body} : {self.expr(node.orelse, IFEXP)}", IFEXP

    def or_if(self, node):
        """(x, d, v) when node is `x if x != v else d`, `x if x < inf else d`
        or `d if x == v else x`: mu's `x or d if v`."""
        t = node.test
        if not isinstance(t, ast.Compare) or len(t.ops) != 1:
            return None
        op, (left, right) = t.ops[0], (t.left, t.comparators[0])
        same = ast.dump(left) == ast.dump(node.body)
        if same and isinstance(op, ast.NotEq):
            return left, node.orelse, right
        if (
            same
            and isinstance(op, ast.Lt)
            and isinstance(right, ast.Name)
            and right.id == "inf"
        ):
            return left, node.orelse, right
        if isinstance(op, ast.Eq) and ast.dump(left) == ast.dump(node.orelse):
            return left, node.body, right
        return None

    def e_Attribute(self, node):
        return f"{self.expr(node.value, POSTFIX)}.{node.attr}", POSTFIX

    def e_Subscript(self, node):
        value = self.expr(node.value, POSTFIX)
        return f"{value}[{self.index(node.slice)}]", POSTFIX

    def index(self, node):
        if isinstance(node, ast.Slice):
            parts = [node.lower, node.upper] + ([node.step] if node.step else [])
            return ":".join("" if p is None else self.expr(p, IFEXP) for p in parts)
        if isinstance(node, ast.Tuple) and node.elts:
            return ", ".join(self.index(e) for e in node.elts)
        return self.expr(node, IFEXP)

    def e_Starred(self, node):
        return f"*{self.expr(node.value, BOR)}", ATOM

    def e_Tuple(self, node):
        items = [self.expr(e, IFEXP) for e in node.elts]
        if len(items) == 1:
            return f"({items[0]},)", ATOM
        return f"({', '.join(items)})", ATOM

    def e_List(self, node):
        return "[" + ", ".join(self.expr(e, IFEXP) for e in node.elts) + "]", ATOM

    def e_Set(self, node):
        return "{" + ", ".join(self.expr(e, IFEXP) for e in node.elts) + "}", ATOM

    def e_Dict(self, node):
        if any(k is None for k in node.keys):
            raise Untranslatable("no mu form for **mapping in a dict")
        items = [
            f"{self.expr(k, IFEXP)}: {self.expr(v, IFEXP)}"
            for k, v in zip(node.keys, node.values)
        ]
        return "{" + ", ".join(items) + "}", ATOM

    def element(self, node):
        """A comprehension's element, which a `for` follows."""
        self.no_fold += 1
        try:
            return self.expr(node, IFEXP)
        finally:
            self.no_fold -= 1

    def comprehension(self, gens):
        out = ""
        for g in gens:
            if g.is_async:
                raise Untranslatable("no mu form for async for")
            names, it = self.loop_head(g.target, g.iter)
            out += f" for {names} in {self.expr(it, OR)}"
            out += "".join(f" if {self.expr(c, OR)}" for c in g.ifs)
        return out

    def e_ListComp(self, node):
        return f"[{self.element(node.elt)}{self.comprehension(node.generators)}]", ATOM

    def e_SetComp(self, node):
        return (
            f"{{{self.element(node.elt)}{self.comprehension(node.generators)}}}",
            ATOM,
        )

    def e_DictComp(self, node):
        pair = f"{self.element(node.key)}: {self.element(node.value)}"
        return f"{{{pair}{self.comprehension(node.generators)}}}", ATOM

    def e_GeneratorExp(self, node):
        return f"({self.element(node.elt)}{self.comprehension(node.generators)})", ATOM

    def e_Lambda(self, node):
        raise Untranslatable("a lambda outside a call's arguments")

    def lambda_(self, node):
        a = node.args
        if a.vararg or a.kwarg or a.kwonlyargs or a.defaults or a.posonlyargs:
            raise Untranslatable("no mu form for this lambda's parameters")
        body = self.expr(node.body, IFEXP)
        if len(a.args) == 1:
            return f"{a.args[0].arg} -> {body}"
        if any(p.arg == "_" for p in a.args):
            raise Untranslatable("mu renames _ among a lambda's names")
        return f"({', '.join(p.arg for p in a.args)}) -> {body}"

    def arg(self, node):
        if isinstance(node, ast.Lambda):
            return self.lambda_(node)
        return self.expr(node, IFEXP)

    def e_Call(self, node):
        name = call_name(node)
        if any(k.arg is None for k in node.keywords):
            raise Untranslatable("no mu form for **kwargs in a call")
        fold = self.fold(node, name)
        if fold:
            return fold, FOLD
        rng = self.range_(node, name)
        if rng:
            return rng, RANGE
        func = self.expr(node.func, POSTFIX)
        kwargs = {k.arg: k.value for k in node.keywords}
        if name in KEYED and "key" in kwargs:
            if name == "sorted":
                func = "sort"
            kwargs = {
                "by" if k == "key" else k: indexed_by(v) if k == "key" else v
                for k, v in kwargs.items()
            }
        elif (
            name == "list"
            and len(node.args) == 1
            and not kwargs
            and isinstance(node.args[0], ast.Call)
            and ast.unparse(node.args[0].func) == "filter"
            and len(node.args[0].args) == 2
            and not node.args[0].keywords
        ):
            f, xs = node.args[0].args
            return f"filter({self.arg(xs)}, by={self.arg(f)})", POSTFIX
        elif name == "Counter":
            func = "counter"
        elif name == "defaultdict" and len(node.args) == 1 and not kwargs:
            return "{:" + self.arg(node.args[0]) + "}", ATOM
        elif name == "accumulate" and not kwargs and len(node.args) in (1, 2):
            op = ast.unparse(node.args[1]) if len(node.args) == 2 else "+"
            if op in ("+", "operator.mul"):
                sym = "+" if op == "+" else "*"
                return f"scan({sym}, {self.expr(node.args[0], IFEXP)})", POSTFIX
        if (
            len(node.args) == 1
            and not kwargs
            and isinstance(node.args[0], ast.GeneratorExp)
        ):
            gen = node.args[0]
            inner = f"{self.element(gen.elt)}{self.comprehension(gen.generators)}"
            return f"{func}({inner})", POSTFIX
        parts = [self.arg(a) for a in node.args]
        parts += [f"{k}={self.arg(v)}" for k, v in kwargs.items()]
        return f"{func}({', '.join(parts)})", POSTFIX

    def fold(self, node, name):
        """sum(e for x in xs if c) is `sum for x in xs if c: e`; count for
        sum(1 for ...)."""
        if name not in ("sum", "max", "min") or self.no_fold:
            return None
        start = ""
        if name in ("max", "min") and len(node.args) == 2 and not node.keywords:
            # max(s, max(gen, default=s)) is `max from s for ...`
            s, inner = node.args
            kw = inner.keywords if isinstance(inner, ast.Call) else []
            if (
                call_name(inner) == name
                and len(inner.args) == 1
                and len(kw) == 1
                and kw[0].arg == "default"
                and ast.dump(kw[0].value) == ast.dump(s)
            ):
                start = f" from {self.expr(s, ARITH)}"
                node = ast.Call(inner.func, inner.args, [])
        if len(node.args) != 1 or node.keywords:
            return None
        gen = node.args[0]
        if not isinstance(gen, ast.GeneratorExp) or len(gen.generators) != 1:
            return None
        g = gen.generators[0]
        if len(g.ifs) > 1 or g.is_async:
            return None
        names, it = self.loop_head(g.target, g.iter)
        where = f" if {self.expr(g.ifs[0], OR)}" if g.ifs else ""
        head = f"for {names} in {self.expr(it, OR)}{where}"
        if name == "sum" and is_const(gen.elt, 1) and type(gen.elt.value) is int:
            return f"count {head}"
        return f"{name}{start} {head}: {self.expr(gen.elt, IFEXP)}"

    def first(self, node):
        """lo + bisect_left(range(lo, hi), True, key=lambda k: p) is
        `first k in lo..<hi if p`."""
        if not (isinstance(node.op, ast.Add) and isinstance(node.right, ast.Call)):
            return None
        c = node.right
        if call_name(c) != "bisect_left" or len(c.args) != 2 or len(c.keywords) != 1:
            return None
        rng, key = c.args[0], c.keywords[0]
        if (
            call_name(rng) != "range"
            or len(rng.args) != 2
            or not is_const(c.args[1], True)
        ):
            return None
        if ast.dump(rng.args[0]) != ast.dump(node.left) or key.arg != "key":
            return None
        fn = key.value
        if not isinstance(fn, ast.Lambda) or len(fn.args.args) != 1:
            return None
        lo, hi = self.expr(rng.args[0], BOR), self.expr(rng.args[1], BOR)
        k = fn.args.args[0].arg
        return f"first {k} in {lo}..<{hi} if {self.expr(fn.body, IFEXP)}"

    def range_(self, node, name):
        """range(a, b + 1) is a..b, range(a, b) is a..<b."""
        if name != "range" or len(node.args) != 2 or node.keywords:
            return None
        lo, hi = node.args
        lo_text = self.expr(lo, BOR)
        if (
            isinstance(hi, ast.BinOp)
            and isinstance(hi.op, ast.Add)
            and is_const(hi.right, 1)
            and self.form(hi.left)[1] >= ARITH
        ):
            return f"{lo_text}..{self.expr(hi.left, BOR)}"
        return f"{lo_text}..<{self.expr(hi, BOR)}"

    def loop_head(self, target, it):
        """`for i, x in enumerate(xs)` is `for >i, x in xs`; `for i in
        range(len(xs))` is `for >i in xs`; the same loops right to left,
        `range(len(xs) - 1, -1, -1)` and its zip with `reversed(xs)`, are
        `for <i in xs` and `for <i, x in xs`; a pair unpacks without
        brackets."""
        enum = call_name(it) == "enumerate" and len(it.args) == 1 and not it.keywords
        if isinstance(target, ast.Tuple) and len(target.elts) == 2:
            i, x = target.elts
            if isinstance(i, ast.Name) and enum:
                return f">{i.id}, {self.pattern(x)}", it.args[0]
            if isinstance(i, ast.Name) and self.indices_back_of(it, x) is not None:
                return f"<{i.id}, {self.pattern(x)}", self.indices_back_of(it, x)
        if isinstance(target, ast.Name) and self.indices_of(it) is not None:
            return f">{target.id}", self.indices_of(it)
        if isinstance(target, ast.Name) and self.indices_back_of(it) is not None:
            return f"<{target.id}", self.indices_back_of(it)
        if isinstance(target, ast.Tuple):
            return ", ".join(self.pattern(e) for e in target.elts), it
        return self.pattern(target), it

    def indices_of(self, it):
        """xs when it is range(len(xs)); visit_Call has made it range(0, len(xs))."""
        if call_name(it) != "range" or len(it.args) != 2 or it.keywords:
            return None
        lo, hi = it.args
        if not is_const(lo, 0) or call_name(hi) != "len" or len(hi.args) != 1:
            return None
        return hi.args[0] if not hi.keywords else None

    def indices_back_of(self, it, item=None):
        """xs when it is range(len(xs) - 1, -1, -1) or reversed(range(len(xs))),
        or, with an item, zip(range(len(xs) - 1, -1, -1), reversed(xs))."""
        if item is not None:
            if call_name(it) != "zip" or len(it.args) != 2 or it.keywords:
                return None
            rng, rev = it.args
            xs = self.indices_back_of(rng)
            if xs is None or call_name(rev) != "reversed" or len(rev.args) != 1:
                return None
            return xs if ast.unparse(rev.args[0]) == ast.unparse(xs) else None
        if call_name(it) == "reversed" and len(it.args) == 1 and not it.keywords:
            return self.indices_of(it.args[0])
        if call_name(it) != "range" or len(it.args) != 3 or it.keywords:
            return None
        hi, stop, step = it.args
        if not is_const(stop, -1) or not is_const(step, -1):
            return None
        if not (isinstance(hi, ast.BinOp) and isinstance(hi.op, ast.Sub)):
            return None
        if not is_const(hi.right, 1) or call_name(hi.left) != "len":
            return None
        return (
            hi.left.args[0] if len(hi.left.args) == 1 and not hi.left.keywords else None
        )

    def pattern(self, node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Tuple):
            return "(" + ", ".join(self.pattern(e) for e in node.elts) + ")"
        raise Untranslatable(f"no mu loop target for {ast.unparse(node)}")

    # statements

    def block(self, stmts, ind, tail=False):
        """mu lines for a block; with tail, the last statement's value is
        the function's: `return e` drops its `return`."""
        out = []
        stmts = [
            s
            for s in stmts
            if not isinstance(s, (ast.Pass, ast.Nonlocal))
            and not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
        ]
        for k, s in enumerate(stmts):
            last = tail and k == len(stmts) - 1
            out += self.stmt(s, ind, last)
        return out or ["  " * ind + "pass"]

    def stmt(self, s, ind, last):
        pad = "  " * ind
        kind = type(s).__name__
        if isinstance(s, ast.Return):
            if s.value is None:
                return [] if last else [pad + "return"]
            return [pad + ("" if last else "return ") + self.bare(s.value)]
        if isinstance(s, ast.FunctionDef):
            return self.def_(s, ind)
        if isinstance(s, FoldBlock):
            names, it = self.loop_head(s.target, s.iter)
            start = f" from {self.expr(s.start, ARITH)}" if s.start is not None else ""
            head = f"{pad}{s.op}{start} for {names} in {self.expr(it, OR)}"
            body = self.block(s.body, ind + 1) if s.body else []
            return [head] + body + [f"{pad}  {self.top(s.value)}"]
        if isinstance(s, ast.If):
            return self.if_(s, ind, last)
        if isinstance(s, (ast.For, ast.While)):
            other = [f"{pad}else"] + self.block(s.orelse, ind + 1) if s.orelse else []
            if isinstance(s, ast.While):
                head = f"{pad}while {self.top(s.test)}"
            else:
                names, it = self.loop_head(s.target, s.iter)
                into = "" if names == "_" else f"{names} in "
                head = f"{pad}for {into}{self.check(it, self.expr(it, LAMBDA + 1))}"
            return [head] + self.block(s.body, ind + 1) + other
        if isinstance(s, ast.Assign):
            if len(s.targets) == 1 and isinstance(s.value, ast.Lambda):
                return self.lambda_def(s, ind)
            lhs = " = ".join(self.bare(t) for t in s.targets)
            return [f"{pad}{lhs} = {self.value(s.value)}"]
        if isinstance(s, ast.AnnAssign):
            if s.value is None:
                return []
            return [f"{pad}{self.bare(s.target)} = {self.value(s.value)}"]
        if isinstance(s, ast.AugAssign):
            op = BINOPS[type(s.op)][0]
            return [f"{pad}{self.bare(s.target)} {op}= {self.value(s.value)}"]
        if isinstance(s, ast.Expr) and isinstance(s.value, ast.Yield):
            v = s.value.value
            return [pad + "yield" + (f" {self.bare(v)}" if v else "")]
        if isinstance(s, ast.Expr) and isinstance(s.value, ast.YieldFrom):
            return [f"{pad}yield from {self.top(s.value.value)}"]
        if isinstance(s, ast.Expr):
            return [pad + self.expr_stmt(s.value)]
        if isinstance(s, (ast.Break, ast.Continue)):
            return [pad + kind.lower()]
        if isinstance(s, ast.Delete):
            return [pad + "del " + ", ".join(self.top(t) for t in s.targets)]
        if isinstance(s, ast.Assert):
            msg = f", {self.top(s.msg)}" if s.msg else ""
            return [f"{pad}assert {self.top(s.test)}{msg}"]
        if isinstance(s, (ast.Import, ast.ImportFrom)):
            return [] if preloaded(s) else [pad + ast.unparse(s)]
        raise Untranslatable(f"no mu form for {kind}")

    def value(self, node):
        """An assignment's right side: `x = stack .` pops."""
        pop = self.pop(node)
        return pop if pop else self.bare(node)

    def pop(self, node):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "pop"
            and not node.args
            and not node.keywords
        ):
            return f"{self.expr(node.func.value, POSTFIX)} ."
        return None

    def expr_stmt(self, node):
        """`xs.append(x)` is `xs <- x`, `xs.pop()` is `xs .`."""
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
            and len(node.args) == 1
            and not node.keywords
        ):
            target = self.expr(node.func.value, POSTFIX)
            return f"{target} <- {self.expr(node.args[0], IFEXP)}"
        return self.pop(node) or self.top(node)

    def if_(self, s, ind, last):
        pad = "  " * ind
        out = [f"{pad}if {self.top(s.test)}"] + self.block(s.body, ind + 1, last)
        orelse = s.orelse
        while len(orelse) == 1 and isinstance(orelse[0], ast.If):
            s = orelse[0]
            out += [f"{pad}elif {self.top(s.test)}"] + self.block(s.body, ind + 1, last)
            orelse = s.orelse
        if orelse:
            out += [f"{pad}else"] + self.block(orelse, ind + 1, last)
        return out

    def lambda_def(self, s, ind):
        """`f = lambda x, y: e` is a def."""
        target, fn = s.targets[0], s.value
        if not isinstance(target, ast.Name) or fn.args.defaults or fn.args.vararg:
            raise Untranslatable("no mu form for this lambda")
        params = ", ".join(a.arg for a in fn.args.args)
        body = self.top(fn.body)
        return [f"{'  ' * ind}def {target.id}({params})", f"{'  ' * (ind + 1)}{body}"]

    def params(self, fn, typed):
        a = fn.args
        if a.vararg or a.kwarg or a.kwonlyargs or a.posonlyargs:
            raise Untranslatable(f"no mu form for the parameters of {fn.name}")
        defaults = [None] * (len(a.args) - len(a.defaults)) + a.defaults
        out = []
        for p, d in zip(a.args, defaults):
            if p.arg == "self":
                continue
            quoted = isinstance(p.annotation, ast.Constant)  # not defined yet
            t = mu_type(p.annotation) if typed and not quoted else None
            head = p.arg if t is None else f"{p.arg}: {t}"
            out.append(head if d is None else f"{head}={self.arg(d)}")
        return ", ".join(out)

    def def_(self, fn, ind, typed=False):
        pad = "  " * ind
        cached = [d for d in fn.decorator_list if self.is_cache(d)]
        others = [d for d in fn.decorator_list if not self.is_cache(d)]
        # mu writes the cache nearest the def, and gives every method a self
        if fn.decorator_list != others + cached or any(
            ast.unparse(d) in NO_SELF for d in others
        ):
            raise Untranslatable(f"no mu form for the decorators of {fn.name}")
        above = [f"{pad}@{self.top(d)}" for d in others]
        if cached:
            memo = self.memo(fn, ind)
            if memo:
                return above + memo
        ret = mu_type(fn.returns) if typed else None
        if cached:  # a body the case form cannot say: memo's block form
            head = f"{pad}memo {fn.name}({self.params(fn, False)})"
        else:
            head = f"{pad}def {fn.name}({self.params(fn, typed)})"
            head += f" -> {ret}" if ret else ""
        return above + [head] + self.block(fn.body, ind + 1, tail=True)

    def is_cache(self, d):
        if isinstance(d, ast.Call):
            args = d.args + [k.value for k in d.keywords]
            unbounded = all(is_const(a, None) for a in args)
            return ast.unparse(d.func) in CACHE_DECORATORS and unbounded
        return ast.unparse(d) in CACHE_DECORATORS

    def memo(self, fn, ind):
        """A cached function whose body is only `if g: return v` cases and a
        last `return v` is a mu memo."""
        cases, body = [], list(fn.body)
        while body and isinstance(body[0], ast.If):
            s = body.pop(0)
            arms = []
            while True:
                if len(s.body) != 1 or not isinstance(s.body[0], ast.Return):
                    return None
                arms.append((s.test, s.body[0].value))
                if len(s.orelse) == 1 and isinstance(s.orelse[0], ast.If):
                    s = s.orelse[0]
                    continue
                break
            cases += arms
            if s.orelse:
                if len(s.orelse) != 1 or not isinstance(s.orelse[0], ast.Return):
                    return None
                body.insert(0, s.orelse[0])
                break
        if (
            len(body) != 1
            or not isinstance(body[0], ast.Return)
            or body[0].value is None
        ):
            return None
        if any(v is None for _, v in cases):
            return None
        self.params(fn, False)  # plain names only
        pad = "  " * ind
        params = ", ".join(a.arg for a in fn.args.args)
        if not cases:
            return [f"{pad}memo {fn.name}({params}) = {self.top(body[0].value)}"]
        out = [f"{pad}memo {fn.name}({params}) ="]
        for test, val in cases:
            out.append(f"{pad}  | {self.top(test)} -> {self.bare(val)}")
        out.append(f"{pad}  | else -> {self.bare(body[0].value)}")
        return out


def target_names(node):
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        return set().union(*(target_names(e) for e in node.elts))
    if isinstance(node, ast.Starred):
        return target_names(node.value)
    return set()


def own_nodes(stmts):
    """Every node of a block, not entering nested defs, lambdas or classes."""
    todo = list(stmts)
    while todo:
        n = todo.pop()
        yield n
        if not isinstance(n, (ast.FunctionDef, ast.Lambda, ast.ClassDef)):
            todo.extend(ast.iter_child_nodes(n))


def mu_bound(fn):
    """The names mu counts as bound by a def: parameters, assignment and
    loop targets, nested def names."""
    out = {a.arg for a in fn.args.args}
    for n in own_nodes(fn.body):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                out |= target_names(t)
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign, ast.For)):
            out |= target_names(n.target)
        elif isinstance(n, ast.FunctionDef):
            out.add(n.name)
    return out


def py_locals(fn):
    """Names a nested def assigns that Python keeps local to it."""
    out, shared = set(), set()
    for n in own_nodes(fn.body):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                out |= target_names(t)
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign)):
            out |= target_names(n.target)
        elif isinstance(n, (ast.Nonlocal, ast.Global)):
            shared |= set(n.names)
    return out - shared - {a.arg for a in fn.args.args}


def unshadow(fn, scopes=(), shared=frozenset()):
    """mu makes a nested def's assignment to a name its enclosing def binds
    nonlocal; Python makes it local. Rename such a local: left is left_.
    shared names were attributes, self.name, and stay shared."""
    if scopes:
        clash = py_locals(fn) & set().union(*scopes) - shared
        taken = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        for name in sorted(clash):
            new = name + "_"
            while new in taken or any(new in s for s in scopes):
                new += "_"
            taken.add(new)
            for n in ast.walk(fn):
                if isinstance(n, ast.Name) and n.id == name:
                    n.id = new
    inner = scopes + (mu_bound(fn),)
    for n in own_nodes(fn.body):
        if isinstance(n, ast.FunctionDef):
            unshadow(n, inner, shared)


def mu_calls():
    """Names mu gives a meaning when called."""
    return {h for h in HELPERS if not h.startswith("_")} | {
        "sort", "filter", "scan", "counter", "ceil", "floor", "deep", "Grid"
    }  # fmt: skip


def rename_helper_clashes(fn):
    """A name the solution binds that mu also calls, such as a list called
    cells next to a cells(...) the idioms wrote, is renamed: cells_."""
    bound = (
        mu_bound(fn)
        | {n.name for n in ast.walk(fn) if isinstance(n, ast.FunctionDef)}
        | {n.arg for n in ast.walk(fn) if isinstance(n, ast.arg)}
    )
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            bound.add(n.id)
    called = {
        n.func.id
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    for name in sorted(bound & mu_calls() & called):
        new = name + "_"
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and n.id == name and not getattr(n, "mu", False):
                n.id = new
            elif isinstance(n, ast.arg) and n.arg == name:
                n.arg = new
            elif isinstance(n, ast.FunctionDef) and n.name == name:
                n.name = new


def solution_class(tree):
    for n in tree.body:
        if isinstance(n, ast.ClassDef) and n.name == "Solution":
            return n
    raise Untranslatable("no class Solution")


def translate(src, entries=None, title=None, square=False, fired=None):
    """The Solution class in src, as mu. entries are the methods the
    asserts call; any other method becomes a def nested in each entry."""
    tree = ast.parse(src)
    cls = solution_class(tree)
    names = [n.name for n in cls.body if isinstance(n, ast.FunctionDef)]
    norm = Normalize(entries or names[:1])
    cls = norm.visit(cls)
    if cls.bases:
        raise Untranslatable("Solution has a base class")
    fns = [n for n in cls.body if isinstance(n, ast.FunctionDef)]
    attrs = [n for n in cls.body if isinstance(n, (ast.Assign, ast.AnnAssign))]
    other = [
        n
        for n in cls.body
        if n not in fns
        and n not in attrs
        and not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))
    ]
    if other:
        raise Untranslatable(f"class body holds a {type(other[0]).__name__}")
    if any(f.name == "__init__" for f in fns):
        raise Untranslatable("Solution has __init__")
    entries = entries or [fns[0].name]
    tops = [f for f in fns if f.name in entries]
    helpers = [f for f in fns if f.name not in entries]
    if not tops:
        raise Untranslatable("the asserts call no method of Solution")
    if helpers and len(tops) > 1:
        raise Untranslatable("helper methods shared by several entries")
    p = Printer()
    out = [f"# {title}"] if title else []
    out.append("class Solution")
    for k, fn in enumerate(tops):
        # class attributes and helper methods move inside the entry
        fn.body = attrs + helpers + fn.body
        got = Rules(fn, tree, square).run()
        if fired is not None:
            fired.update(got)
        # an attribute the entry never sets is bound in it, so the helpers share it
        unset = sorted(norm.attrs - mu_bound(fn) - {f.name for f in helpers})
        fn.body = [
            ast.Assign([ast.Name(a, ast.Store())], ast.Constant(None), lineno=0)
            for a in unset
        ] + fn.body
        unshadow(fn, shared=frozenset(norm.attrs))
        rename_helper_clashes(fn)
        out += ([""] if k else []) + p.def_(fn, 1, typed=True)
    return fmt("\n".join(out).strip("\n") + "\n")


def code_lines(text, comment="#"):
    return sum(
        1
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith(comment)
    )


def python_lines(src):
    """Lines of the Solution class, less blanks, comments and docstrings."""
    tree = ast.parse(src)
    cls = solution_class(tree)
    lines = src.splitlines()[cls.lineno - 1 : cls.end_lineno]
    doc = set()
    for n in ast.walk(cls):
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant):
            doc |= set(range(n.lineno, n.end_lineno + 1))
    kept = [
        ln
        for k, ln in enumerate(lines, cls.lineno)
        if k not in doc and ln.strip() and not ln.strip().startswith("#")
    ]
    return len(kept)


# the corpus


def run(code, timeout=30):
    try:
        r = subprocess.run(
            [PYTHON, "-c", code], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return "timeout"
    return "" if r.returncode == 0 else (r.stderr.strip().splitlines() or ["?"])[-1]


def one(num, diff=False):
    """Translate cached problem num and run it against its asserts; with
    diff, also against the original on mutated inputs (difftest.py)."""
    data = json.loads((CACHE / f"{num}.json").read_text())
    src = data.get("solution") or ""
    row = {"num": num, "title": data.get("title", "")}
    try:
        tree = ast.parse(src)
        solution_class(tree)  # skip a file without one
    except (SyntaxError, Untranslatable) as e:
        return {**row, "status": "skip", "why": str(e)}
    entries = sorted(set(re.findall(r"\bsol\.(\w+)\(", src)))
    title = f"{num}. {row['title']}"
    doc = ast.get_docstring(tree) or ""
    square = bool(re.search(r"\b([nN]) ?[x*\u00d7] ?\1\b", doc))
    fired = Counter()
    try:
        mu_src = translate(src, entries, title, square, fired)
        py = transpile(mu_src)
    except (Untranslatable, MuError, RecursionError) as e:
        return {**row, "status": "untranslatable", "why": str(e)[:200]}
    row |= {"py_lines": python_lines(src), "mu_lines": code_lines(mu_src), "mu": mu_src}
    row["fired"] = dict(fired)
    return {**row, **check(src, entries, py, diff)}


def check(src, entries, py, diff=False):
    """{"status": ...} for the transpiled py in place of src's Solution:
    pass, fail (with why), or skip when the original fails too. With
    diff, a passing one also carries the differential result."""
    cls = solution_class(ast.parse(src))
    lines = src.splitlines(keepends=True)
    start = cls.lineno - 1 - len(cls.decorator_list)
    code = "".join(lines[:start]) + py + "\n" + "".join(lines[cls.end_lineno :])
    err = run(code)
    if not err:
        out = {"status": "pass"}
        if diff:
            try:
                cases = difftest.inputs(src, entries)
            except (ValueError, TypeError, IndexError, RecursionError):
                cases = []
            out["diff"] = difftest.differential(
                difftest.head(src), difftest.head(code), cases, PYTHON
            )
        return out
    if run(src):
        return {"status": "skip", "why": "the Python fails its own asserts"}
    return {"status": "fail", "why": err[:200]}


def corpus(nums, jobs=8, out=None, diff=False):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(jobs) as pool:
        rows = list(pool.map(lambda n: one(n, diff), nums))
    if out:
        Path(out).write_text(json.dumps(rows, indent=1))
    by = {}
    for r in rows:
        by.setdefault(r["status"], []).append(r)
    total = len(rows) - len(by.get("skip", []))
    print(
        f"{total} solutions translated from Python to mu and run against their asserts."
    )
    for status in ("pass", "fail", "untranslatable"):
        n = len(by.get(status, []))
        print(f"{n} of them ({100 * n / max(total, 1):.0f}%) {status_words[status]}.")
    ok = by.get("pass", [])
    if ok:
        py = sum(r["py_lines"] for r in ok)
        mu = sum(r["mu_lines"] for r in ok)
        print(
            f"The passing ones went from {py} lines of Python to {mu} lines of mu ({100 * mu / py:.0f}%)."
        )
    if diff:
        d = [r.get("diff", {}) for r in ok]
        agree = sum(1 for x in d if x.get("compared"))
        differs = [r for r in ok if "differs" in r.get("diff", {})]
        none = len(ok) - agree - len(differs)
        print(
            f"On mutated inputs, {agree} of the passing ones agree with the original,"
            f" {len(differs)} differ, and {none} had no input to compare."
        )
        for r in differs:
            print(f"  {r['num']}. {r['title']}: {r['diff']['differs'][1]}")
    reasons = {}
    for r in by.get("untranslatable", []) + by.get("fail", []):
        key = re.sub(r"'[^']*'|\"[^\"]*\"|\d+", "_", r["why"])[:90]
        reasons[key] = reasons.get(key, 0) + 1
    for why, n in sorted(reasons.items(), key=lambda kv: -kv[1])[:25]:
        print(f"{n:5}  {why}")
    return rows


status_words = {
    "pass": "pass",
    "fail": "fail their asserts",
    "untranslatable": "use something mu cannot say",
}


if __name__ == "__main__":
    args = sys.argv[1:]
    profile()
    if args[:1] == ["--corpus"]:
        nums = [int(a) for a in args[1:] if a.isdigit()]
        if not nums:
            nums = sorted(int(p.stem) for p in CACHE.glob("*.json") if p.stem.isdigit())
        out = next((a.split("=", 1)[1] for a in args if a.startswith("--out=")), None)
        corpus(nums, out=out, diff="--diff" in args)
    elif len(args) == 1:
        src = Path(args[0]).read_text()
        entries = sorted(set(re.findall(r"\bsol\.(\w+)\(", src)))
        try:
            sys.stdout.write(translate(src, entries or None))
        except Untranslatable as e:
            sys.exit(f"{args[0]}: {e}")
    else:
        sys.exit(__doc__)
