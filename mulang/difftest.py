"""Differential testing: the original Python is right for every valid input,
so a translation must agree with it on inputs beyond the cached asserts.

The inputs are the asserts' own arguments, mutated: an int moves within
the values seen in its place, a list changes length and takes elements
from the pool its place has seen, a grid stays rectangular, a string keeps
its alphabet. An input on which the original raises is out of the
constraints and is skipped. The results are compared, and so are the
arguments afterwards, for problems that change them in place.
"""

import ast
import json
import random
import subprocess
import sys

MAX_INPUT = 3000  # an assert whose arguments print longer is not a base


def calls(src, entries):
    """[(method, [argument source])] for every `sol.method(...)` in the asserts."""
    tail = src[src.index("\nsol = ") :] if "\nsol = " in src else ""
    out = []
    try:
        tree = ast.parse(tail)
    except SyntaxError:
        return out
    for n in ast.walk(tree):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "sol"
            and n.func.attr in entries
            and not n.keywords
        ):
            args = [ast.get_source_segment(tail, a) for a in n.args]
            if all(args) and sum(map(len, args)) < MAX_INPUT:
                out.append((n.func.attr, args))
    return out


def literal(text):
    """(value, wrap): the literal inside the argument and a function that
    writes a new one back, so build_tree([1, 2]) keeps build_tree."""
    try:
        return ast.literal_eval(text), repr
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        pass
    try:
        node = ast.parse(text, mode="eval").body
    except SyntaxError:
        return None, None
    if isinstance(node, ast.Call) and len(node.args) == 1 and not node.keywords:
        head = ast.unparse(node.func)
        try:
            inner = ast.literal_eval(node.args[0])
        except (ValueError, SyntaxError, TypeError):
            return None, None
        return inner, lambda v: f"{head}({v!r})"
    return None, None


class Pool:
    """Every value seen in one argument place, gathered by kind."""

    def __init__(self, values):
        self.ints, self.strs, self.chars, self.lens = [], [], set(), []
        self.floats = []
        for v in values:
            self.add(v)

    def add(self, v):
        if isinstance(v, bool):
            return
        if isinstance(v, int):
            self.ints.append(v)
        elif isinstance(v, float):
            self.floats.append(v)
        elif isinstance(v, str):
            self.strs.append(v)
            self.chars |= set(v)
        elif isinstance(v, (list, tuple)):
            self.lens.append(len(v))
            for x in v:
                self.add(x)


def mutate(v, pool, rng, depth=0):
    """A value like v: same type and shape family, parts drawn from pool."""
    if isinstance(v, bool):
        return rng.random() < 0.5
    if isinstance(v, int):
        lo = min(pool.ints + [v])
        hi = max(pool.ints + [v])
        choices = [v - 1, v + 1, v * 2, v // 2, lo, hi] + pool.ints[:50]
        return min(hi, max(lo, rng.choice(choices)))
    if isinstance(v, float):
        return rng.choice(pool.floats or [v]) * rng.choice([0.5, 1, 1.5])
    if isinstance(v, str):
        if not v or not pool.chars:
            return v
        chars = sorted(pool.chars)
        s = list(v)
        for _ in range(rng.randint(1, 3)):
            op = rng.random()
            if op < 0.5:
                s[rng.randrange(len(s))] = rng.choice(chars)
            elif op < 0.75 and len(s) > 1:
                s.pop(rng.randrange(len(s)))
            else:
                s.insert(rng.randrange(len(s) + 1), rng.choice(chars))
        return "".join(s)
    if isinstance(v, (list, tuple)):
        out = list(v)
        rect = out and all(isinstance(r, list) for r in out)
        rect = rect and len({len(r) for r in out}) == 1
        if rect and out[0] and not isinstance(out[0][0], list):
            cells = [x for r in out for x in r]
            grid = [list(r) for r in out]
            for _ in range(rng.randint(1, max(1, len(cells) // 3))):
                i, j = rng.randrange(len(grid)), rng.randrange(len(grid[0]))
                grid[i][j] = rng.choice(cells)
            square = len(grid) == len(grid[0])
            if rng.random() < 0.2 and len(grid) > 1 and not square:
                grid.pop()  # a square grid stays square: n x n problems
            return grid if isinstance(v, list) else tuple(grid)
        rows = out and all(isinstance(r, str) for r in out)
        if rows and len({len(r) for r in out}) == 1 and len(out) > 1:
            # a grid of strings: rows keep their length, cells change
            chars = sorted(set("".join(out)))
            grid = [list(r) for r in out]
            for _ in range(rng.randint(1, 3)):
                i, j = rng.randrange(len(grid)), rng.randrange(len(grid[0]))
                grid[i][j] = rng.choice(chars)
            return ["".join(r) for r in grid]
        if out:
            for _ in range(rng.randint(1, 3)):
                if not out:
                    break
                op = rng.random()
                k = rng.randrange(len(out))
                if op < 0.6:
                    out[k] = mutate(out[k], pool, rng, depth + 1)
                elif op < 0.8 and len(out) > min(pool.lens or [1]):
                    out.pop(k)
                elif len(out) < max(pool.lens or [len(out)]):
                    out.insert(k, mutate(out[k], pool, rng, depth + 1))
        return out if isinstance(v, list) else tuple(out)
    return v


def inputs(src, entries, n=150, seed=0):
    """Up to n [(method, [argument source])] made from the asserts."""
    base = calls(src, entries)
    rng = random.Random(seed)
    parsed = []
    for method, args in base:
        lits = [literal(a) for a in args]
        if lits and all(w for _, w in lits):
            parsed.append((method, lits))
    if not parsed:
        return []
    width = max(len(ls) for _, ls in parsed)
    pools = [Pool([ls[k][0] for _, ls in parsed if k < len(ls)]) for k in range(width)]
    out, seen = [], set()
    for _ in range(n * 3):
        method, lits = rng.choice(parsed)
        vals = [v for v, _ in lits]
        for _ in range(rng.randint(1, 2)):
            k = rng.randrange(len(vals))
            vals[k] = mutate(vals[k], pools[k], rng)
        args = [w(v) for v, (_, w) in zip(vals, lits)]
        key = (method, tuple(args))
        if key not in seen and sum(map(len, args)) < MAX_INPUT * 2:
            seen.add(key)
            out.append((method, args))
        if len(out) == n:
            break
    return out


RUNNER = r'''
import copy, math, signal, sys, json

def plain(x, depth=0):
    """A comparable copy: nodes become nested tuples, floats round."""
    if depth > 3000:
        return "deep"
    if isinstance(x, float):
        return round(x, 5)
    if isinstance(x, (list, tuple)):
        return [plain(v, depth + 1) for v in x]
    if isinstance(x, dict):
        return sorted((repr(k), plain(v, depth + 1)) for k, v in x.items())
    if isinstance(x, set):
        return sorted(repr(v) for v in x)
    if hasattr(x, "__dict__"):
        seen, out, node = set(), [], x
        while node is not None and id(node) not in seen and hasattr(node, "__dict__"):
            seen.add(id(node))
            d = node.__dict__
            if "next" in d and len(d) <= 3:  # a linked list
                out.append(plain(d.get("val"), depth + 1))
                node = d["next"]
                continue
            return [type(node).__name__] + [
                (k, plain(v, depth + 1)) for k, v in sorted(d.items())
            ]
        return out
    return x

class Late(Exception):
    pass

def late(*_):
    raise Late()

signal.signal(signal.SIGALRM, late)

def run(ns, method, args):
    try:
        signal.alarm(3)
        vals = [eval(a, ns) for a in args]
        got = getattr(ns["Solution"](), method)(*vals)
        return ("ok", plain(got), plain(vals))
    except Late:
        return ("late",)
    except BaseException as e:
        return ("raise", type(e).__name__)
    finally:
        signal.alarm(0)

A, B = {}, {}
exec(compile(ORIGINAL, "original", "exec"), A)
exec(compile(TRANSLATED, "translated", "exec"), B)
compared = 0
for method, args in CASES:
    a = run(A, method, args)
    if a[0] != "ok":
        continue
    b = run(B, method, args)
    compared += 1
    if a != b:
        print(json.dumps({"differs": [method, args], "original": repr(a)[:300], "mu": repr(b)[:300]}))
        sys.exit(0)
print(json.dumps({"compared": compared}))
'''


def differential(original, translated, cases, python, timeout=120):
    """Run both on every case: {"compared": n} or {"differs": ...}."""
    prelude = (
        f"ORIGINAL = {original!r}\nTRANSLATED = {translated!r}\nCASES = {cases!r}\n"
    )
    try:
        r = subprocess.run(
            [python, "-c", prelude + RUNNER],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    lines = r.stdout.strip().splitlines()
    if not lines:
        return {"error": (r.stderr.strip().splitlines() or ["?"])[-1][:200]}
    return json.loads(lines[-1])


def head(src):
    """The file up to the asserts: imports, helpers, the class."""
    return src[: src.index("\nsol = ")] if "\nsol = " in src else src


if __name__ == "__main__":
    sys.exit(__doc__)
