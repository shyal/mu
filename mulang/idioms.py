"""Python idioms rewritten as the mu helpers and folds, for py2mu.

Each rule reads a few statements of one block and, when they have the
exact shape it knows, returns the Python that says the same thing with a
helper: two range loops over a grid become `for (r, c) in cells(grid)`.
py2mu prints the result as mu. The rules keep Python's meaning; the corpus
run checks them against every cached problem's asserts.
"""

import ast
from collections import Counter

PURE = {
    "len", "abs", "min", "max", "sum", "ord", "chr", "int", "str", "float",
    "bool", "tuple", "divmod", "pow", "bin", "hex", "range", "sorted", "set",
    "list", "zip", "enumerate", "reversed", "any", "all", "gcd", "sqrt", "isqrt",
}  # fmt: skip
CARDINALS = {(-1, 0), (1, 0), (0, -1), (0, 1)}
COMPARE_KWARGS = {
    ast.Eq: "eq",
    ast.Lt: "lt",
    ast.LtE: "lte",
    ast.Gt: "gt",
    ast.GtE: "gte",
}
FLIPPED = {
    ast.Eq: ast.Eq,
    ast.Lt: ast.Gt,
    ast.LtE: ast.GtE,
    ast.Gt: ast.Lt,
    ast.GtE: ast.LtE,
}


def same(a, b):
    return a is not None and b is not None and ast.dump(a) == ast.dump(b)


def name(n):
    return n.id if isinstance(n, ast.Name) else None


def call(fn, *args, **kwargs):
    """A call the rules write; its name is marked as mu's, not the solution's."""
    func = ast.Name(fn, ast.Load())
    func.mu = True
    return ast.Call(func, list(args), [ast.keyword(k, v) for k, v in kwargs.items()])


def load(n):
    return ast.Name(n, ast.Load())


def pair(a, b, ctx=None):
    return ast.Tuple([a, b], ctx or ast.Load())


def uses(nodes, ident):
    return any(
        isinstance(n, ast.Name) and n.id == ident
        for node in nodes
        for n in ast.walk(node)
    )


def loaded(nodes):
    return {
        n.id
        for node in nodes
        for n in ast.walk(node)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }


def free_reads(stmts):
    """Names stmts may read before binding them. A loop's target and an
    assignment's plain names are bound for what follows; a name bound only
    inside an if or a loop body is not."""
    bound, out = set(), set()
    for s in stmts:
        if isinstance(s, ast.For):
            out |= loaded([s.iter]) - bound
            inner = bound | {
                n.id for n in ast.walk(s.target) if isinstance(n, ast.Name)
            }
            out |= free_reads(s.body) - inner
            out |= free_reads(s.orelse) - bound
            bound = inner
        elif isinstance(s, ast.Assign) and all(
            isinstance(t, (ast.Name, ast.Tuple)) for t in s.targets
        ):
            out |= loaded([s.value]) - bound
            for t in s.targets:
                bound |= {n.id for n in ast.walk(t) if isinstance(n, ast.Name)}
        else:
            out |= loaded([s]) - bound
            if isinstance(s, ast.AugAssign):
                out |= {
                    n.id for n in ast.walk(s.target) if isinstance(n, ast.Name)
                } - bound
    return out


def breaks(stmts):
    """A break or continue that belongs to the loop around stmts."""
    todo = list(stmts)
    while todo:
        n = todo.pop()
        if isinstance(n, (ast.Break, ast.Continue)):
            return True
        if not isinstance(n, (ast.For, ast.While, ast.FunctionDef, ast.Lambda)):
            todo.extend(ast.iter_child_nodes(n))
    return False


def has_break(stmts):
    todo = list(stmts)
    while todo:
        n = todo.pop()
        if isinstance(n, ast.Break):
            return True
        if not isinstance(n, (ast.For, ast.While, ast.FunctionDef, ast.Lambda)):
            todo.extend(ast.iter_child_nodes(n))
    return False


def range_args(node):
    """(lo, hi) of range(lo, hi); Normalize has made range(n) range(0, n)."""
    if (
        isinstance(node, ast.Call)
        and name(node.func) == "range"
        and len(node.args) == 2
        and not node.keywords
    ):
        return node.args
    return None


def is_int(node, v):
    return (
        isinstance(node, ast.Constant) and type(node.value) is int and node.value == v
    )


def plain_value(node):
    """A value safe to repeat in every cell: a constant, inf, -inf."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Name) and node.id in ("inf", "None", "True", "False"):
        return True
    return isinstance(node, ast.UnaryOp) and plain_value(node.operand)


def conjuncts(test):
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        return [c for v in test.values for c in conjuncts(v)]
    return [test]


def join_and(parts):
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else ast.BoolOp(ast.And(), parts)


def assigned_once(fn):
    """Names bound exactly once in fn, with the value bound: {name: value}.
    A tuple assignment `m, n = len(g), len(g[0])` binds each part."""
    counts, values = Counter(), {}
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                names = [t] if isinstance(t, ast.Name) else getattr(t, "elts", [])
                vals = [n.value] if isinstance(t, ast.Name) else None
                if isinstance(t, ast.Tuple) and isinstance(n.value, ast.Tuple):
                    if len(n.value.elts) == len(t.elts):
                        vals = n.value.elts
                for k, tn in enumerate(names):
                    if isinstance(tn, ast.Name):
                        counts[tn.id] += 1
                        if vals:
                            values[tn.id] = vals[k]
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign, ast.For)):
            for tn in ast.walk(n.target):
                if isinstance(tn, ast.Name):
                    counts[tn.id] += 2
        elif isinstance(n, ast.arg):
            counts[n.arg] += 2
        elif isinstance(n, (ast.FunctionDef, ast.comprehension)):
            if isinstance(n, ast.FunctionDef):
                counts[n.name] += 2
            else:
                for tn in ast.walk(n.target):
                    if isinstance(tn, ast.Name):
                        counts[tn.id] += 2
    return {k: v for k, v in values.items() if counts[k] == 1}


class Rules:
    """The idiom rules for one method. fired counts which ones applied."""

    def __init__(self, fn, module, square=False):
        self.fn = fn
        self.square = square
        self.fired = Counter()
        self.once = assigned_once(fn)
        self.module = {}
        for s in module.body:
            if isinstance(s, ast.Assign) and len(s.targets) == 1:
                if isinstance(s.targets[0], ast.Name):
                    self.module[s.targets[0].id] = s.value

    # sizes of a grid

    def rows_of(self, node):
        """The grid whose row count node is: len(g), or a name bound once to it."""
        if isinstance(node, ast.Name) and node.id in self.once:
            node = self.once[node.id]
        if (
            isinstance(node, ast.Call)
            and name(node.func) == "len"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Name)
        ):
            return node.args[0].id
        return None

    def cols_of(self, node):
        """The grid whose column count node is: len(g[0]), or a name bound
        once to it. When the statement says the grid is square, a row count
        will do."""
        if isinstance(node, ast.Name) and node.id in self.once:
            node = self.once[node.id]
        if (
            isinstance(node, ast.Call)
            and name(node.func) == "len"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Subscript)
            and is_int(node.args[0].slice, 0)
            and isinstance(node.args[0].value, ast.Name)
        ):
            return node.args[0].value.id
        return self.rows_of(node) if self.square else None

    def constant(self, node):
        if isinstance(node, ast.Name):
            node = self.once.get(node.id) or self.module.get(node.id)
        return node

    # driving

    def run(self):
        for _ in range(6):
            before = ast.dump(self.fn)
            self.fn.body = self.block(self.fn.body, tail=True)
            Exprs(self).visit(self.fn)
            self.fn.body = self.dead(self.fn.body)
            self.once = assigned_once(self.fn)
            if ast.dump(self.fn) == before:
                break
        self.fn.body = self.merged(self.fn.body)
        return self.fired

    def block(self, stmts, tail=False):
        for s in stmts:
            for field in ("body", "orelse"):
                inner = getattr(s, field, None)
                if isinstance(inner, list) and inner and isinstance(inner[0], ast.stmt):
                    fn_tail = isinstance(s, ast.FunctionDef)
                    arm_tail = tail and isinstance(s, ast.If)
                    setattr(s, field, self.block(inner, fn_tail or arm_tail))
        rules = [
            self.comprehension,
            self.counter,
            self.fold,
            self.fold_block,
            self.cells,
            self.pairs,
            self.nbrs,
            self.levels,
            self.enumerate_,
            self.first_true,
            self.if_assign,
            self.adjacency,
            self.inline_temp,
            self.else_after_exit,
        ]
        changed = True
        while changed:
            changed = False
            for i in range(len(stmts)):
                for rule in rules:
                    got = rule(stmts, i, tail)
                    if got:
                        new, used = got
                        stmts = stmts[:i] + new + stmts[i + used :]
                        self.fired[rule.__name__.strip("_")] += 1
                        changed = True
                        break
                if changed:
                    break
        return self.ternary(stmts, tail)

    # rules: each takes (stmts, i, tail) and returns (new stmts, how many
    # statements they replace), or None

    def comprehension(self, stmts, i, tail):
        """xs = [] and a loop that only appends is a list comprehension;
        likewise a set with add and a dict with d[k] = v."""
        if i + 1 >= len(stmts):
            return None
        init, loop = stmts[i], stmts[i + 1]
        if not (isinstance(init, ast.Assign) and len(init.targets) == 1):
            return None
        acc = name(init.targets[0])
        kind = self.empty_collection(init.value)
        if not acc or not kind or not isinstance(loop, ast.For):
            return None
        gens, cur = [], loop
        while True:
            if isinstance(cur, ast.For) and not cur.orelse and len(cur.body) == 1:
                gens.append(ast.comprehension(cur.target, cur.iter, [], 0))
                cur = cur.body[0]
            elif (
                isinstance(cur, ast.If)
                and not cur.orelse
                and len(cur.body) == 1
                and gens
            ):
                gens[-1].ifs.append(cur.test)
                cur = cur.body[0]
            else:
                break
        leaf = self.collect(cur, acc, kind)
        if leaf is None:
            return None
        parts = [g.iter for g in gens] + [c for g in gens for c in g.ifs] + list(leaf)
        if uses(parts, acc):
            return None
        targets = {
            n.id for g in gens for n in ast.walk(g.target) if isinstance(n, ast.Name)
        }
        if targets & free_reads(stmts[i + 2 :]):
            return None  # the loop variable is read after the loop
        if kind == "list":
            value = ast.ListComp(leaf[0], gens)
        elif kind == "set":
            value = ast.SetComp(leaf[0], gens)
        else:
            value = ast.DictComp(leaf[0], leaf[1], gens)
        return [ast.Assign([init.targets[0]], value, lineno=0)], 2

    def empty_collection(self, node):
        if isinstance(node, ast.List) and not node.elts:
            return "list"
        if isinstance(node, ast.Dict) and not node.keys:
            return "dict"
        if isinstance(node, ast.Call) and not node.args and not node.keywords:
            return {"set": "set", "list": "list", "dict": "dict"}.get(name(node.func))
        return None

    def collect(self, s, acc, kind):
        """The element(s) s adds to acc: xs.append(e), s.add(e), d[k] = v.
        An if whose two arms each append is one element, a if c else b."""
        if (
            kind == "list"
            and isinstance(s, ast.If)
            and len(s.body) == 1
            and len(s.orelse) == 1
        ):
            a = self.collect(s.body[0], acc, kind)
            b = self.collect(s.orelse[0], acc, kind)
            if a and b:
                value = ast.IfExp(s.test, a[0], b[0])
                if len(ast.unparse(value)) <= 60:
                    return (value,)
            return None
        if kind in ("list", "set") and isinstance(s, ast.Expr):
            c = s.value
            verb = "append" if kind == "list" else "add"
            if (
                isinstance(c, ast.Call)
                and isinstance(c.func, ast.Attribute)
                and c.func.attr == verb
                and name(c.func.value) == acc
                and len(c.args) == 1
                and not c.keywords
                and not isinstance(c.args[0], ast.Starred)
            ):
                return (c.args[0],)
        if kind == "dict" and isinstance(s, ast.Assign) and len(s.targets) == 1:
            t = s.targets[0]
            if isinstance(t, ast.Subscript) and name(t.value) == acc:
                if not isinstance(t.slice, ast.Slice):
                    return (t.slice, s.value)
        return None

    def counter(self, stmts, i, tail):
        """d = defaultdict(int) and a loop of d[x] += 1 is Counter(xs)."""
        if i + 1 >= len(stmts):
            return None
        init, loop = stmts[i], stmts[i + 1]
        if not (isinstance(init, ast.Assign) and len(init.targets) == 1):
            return None
        acc = name(init.targets[0])
        v = init.value
        fresh = (isinstance(v, ast.Dict) and not v.keys) or (
            isinstance(v, ast.Call)
            and not v.keywords
            and (
                (
                    name(v.func) == "defaultdict"
                    and len(v.args) == 1
                    and name(v.args[0]) == "int"
                )
                or (name(v.func) == "Counter" and not v.args)
            )
        )
        if not (acc and fresh and isinstance(loop, ast.For) and not loop.orelse):
            return None
        if len(loop.body) != 1:
            return None
        s = loop.body[0]
        key = None
        if (
            isinstance(s, ast.AugAssign)
            and isinstance(s.op, ast.Add)
            and is_int(s.value, 1)
            and isinstance(s.target, ast.Subscript)
            and name(s.target.value) == acc
        ):
            key = s.target.slice
            if isinstance(v, ast.Dict):
                return None  # d[x] += 1 on a plain dict raises
        elif (
            isinstance(s, ast.Assign)
            and len(s.targets) == 1
            and isinstance(s.targets[0], ast.Subscript)
            and name(s.targets[0].value) == acc
        ):
            key = s.targets[0].slice
            want = ast.BinOp(
                ast.Call(
                    ast.Attribute(load(acc), "get", ast.Load()),
                    [key, ast.Constant(0)],
                    [],
                ),
                ast.Add(),
                ast.Constant(1),
            )
            if not same(Canon(s.value), Canon(want)):
                return None
        if key is None or uses([key, loop.iter], acc):
            return None
        if {
            n.id for n in ast.walk(loop.target) if isinstance(n, ast.Name)
        } & free_reads(stmts[i + 2 :]):
            return None
        if same(Canon(key), Canon(loop.target)):
            arg = loop.iter
        else:
            arg = ast.GeneratorExp(
                key, [ast.comprehension(loop.target, loop.iter, [], 0)]
            )
        return [ast.Assign([init.targets[0]], call("Counter", arg), lineno=0)], 2

    def accumulation(self, s, acc):
        """(op, value) when s is acc += e, acc = max(acc, e), acc = min(acc, e)."""
        if (
            isinstance(s, ast.AugAssign)
            and isinstance(s.op, ast.Add)
            and name(s.target) == acc
        ):
            return "sum", s.value
        if (
            isinstance(s, ast.Assign)
            and len(s.targets) == 1
            and name(s.targets[0]) == acc
            and isinstance(s.value, ast.Call)
            and name(s.value.func) in ("max", "min")
            and len(s.value.args) == 2
            and not s.value.keywords
        ):
            a, b = s.value.args
            if name(a) == acc:
                return s.value.func.id, b
            if name(b) == acc:
                return s.value.func.id, a
        return None

    def fold_head(self, stmts, i):
        """(acc, start, loop) for acc = start followed by a for loop."""
        if i + 1 >= len(stmts):
            return None
        init, loop = stmts[i], stmts[i + 1]
        if not (isinstance(init, ast.Assign) and len(init.targets) == 1):
            return None
        acc = name(init.targets[0])
        if not acc or not isinstance(loop, ast.For) or loop.orelse:
            return None
        if uses([init.value, loop.iter, loop.target], acc):
            return None
        return acc, init.value, loop

    def fold(self, stmts, i, tail):
        """acc = 0 and a loop whose only statement is acc += e (under at
        most one if) is acc = sum(e for ...); count for += 1; max and min
        from acc = max(acc, e)."""
        head = self.fold_head(stmts, i)
        if not head:
            return None
        acc, start, loop = head
        body, ifs = loop.body, []
        if len(body) == 1 and isinstance(body[0], ast.If) and not body[0].orelse:
            ifs, body = [body[0].test], body[0].body
        if len(body) != 1:
            return None
        got = self.accumulation(body[0], acc)
        if not got or uses(ifs + [got[1]], acc):
            return None
        op, value = got
        gen = ast.GeneratorExp(
            value, [ast.comprehension(loop.target, loop.iter, ifs, 0)]
        )
        if op == "sum":
            if not is_int(start, 0):
                return None
            new = call("sum", gen)
        else:
            if not plain_value(start):
                return None
            new = call(op, start, call(op, gen, default=start))
        return [ast.Assign([stmts[i].targets[0]], new, lineno=0)], 2

    def fold_block(self, stmts, i, tail):
        """acc = start, a loop that ends with acc += e, then `return acc`
        as the method's last line: a fold with a block."""
        head = self.fold_head(stmts, i)
        if not head or not tail or i + 3 != len(stmts):
            return None
        acc, start, loop = head
        ret = stmts[i + 2]
        if not (isinstance(ret, ast.Return) and name(ret.value) == acc):
            return None
        got = self.accumulation(loop.body[-1], acc)
        if not got or len(loop.body) < 2 or uses(loop.body[:-1] + [got[1]], acc):
            return None
        op, value = got
        if op == "sum" and is_int(start, 0):
            start = None
        elif not plain_value(start):
            return None
        return [FoldBlock(op, start, loop.target, loop.iter, loop.body[:-1], value)], 3

    def ternary(self, stmts, tail):
        """if c: return a, then return b, as the last lines: return a if c
        else b. x = e, then return x: return e."""
        while len(stmts) >= 2:
            s, last = stmts[-2], stmts[-1]
            if (
                isinstance(s, ast.Assign)
                and len(s.targets) == 1
                and name(s.targets[0])
                and isinstance(last, ast.Return)
                and name(last.value) == s.targets[0].id
            ):
                stmts = stmts[:-2] + [ast.Return(s.value)]
                self.fired["inline return"] += 1
                continue
            s, last = stmts[-2], stmts[-1]
            if not (
                isinstance(s, ast.If)
                and not s.orelse
                and len(s.body) == 1
                and isinstance(s.body[0], ast.Return)
                and s.body[0].value is not None
                and isinstance(last, ast.Return)
                and last.value is not None
            ):
                break
            value = ast.IfExp(s.test, s.body[0].value, last.value)
            if len(ast.unparse(value)) > 70:
                break
            stmts = stmts[:-2] + [ast.Return(value)]
            self.fired["ternary"] += 1
        return stmts

    def adjacency(self, stmts, i, tail):
        """g = defaultdict(list) (or [[] for _ in range(n)]), indeg = [0] * n,
        and a loop over edges that only appends and counts, are
        g = adjacency(edges, ...) and indeg = indegrees(edges, ...)."""
        for k in (2, 1):
            if i + k >= len(stmts):
                continue
            inits, loop = stmts[i : i + k], stmts[i + k]
            if not (isinstance(loop, ast.For) and not loop.orelse):
                continue
            t = loop.target
            if not (
                isinstance(t, ast.Tuple)
                and len(t.elts) == 2
                and all(name(e) for e in t.elts)
            ):
                continue
            u, v = t.elts[0].id, t.elts[1].id
            kinds = {}
            for s in inits:
                if not (
                    isinstance(s, ast.Assign)
                    and len(s.targets) == 1
                    and name(s.targets[0])
                ):
                    break
                kind = self.edge_store(s.value)
                if not kind:
                    break
                kinds[s.targets[0].id] = kind
            if len(kinds) != k:
                continue
            got = self.edge_body(loop.body, u, v, kinds)
            if not got or uses([loop.iter], u) or uses([loop.iter], v):
                continue
            if {u, v} & free_reads(stmts[i + k + 1 :]):
                continue
            out = []
            for acc, (kind, size) in kinds.items():
                flags = got.get(acc)
                if flags is None:
                    break
                args = [loop.iter] + ([size] if size is not None else [])
                kw = {}
                if kind == "adj":
                    fwd, back = flags
                    if back and not fwd:
                        kw["reverse"] = ast.Constant(True)
                    elif fwd and back:
                        kw["directed"] = ast.Constant(False)
                    value = call("adjacency", *args, **kw)
                else:
                    fwd, back = flags
                    if fwd and back:
                        kw["directed"] = ast.Constant(False)
                    elif back:
                        kw["reverse"] = ast.Constant(True)
                    if size is not None:
                        kw["type"] = load("list")
                    value = call("indegrees", *args, **kw)
                out.append(ast.Assign([ast.Name(acc, ast.Store())], value, lineno=0))
            else:
                return out, k + 1
        return None

    def edge_store(self, v):
        """("adj", n) for defaultdict(list) or [[] for _ in range(n)];
        ("deg", n) for [0] * n or defaultdict(int). n is None for a dict."""
        if (
            isinstance(v, ast.Call)
            and name(v.func) == "defaultdict"
            and len(v.args) == 1
        ):
            return {"list": ("adj", None), "int": ("deg", None)}.get(name(v.args[0]))
        if isinstance(v, ast.ListComp) and len(v.generators) == 1:
            g = v.generators[0]
            a = range_args(g.iter)
            if (
                isinstance(v.elt, ast.List)
                and not v.elt.elts
                and a
                and is_int(a[0], 0)
                and not g.ifs
            ):
                return ("adj", a[1])
        if (
            isinstance(v, ast.BinOp)
            and isinstance(v.op, ast.Mult)
            and isinstance(v.left, ast.List)
            and len(v.left.elts) == 1
            and is_int(v.left.elts[0], 0)
        ):
            return ("deg", v.right)
        return None

    def edge_body(self, body, u, v, kinds):
        """{acc: (forward, backward)}: which ways each store is filled."""
        got = {acc: [False, False] for acc in kinds}
        for s in body:
            hit = None
            if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call):
                c = s.value
                if (
                    isinstance(c.func, ast.Attribute)
                    and c.func.attr == "append"
                    and len(c.args) == 1
                    and isinstance(c.func.value, ast.Subscript)
                ):
                    acc, key, val = (
                        name(c.func.value.value),
                        name(c.func.value.slice),
                        name(c.args[0]),
                    )
                    if kinds.get(acc, ("",))[0] == "adj" and (key, val) in (
                        (u, v),
                        (v, u),
                    ):
                        hit = acc, (key, val) == (v, u)
            elif (
                isinstance(s, ast.AugAssign)
                and isinstance(s.op, ast.Add)
                and is_int(s.value, 1)
                and isinstance(s.target, ast.Subscript)
            ):
                acc, key = name(s.target.value), name(s.target.slice)
                if kinds.get(acc, ("",))[0] == "deg" and key in (u, v):
                    hit = acc, key == u
            if not hit or got[hit[0]][hit[1]]:
                return None
            got[hit[0]][hit[1]] = True
        return (
            {acc: tuple(f) for acc, f in got.items() if any(f)}
            if all(any(f) for f in got.values())
            else None
        )

    def inline_temp(self, stmts, i, tail):
        """x = e read once, in the next statement, is e written there. Only
        when e is pure and the next statement evaluates the place once."""
        if i + 1 >= len(stmts):
            return None
        s, nxt = stmts[i], stmts[i + 1]
        if not (
            isinstance(s, ast.Assign) and len(s.targets) == 1 and name(s.targets[0])
        ):
            return None
        x = s.targets[0].id
        if x not in self.once or not self.pure_call(s.value):
            return None
        reads = [
            n
            for n in ast.walk(self.fn)
            if isinstance(n, ast.Name) and n.id == x and isinstance(n.ctx, ast.Load)
        ]
        if len(reads) != 1:
            return None
        once = self.evaluated_once(nxt)
        if once is None or not all(self.pure_call(e) for e in once):
            return None
        spot = None
        for e in once:
            spot = spot or self.find_plain(e, reads[0])
        if spot is None:
            return None
        value = s.value
        if not isinstance(
            value, (ast.Name, ast.Constant, ast.Call, ast.Subscript, ast.Attribute)
        ):
            value = ast.parse(f"({ast.unparse(value)})", mode="eval").body
        self.put(spot, value)
        if len(ast.unparse(nxt).split("\n")[0]) > 76:
            self.put(spot, reads[0])  # too long: put the name back
            return None
        return [nxt], 2

    @staticmethod
    def put(spot, value):
        parent, field, k = spot
        if k is None:
            setattr(parent, field, value)
        else:
            getattr(parent, field)[k] = value

    def pure_call(self, node):
        """No call but to a builtin that reads, so moving it changes nothing."""
        for n in ast.walk(node):
            if isinstance(n, (ast.Lambda, ast.NamedExpr, ast.Yield, ast.Await)):
                return False
            if isinstance(n, ast.Call) and name(n.func) not in PURE:
                return False
        return True

    def evaluated_once(self, s):
        """The expressions a statement evaluates exactly once, before it
        changes anything; None for a statement that is not simple."""
        if isinstance(s, ast.Assign):
            return [s.value] + s.targets
        if isinstance(s, ast.AugAssign):
            return [s.target, s.value]
        if isinstance(s, (ast.Return, ast.Expr)) and s.value is not None:
            return [s.value]
        if isinstance(s, ast.If):
            return [s.test]
        if isinstance(s, ast.For):
            return [s.iter]
        return None

    def find_plain(self, node, target):
        """(parent, field, index) of target inside node, outside any
        comprehension or lambda, where it would be evaluated again."""
        for parent in ast.walk(node):
            if isinstance(
                parent,
                (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp),
            ):
                if any(n is target for n in ast.walk(parent)):
                    return None
            for field, value in ast.iter_fields(parent):
                if value is target:
                    return parent, field, None
                if isinstance(value, list):
                    for k, v in enumerate(value):
                        if v is target:
                            return parent, field, k
        return None

    def else_after_exit(self, stmts, i, tail):
        """if c: ... return, then else: rest, is the if and then rest."""
        s = stmts[i]
        if not (isinstance(s, ast.If) and s.orelse and s.body):
            return None
        if len(s.orelse) == 1 and isinstance(s.orelse[0], ast.If):
            return None  # an elif: nothing saved
        if not isinstance(s.body[-1], (ast.Return, ast.Continue, ast.Break, ast.Raise)):
            return None
        return [ast.If(s.test, s.body, [], lineno=0)] + s.orelse, 1

    def if_assign(self, stmts, i, tail):
        """if c: x = a else: x = b is x = a if c else b; also with +=."""
        s = stmts[i]
        if not (isinstance(s, ast.If) and len(s.body) == 1 and len(s.orelse) == 1):
            return None
        a, b = s.body[0], s.orelse[0]
        if isinstance(b, ast.If):
            got = self.if_assign([b], 0, tail)
            if not got:
                return None
            b = got[0][0]
        if type(a) is not type(b) or not isinstance(a, (ast.Assign, ast.AugAssign)):
            return None
        if isinstance(a, ast.Assign):
            if len(a.targets) != 1 or len(b.targets) != 1:
                return None
            ta, tb = a.targets[0], b.targets[0]
        else:
            if type(a.op) is not type(b.op):
                return None
            ta, tb = a.target, b.target
        if not (isinstance(ta, ast.Name) and same(Canon(ta), Canon(tb))):
            return None
        value = ast.IfExp(s.test, a.value, b.value)
        if len(ast.unparse(value)) > 60:
            return None
        if isinstance(a, ast.Assign):
            return [ast.Assign([ta], value, lineno=0)], 1
        return [ast.AugAssign(ta, a.op, value, lineno=0)], 1

    def merged(self, stmts):
        """Consecutive assignments of independent values are one line:
        q, seen = deque(), set(); a = b = 0 when the values are one constant."""
        for s in stmts:
            for field in ("body", "orelse"):
                inner = getattr(s, field, None)
                if isinstance(inner, list) and inner and isinstance(inner[0], ast.stmt):
                    setattr(s, field, self.merged(inner))
        out, group = [], []

        def flush():
            if len(group) > 1:
                self.fired["merged"] += len(group) - 1
                out.append(self.merge(group))
            else:
                out.extend(group)
            group.clear()

        for s in stmts:
            parts = self.parts(s)
            if parts is None:
                flush()
                out.append(s)
                continue
            names = {t.id for t, _ in parts}
            earlier = {t.id for g in group for t, _ in self.parts(g)}
            clash = names & earlier or loaded([v for _, v in parts]) & earlier
            if group and (clash or len(ast.unparse(self.merge(group + [s]))) > 70):
                flush()
            group.append(s)
        flush()
        return out

    def parts(self, s):
        """[(name, value)] for x = e, a, b = e1, e2, or x += 1 (a number:
        += on a list extends it in place, which x = x + ys does not)."""
        if (
            isinstance(s, ast.AugAssign)
            and isinstance(s.target, ast.Name)
            and isinstance(s.value, ast.Constant)
            and type(s.value.value) in (int, float)
        ):
            t = s.target
            return [(t, ast.BinOp(ast.Name(t.id, ast.Load()), s.op, s.value))]
        if not (isinstance(s, ast.Assign) and len(s.targets) == 1):
            return None
        t, v = s.targets[0], s.value
        if isinstance(v, ast.Lambda) or getattr(v, "mu_keep", False):
            return None
        if isinstance(t, ast.Name):
            return [(t, v)]
        if (
            isinstance(t, ast.Tuple)
            and isinstance(v, ast.Tuple)
            and len(t.elts) == len(v.elts)
            and all(isinstance(e, ast.Name) for e in t.elts)
            and not any(isinstance(e, ast.Starred) for e in v.elts)
        ):
            return list(zip(t.elts, v.elts))
        return None

    def merge(self, group):
        parts = [p for g in group for p in self.parts(g)]
        values = [v for _, v in parts]
        const = all(
            isinstance(v, ast.Constant)
            and type(v.value) in (int, str, bool, type(None))
            for v in values
        )
        if const and len({repr(v.value) + type(v.value).__name__ for v in values}) == 1:
            targets = [ast.Name(t.id, ast.Store()) for t, _ in parts]
            return ast.Assign(targets, values[0], lineno=0)
        target = ast.Tuple([ast.Name(t.id, ast.Store()) for t, _ in parts], ast.Store())
        return ast.Assign([target], ast.Tuple(values, ast.Load()), lineno=0)

    def two_loops(self, stmts, i):
        """(outer, inner, body) for a for loop whose whole body is a for loop."""
        outer = stmts[i]
        if not (
            isinstance(outer, ast.For) and not outer.orelse and len(outer.body) == 1
        ):
            return None
        inner = outer.body[0]
        if not (isinstance(inner, ast.For) and not inner.orelse):
            return None
        if not (
            isinstance(outer.target, ast.Name) and isinstance(inner.target, ast.Name)
        ):
            return None
        return outer, inner, inner.body

    def cells(self, stmts, i, tail):
        """for r in range(len(g)): for c in range(len(g[0])) is
        for (r, c) in cells(g); a lone `if g[r][c] == v` becomes eq=v."""
        loops = self.two_loops(stmts, i)
        if not loops:
            return None
        outer, inner, body = loops
        grid, start = self.grid_ranges(outer, inner)
        if not grid or has_break(body):
            return None
        kwargs = {"start": ast.Constant(1)} if start else {}
        body = self.cell_filter(body, grid, outer.target.id, inner.target.id, kwargs)
        target = pair(outer.target, inner.target, ast.Store())
        return [
            ast.For(target, call("cells", load(grid), **kwargs), body, [], lineno=0)
        ], 1

    def grid_ranges(self, outer, inner):
        a, b = range_args(outer.iter), range_args(inner.iter)
        if not a or not b:
            return None, None
        if not (same(a[0], b[0]) and (is_int(a[0], 0) or is_int(a[0], 1))):
            return None, None
        grid = self.rows_of(a[1])
        if not grid or self.cols_of(b[1]) != grid or uses([b[1]], outer.target.id):
            return None, None
        return grid, a[0].value == 1

    def cell_filter(self, body, grid, r, c, kwargs):
        """A body that is one `if g[r][c] <op> v` loses the if to a keyword."""
        if not (len(body) == 1 and isinstance(body[0], ast.If) and not body[0].orelse):
            return body
        test = body[0].test
        kw = self.value_test(test, grid, r, c)
        if not kw:
            return body
        kwargs.update(kw)
        return body[0].body

    def value_test(self, test, grid, r, c):
        """{op: v} when test is g[r][c] <op> v, v a constant."""
        if not (isinstance(test, ast.Compare) and len(test.ops) == 1):
            return None
        op, left, right = type(test.ops[0]), test.left, test.comparators[0]
        cell = ast.Subscript(ast.Subscript(load(grid), load(r)), load(c))
        if not same(Canon(left), Canon(cell)):
            if not same(Canon(right), Canon(cell)) or op not in FLIPPED:
                return None
            op, left, right = FLIPPED[op], right, left
        if op not in COMPARE_KWARGS or not plain_value(right):
            return None
        return {COMPARE_KWARGS[op]: right}

    def pairs(self, stmts, i, tail):
        """for i in range(n): for j in range(i + 1, n) is for (i, j) in pairs(n)."""
        loops = self.two_loops(stmts, i)
        if not loops:
            return None
        outer, inner, body = loops
        a, b = range_args(outer.iter), range_args(inner.iter)
        if not a or not b or not is_int(a[0], 0) or has_break(body):
            return None
        if is_int(b[0], 0) and same(b[1], load(outer.target.id)):
            # for j in range(n): for i in range(j) is pairs(n, back=True)
            target = pair(inner.target, outer.target, ast.Store())
            it = call("pairs", a[1], back=ast.Constant(True))
            return [ast.For(target, it, body, [], lineno=0)], 1
        if not same(a[1], b[1]):
            return None
        want = ast.BinOp(load(outer.target.id), ast.Add(), ast.Constant(1))
        if not same(Canon(b[0]), Canon(want)):
            return None
        target = pair(outer.target, inner.target, ast.Store())
        return [ast.For(target, call("pairs", a[1]), body, [], lineno=0)], 1

    def nbrs(self, stmts, i, tail):
        """A loop over the four directions that steps to (nr, nc) and checks
        the grid's bounds is for (nr, nc) in nbrs(g, r, c)."""
        loop = stmts[i]
        if not (isinstance(loop, ast.For) and not loop.orelse and len(loop.body) >= 2):
            return None
        t = loop.target
        if not (
            isinstance(t, ast.Tuple)
            and len(t.elts) == 2
            and all(name(e) for e in t.elts)
        ):
            return None
        dr, dc = t.elts[0].id, t.elts[1].id
        if not self.directions(loop.iter):
            return None
        step = loop.body[0]
        stepped = self.step(step, dr, dc)
        if not stepped:
            return None
        (nr, nc), (r, c) = stepped
        rest = loop.body[1:]
        found = self.bounds(rest, nr, nc)
        if not found:
            return None
        grid, rest = found
        if uses(rest, dr) or uses(rest, dc):
            return None
        kwargs = {}
        if len(rest) == 1 and isinstance(rest[0], ast.If) and not rest[0].orelse:
            kw = self.value_test(rest[0].test, grid, nr, nc)
            if kw:
                kwargs, rest = kw, rest[0].body
        it = call("nbrs", load(grid), load(r), load(c), **kwargs)
        target = pair(ast.Name(nr, ast.Store()), ast.Name(nc, ast.Store()), ast.Store())
        return [ast.For(target, it, rest, [], lineno=0)], 1

    def directions(self, node):
        node = self.constant(node)
        if not isinstance(node, (ast.List, ast.Tuple)) or len(node.elts) != 4:
            return False
        got = set()
        for e in node.elts:
            if not (isinstance(e, (ast.Tuple, ast.List)) and len(e.elts) == 2):
                return False
            try:
                got.add(tuple(ast.literal_eval(x) for x in e.elts))
            except ValueError:
                return False
        return got == CARDINALS

    def step(self, s, dr, dc):
        """((nr, nc), (r, c)) when s is nr, nc = r + dr, c + dc."""
        if not (isinstance(s, ast.Assign) and len(s.targets) == 1):
            return None
        t, v = s.targets[0], s.value
        if not (isinstance(t, ast.Tuple) and isinstance(v, ast.Tuple)):
            return None
        if len(t.elts) != 2 or len(v.elts) != 2 or not all(name(e) for e in t.elts):
            return None
        base = []
        for e, d in zip(v.elts, (dr, dc)):
            if not (isinstance(e, ast.BinOp) and isinstance(e.op, ast.Add)):
                return None
            sides = [name(e.left), name(e.right)]
            if d not in sides or None in sides:
                return None
            base.append(sides[1 - sides.index(d)])
        return (t.elts[0].id, t.elts[1].id), tuple(base)

    def bounds(self, rest, nr, nc):
        """(grid, statements) with the bounds check on (nr, nc) taken out:
        `if 0 <= nr < m and 0 <= nc < n and rest: body` keeps `if rest`,
        and `if nr < 0 or ... : continue` goes."""
        first = rest[0]
        if not isinstance(first, ast.If) or first.orelse:
            return None
        if len(first.body) == 1 and isinstance(first.body[0], ast.Continue):
            test = first.test
            parts = (
                test.values
                if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or)
                else [test]
            )
            grid, others = self.outside(parts, nr, nc)
            if not grid:
                return None
            if others:
                kept = ast.If(
                    ast.BoolOp(ast.Or(), others) if len(others) > 1 else others[0],
                    first.body,
                    [],
                )
                return grid, [kept] + rest[1:]
            return grid, rest[1:]
        if len(rest) != 1:
            return None
        grid, others = self.inside(conjuncts(first.test), nr, nc)
        if not grid:
            return None
        if others:
            return grid, [ast.If(join_and(others), first.body, [])]
        return grid, first.body

    def inside(self, parts, nr, nc):
        """The grid whose bounds parts check for (nr, nc), and the other parts."""
        need = {("lo", nr), ("hi", nr), ("lo", nc), ("hi", nc)}
        grid, seen, others = None, set(), []
        for p in parts:
            got = self.in_bound(p)
            if not got:
                others.append(p)
                continue
            for side, var, size in got:
                g = None
                if side == "hi":
                    g = self.rows_of(size) if var == nr else self.cols_of(size)
                    if g is None or (grid and g != grid):
                        return None, None
                    grid = g
                seen.add((side, var))
        if seen != need or not grid:
            return None, None
        if any(uses([o], nr) and False for o in others):
            return None, None
        return grid, others

    def in_bound(self, p):
        """[(side, var, size)] for 0 <= v < n, v >= 0, v < n, 0 <= v, n > v."""
        if not isinstance(p, ast.Compare):
            return None
        terms = [p.left] + p.comparators
        out = []
        for a, op, b in zip(terms, p.ops, terms[1:]):
            op = type(op)
            if op in (ast.Gt, ast.GtE):
                a, b, op = b, a, {ast.Gt: ast.Lt, ast.GtE: ast.LtE}[op]
            if op == ast.LtE and is_int(a, 0) and name(b):
                out.append(("lo", b.id, None))
            elif op == ast.Lt and name(a) and not isinstance(b, ast.Constant):
                out.append(("hi", a.id, b))
            else:
                return None
        return out

    def outside(self, parts, nr, nc):
        need = {("lo", nr), ("hi", nr), ("lo", nc), ("hi", nc)}
        grid, seen, others = None, set(), []
        for p in parts:
            got = None
            if isinstance(p, ast.Compare) and len(p.ops) == 1:
                a, op, b = p.left, type(p.ops[0]), p.comparators[0]
                if op in (ast.Gt, ast.GtE) and not name(a) and name(b):
                    a, b, op = b, a, {ast.Gt: ast.Lt, ast.GtE: ast.LtE}[op]
                elif op in (ast.Lt, ast.LtE) and not name(a) and name(b):
                    a, b, op = b, a, {ast.Lt: ast.Gt, ast.LtE: ast.GtE}[op]
                if name(a) in (nr, nc):
                    if op == ast.Lt and is_int(b, 0):
                        got = ("lo", a.id, None)
                    elif op == ast.GtE and not isinstance(b, ast.Constant):
                        got = ("hi", a.id, b)
            if not got:
                others.append(p)
                continue
            side, var, size = got
            if side == "hi":
                g = self.rows_of(size) if var == nr else self.cols_of(size)
                if g is None or (grid and g != grid):
                    return None, None
                grid = g
            seen.add((side, var))
        if seen != need:
            return None, None
        return grid, others

    def levels(self, stmts, i, tail):
        """while q: x = q.popleft() ... is for (_, x) in levels(q)."""
        loop = stmts[i]
        if not (isinstance(loop, ast.While) and not loop.orelse and name(loop.test)):
            return None
        q = loop.test.id
        first = loop.body[0]
        if not (
            isinstance(first, ast.Assign)
            and len(first.targets) == 1
            and isinstance(first.value, ast.Call)
            and isinstance(first.value.func, ast.Attribute)
            and first.value.func.attr == "popleft"
            and name(first.value.func.value) == q
            and not first.value.args
        ):
            return None
        rest = loop.body[1:]
        for n in ast.walk(ast.Module(rest, [])):
            if isinstance(n, ast.Attribute) and name(n.value) == q:
                if n.attr in ("popleft", "appendleft", "clear", "pop", "rotate"):
                    return None
            if isinstance(n, ast.Name) and n.id == q and isinstance(n.ctx, ast.Store):
                return None
            if isinstance(n, ast.Call) and name(n.func) == "len" and uses(n.args, q):
                return None
        target = self.store(first.targets[0])
        if target is None:
            return None
        loop_target = pair(ast.Name("_", ast.Store()), target, ast.Store())
        body = rest or [ast.Pass()]
        return [ast.For(loop_target, call("levels", load(q)), body, [], lineno=0)], 1

    def store(self, t):
        if isinstance(t, ast.Name):
            return t
        if isinstance(t, ast.Tuple) and all(self.store(e) for e in t.elts):
            return t
        return None

    def enumerate_(self, stmts, i, tail):
        """for i in range(len(xs)): x = xs[i] is for i, x in enumerate(xs)."""
        loop = stmts[i]
        if not (isinstance(loop, ast.For) and not loop.orelse and name(loop.target)):
            return None
        a = range_args(loop.iter)
        if not a or not is_int(a[0], 0):
            return None
        hi = a[1]
        if not (
            isinstance(hi, ast.Call) and name(hi.func) == "len" and len(hi.args) == 1
        ):
            return None
        xs = name(hi.args[0])
        first = loop.body[0]
        if not xs or not (isinstance(first, ast.Assign) and len(first.targets) == 1):
            return None
        want = ast.Subscript(load(xs), load(loop.target.id))
        if not (name(first.targets[0]) and same(Canon(first.value), Canon(want))):
            return None
        for n in ast.walk(ast.Module(loop.body, [])):
            if isinstance(n, ast.Attribute) and name(n.value) == xs:
                if n.attr in ("append", "extend", "insert", "pop", "remove", "clear"):
                    return None
            if isinstance(n, ast.Name) and n.id in (xs, loop.target.id):
                if isinstance(n.ctx, ast.Store):
                    return None
            if isinstance(n, ast.Delete):
                return None
        target = pair(loop.target, first.targets[0], ast.Store())
        it = call("enumerate", load(xs))
        return [ast.For(target, it, loop.body[1:] or [ast.Pass()], [], lineno=0)], 1

    def first_true(self, stmts, i, tail):
        """while lo < hi: mid = (lo + hi) // 2; if p: hi = mid else lo = mid + 1
        is lo = first mid in lo..<hi if p."""
        loop = stmts[i]
        if not (
            isinstance(loop, ast.While) and not loop.orelse and len(loop.body) == 2
        ):
            return None
        t = loop.test
        if not (
            isinstance(t, ast.Compare)
            and len(t.ops) == 1
            and isinstance(t.ops[0], ast.Lt)
            and name(t.left)
            and name(t.comparators[0])
        ):
            return None
        lo, hi = t.left.id, t.comparators[0].id
        mid_s, branch = loop.body
        if not (
            isinstance(mid_s, ast.Assign)
            and len(mid_s.targets) == 1
            and name(mid_s.targets[0])
        ):
            return None
        mid = mid_s.targets[0].id
        halves = [
            f"({lo} + {hi}) // 2",
            f"{lo} + ({hi} - {lo}) // 2",
            f"({lo} + {hi}) >> 1",
            f"{lo} + ({hi} - {lo} >> 1)",
        ]
        if ast.unparse(mid_s.value) not in halves:
            return None
        if not (
            isinstance(branch, ast.If)
            and len(branch.body) == 1
            and len(branch.orelse) == 1
        ):
            return None
        yes, no = branch.body[0], branch.orelse[0]
        test = branch.test
        if (
            ast.unparse(yes) == f"{lo} = {mid} + 1"
            and ast.unparse(no) == f"{hi} = {mid}"
        ):
            test, yes, no = ast.UnaryOp(ast.Not(), test), no, yes
        if (
            ast.unparse(yes) != f"{hi} = {mid}"
            or ast.unparse(no) != f"{lo} = {mid} + 1"
        ):
            return None
        if uses([test], lo) or uses([test], hi):
            return None
        if {mid, hi} & free_reads(stmts[i + 1 :]):
            return None  # the loop leaves hi == lo and mid set; first does not
        rng = ast.Call(load("range"), [load(lo), load(hi)], [])
        fn = ast.Lambda(ast.arguments([], [ast.arg(mid)], None, [], [], None, []), test)
        key = ast.keyword("key", fn)
        search = ast.Call(load("bisect_left"), [rng, ast.Constant(True)], [key])
        value = ast.BinOp(load(lo), ast.Add(), search)
        return [ast.Assign([ast.Name(lo, ast.Store())], value, lineno=0)], 1

    # dead stores

    def dead(self, stmts):
        """Drop an assignment of a plain value to names nothing reads."""
        reads = loaded([self.fn])
        for n in ast.walk(self.fn):
            if isinstance(n, ast.AugAssign):
                reads |= {t.id for t in ast.walk(n.target) if isinstance(t, ast.Name)}
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Del):
                reads.add(n.id)
        out = []
        for s in stmts:
            for field in ("body", "orelse"):
                inner = getattr(s, field, None)
                if isinstance(inner, list) and inner and isinstance(inner[0], ast.stmt):
                    setattr(s, field, self.dead(inner) or [ast.Pass()])
            if isinstance(s, ast.Assign) and self.pure(s.value):
                names = set()
                for t in s.targets:
                    if not isinstance(t, (ast.Name, ast.Tuple)):
                        break
                    names |= {n.id for n in ast.walk(t) if isinstance(n, ast.Name)}
                else:
                    if names and not names & reads:
                        self.fired["dead"] += 1
                        continue
            out.append(s)
        return out

    def pure(self, node):
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                if name(n.func) not in ("len", "range", "set", "list", "dict", "deque"):
                    return False
            elif isinstance(n, (ast.Lambda, ast.NamedExpr, ast.Await, ast.Yield)):
                return False
        return True


class Exprs(ast.NodeTransformer):
    """Expression idioms: [[v] * n for _ in range(m)] is table(m, n, fill=v)
    or like(g, fill=v); a comprehension over range(len(g)) and
    range(len(g[0])) runs over cells(g)."""

    def __init__(self, rules):
        self.r = rules

    def visit_ListComp(self, node):
        self.generic_visit(node)
        return self.table(node) or self.cell_comp(node) or node

    def visit_GeneratorExp(self, node):
        self.generic_visit(node)
        return self.cell_comp(node) or node

    def visit_SetComp(self, node):
        self.generic_visit(node)
        return self.cell_comp(node) or node

    def table(self, node):
        if len(node.generators) != 1:
            return None
        g = node.generators[0]
        a = range_args(g.iter)
        if (
            g.ifs
            or not a
            or not is_int(a[0], 0)
            or uses([node.elt], name(g.target) or "?")
        ):
            return None
        rows, elt = a[1], node.elt
        cols = fill = None
        if (
            isinstance(elt, ast.BinOp)
            and isinstance(elt.op, ast.Mult)
            and isinstance(elt.left, ast.List)
            and len(elt.left.elts) == 1
        ):
            fill, cols = elt.left.elts[0], elt.right
        elif isinstance(elt, ast.ListComp) and len(elt.generators) == 1:
            ig = elt.generators[0]
            b = range_args(ig.iter)
            if (
                not ig.ifs
                and b
                and is_int(b[0], 0)
                and not uses([elt.elt], name(ig.target) or "?")
            ):
                fill, cols = elt.elt, b[1]
        if fill is None or not plain_value(fill):
            return None
        kwargs = {} if is_int(fill, 0) else {"fill": fill}
        grid = self.r.rows_of(rows)
        if grid and self.r.cols_of(cols) == grid:
            self.r.fired["like"] += 1
            return call("like", load(grid), **kwargs)
        self.r.fired["table"] += 1
        return call("table", rows, cols, **kwargs)

    def cell_comp(self, node):
        gens = node.generators
        for k in range(len(gens) - 1):
            a, b = gens[k], gens[k + 1]
            if a.ifs or not name(a.target) or not name(b.target):
                continue
            ra, rb = range_args(a.iter), range_args(b.iter)
            if not ra or not rb or not is_int(ra[0], 0) or not is_int(rb[0], 0):
                continue
            grid = self.r.rows_of(ra[1])
            if not grid or self.r.cols_of(rb[1]) != grid:
                continue
            kwargs, ifs = {}, list(b.ifs)
            if len(ifs) == 1:
                kw = self.r.value_test(ifs[0], grid, a.target.id, b.target.id)
                if kw:
                    kwargs, ifs = kw, []
            target = pair(a.target, b.target, ast.Store())
            one = ast.comprehension(target, call("cells", load(grid), **kwargs), ifs, 0)
            node.generators = gens[:k] + [one] + gens[k + 2 :]
            self.r.fired["cells"] += 1
            if (
                isinstance(node, (ast.ListComp, ast.GeneratorExp))
                and len(node.generators) == 1
                and not ifs
                and same(Canon(node.elt), Canon(pair(a.target, b.target)))
            ):
                # [(r, c) for (r, c) in cells(g)] is list(cells(g))
                it = node.generators[0].iter
                return call("list", it) if isinstance(node, ast.ListComp) else it
            return node
        return None


class FoldBlock(ast.stmt):
    """A fold whose value comes from a block: `sum for x in xs` with lines
    under it, the last one the value. py2mu prints it."""

    _fields = ("op", "start", "target", "iter", "body", "value")

    def __init__(self, op, start, target, iter, body, value):
        super().__init__()
        self.op, self.start, self.target, self.iter = op, start, target, iter
        self.body, self.value = body, value
        self.lineno = 0


def Canon(node):
    """node with load and store made alike, for comparison."""
    node = ast.parse(ast.unparse(node), mode="eval").body
    for n in ast.walk(node):
        if hasattr(n, "ctx"):
            n.ctx = ast.Load()
    return node
