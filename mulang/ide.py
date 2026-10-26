"""mu ide: hover, completion, go-to-definition, signature help and rename for
VS Code.

    python -m mulang.ide    serve: one JSON request per line on stdin,
                            one JSON reply per line on stdout

A request is {"id", "cmd", "root", "text", "line", "col"}; line and col are
0-based, as in VS Code. cmd is hover, complete, definition, signature or
rename, which also takes "new". The reply is {"id", "result"}.

The file is transpiled with its line map (transpile(mapped=True)) and Jedi
answers on the Python. A mu line that does not parse is blanked and the rest
is transpiled again, so a half-typed line costs only itself. For completion
the cursor line is replaced by the name chain before the cursor, `q.pu`,
which always transpiles; for signature help, by the call up to its last
comma, closed. Positions go through the map both ways: the k-th occurrence
of a word on a mu line is taken to be the k-th on the Python lines it made.
"""

import ast
import inspect
import io
import json
import keyword
import os
import re
import sys
import textwrap
import tokenize
from functools import cache
from pathlib import Path

import mulang  # noqa: E402

from . import (
    BUILTINS,
    DOCS,
    HELPERS,
    NATIVE,
    PATHS,
    RUNTIME,
    MuError,
    find_module,
    load_module,
    mu_type,
    profile,
    transpile,
)

HERE = Path(__file__).parent
BUFFER = ".mu_ide.py"  # the name Jedi sees for the transpiled text
WORD = re.compile(r"[A-Za-z_]\w*")
# the name chain before the cursor: `q.pu`, `q.`, `nu`, or nothing
CHAIN = re.compile(r"(?:[A-Za-z_]\w*\.)*(?:[A-Za-z_]\w*)?$")
LINE_ERR = re.compile(r"^line (\d+):")
DEFINED = re.compile(r"^(?:def|class) ([A-Za-z_]\w*)", flags=re.M)
KEYWORDS = sorted(
    {"def", "memo", "for", "in", "if", "elif", "else", "while", "return", "ret"}
    | {"break", "continue", "pass", "del", "assert", "yield", "from", "import"}
    | {"extends", "and", "or", "not", "is", "true", "false", "none", "inf"}
    | {"first", "sum", "max", "min", "count", "lambda"}
    | {"with", "as", "try", "except", "finally", "raise", "global", "nonlocal"}
    | {"async", "await"}
)
TRIES = 200


def doc_lines():
    """Every line of the ```python blocks of the profile's documentation
    that starts with a name: [(name, line, path, 0-based line number)].
    The same name may have several lines, one call each."""
    out = []
    for path in DOCS:
        fenced = False
        for k, line in enumerate(Path(path).read_text().splitlines()):
            if line.startswith("```"):
                fenced = line.startswith("```python")
                continue
            m = WORD.match(line.lstrip("@"))
            if fenced and m and line.strip():
                out.append((m.group(), line, path, k))
    return out


def helper_docs():
    """The documented helpers: name -> its lines, `call  # what it gives`."""
    docs = {}
    for name, line, _, _ in doc_lines():
        docs.setdefault(name, []).append(line)
    return docs


def documented_at():
    """name -> (path, 0-based line) where the documentation first lists it."""
    at = {}
    for name, _, path, k in doc_lines():
        at.setdefault(name, (path, k))
    return at


def helper_defs():
    """name -> (path, 0-based line) of every helper and preloaded name."""
    out = {}
    for name, h in HELPERS.items():
        if h.path is not None:
            out[name] = (h.path, h.line)
    for name, obj in BUILTINS.items():
        try:
            path = inspect.getsourcefile(obj)
            line = inspect.getsourcelines(obj)[1] - 1
        except (TypeError, OSError):
            continue
        if path:
            out.setdefault(name, (path, line))
    return out


def library():
    """The public defs and classes of the profile's library: the helpers a
    file calls by name, not the language's own and not the constants."""
    out = []
    for name, h in HELPERS.items():
        if name.startswith("_") or name in RUNTIME:
            continue
        body = ast.parse(h.source).body
        if body and isinstance(body[0], (ast.FunctionDef, ast.ClassDef)):
            out.append(name)
    return sorted(out)


def preloaded():
    """Every name the profile's environment preloads that can be called,
    and every public helper of its library: what a file may call without
    defining it."""
    names = {n for n, obj in BUILTINS.items() if callable(obj)}
    return sorted(names | set(library()))


def params_of(args):
    """The parameters of an ast.arguments as written in a call, without
    annotations or self: `h, k=inf`."""
    names = [a.arg for a in args.posonlyargs + args.args]
    defaults = [None] * (len(names) - len(args.defaults)) + list(args.defaults)
    parts = [
        n if d is None else f"{n}={ast.unparse(d)}" for n, d in zip(names, defaults)
    ]
    if args.vararg:
        parts.append("*" + args.vararg.arg)
    for a, d in zip(args.kwonlyargs, args.kw_defaults):
        parts.append(a.arg if d is None else f"{a.arg}={ast.unparse(d)}")
    if args.kwarg:
        parts.append("**" + args.kwarg.arg)
    return ", ".join(p for p in parts if p != "self")


def source_node(name):
    """The def or class behind a name: its helper source, else the object
    the environment holds; None when neither can be read."""
    if name in HELPERS:
        body = ast.parse(HELPERS[name].source).body
        return body[0] if body else None
    try:
        body = ast.parse(textwrap.dedent(inspect.getsource(BUILTINS[name]))).body
    except (KeyError, TypeError, OSError):
        return None
    return body[0] if body else None


def args_of(node):
    """The parameters of a def, or of a class's __init__; none if it has none."""
    if isinstance(node, ast.FunctionDef):
        return node.args
    inits = [n for n in node.body if isinstance(n, ast.FunctionDef)]
    init = next((n for n in inits if n.name == "__init__"), None)
    return init.args if init else ast.arguments([], [], None, [], [], None, [])


def registry_docs():
    """name -> [`call  # first docstring line`] for every helper and preloaded
    name, read from its def; a class is written as a call of its __init__."""
    docs = {}
    for name in preloaded():
        node = source_node(name)
        if node is None or not isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            continue
        doc = HELPERS[name].doc if name in HELPERS else inspect.getdoc(BUILTINS[name])
        doc = (doc or "").split("\n", 1)[0].strip()
        call = f"{name}({params_of(args_of(node))})"
        docs[name] = [f"{call}  # {doc}" if doc else call]
    return docs


@cache
def _docs(revision):
    # the documentation's lines first; an undocumented name gets its def
    out = helper_docs()
    for name, lines in registry_docs().items():
        out.setdefault(name, lines)
    return out


def docs():
    """name -> its hover lines, for the registry as it stands."""
    return _docs(mulang.REVISION)


def depth_after(text, depth):
    """The bracket depth after this line; strings and the comment are skipped."""
    k = 0
    while k < len(text):
        ch = text[k]
        if ch == "#":
            break
        if ch in "\"'":
            end = text.find(ch, k + 1)
            k = len(text) if end < 0 else end + 1
            continue
        depth += (ch in "([{") - (ch in ")]}")
        k += 1
    return depth


def unclosed(lines):
    """The lines with every logical line whose brackets do not close blanked:
    one still open when a line no deeper than its first line arrives, or at
    the end, and one that closes more than it opens. Returns the blanked
    line numbers too."""
    lines = list(lines)
    blanked = set()
    depth, start, start_ind = 0, 0, 0
    for k, text in enumerate(lines + [""]):
        if not text.strip() and k < len(lines):
            continue
        ind = len(text) - len(text.lstrip())
        if depth and (ind <= start_ind or k == len(lines)):
            blanked |= set(range(start, k))
            depth = 0
        if depth == 0:
            start, start_ind = k, ind
        depth = depth_after(text, depth)
        if depth < 0:
            blanked |= set(range(start, k + 1))
            depth = 0
    for k in blanked:
        lines[k] = ""
    return lines, blanked


def repaired(lines, path=None):
    """Transpile the lines, blanking any line that fails until it parses:
    (code, origin, blanked). When a line the parser names is blank already,
    the nearest line above it goes; an error with no line starts from the
    end. None when nothing parses."""
    lines, blanked = unclosed(lines)
    for _ in range(TRIES):
        try:
            code, origin = transpile("\n".join(lines) + "\n", mapped=True, path=path)
            return code, origin, blanked
        except MuError as err:
            m = LINE_ERR.match(str(err))
            k = (int(m.group(1)) if m else len(lines)) - 1
            if not 0 <= k < len(lines):
                k = len(lines) - 1
            while k >= 0 and (k in blanked or not lines[k].strip()):
                k -= 1
            if k < 0:
                return None
            lines[k] = ""
            blanked.add(k)
    return None


def word_at(text, col):
    """(word, start, end) of the name under col, or None."""
    for m in WORD.finditer(text):
        if m.start() <= col <= m.end():
            return m.group(), m.start(), m.end()
    return None


def occurrence(text, word, col):
    """Which occurrence of word, counting from 0, holds col."""
    return sum(1 for m in re.finditer(rf"\b{word}\b", text) if m.end() < col)


def py_lines(origin, line):
    """The 1-based Python lines a 0-based mu line made."""
    return [k + 1 for k, o in enumerate(origin) if o == line + 1]


def nth(code, rows, word, k):
    """(1-based line, column after the word) of the k-th occurrence of word
    on these Python rows, the last one when there are fewer; None if none."""
    hits = []
    text = code.splitlines()
    for row in rows:
        for m in re.finditer(rf"\b{word}\b", text[row - 1]):
            hits.append((row, m.end()))
    if not hits:
        return None
    return hits[min(k, len(hits) - 1)]


def script(code, root):
    import jedi

    project = jedi.Project(root, added_sys_path=[root, *PATHS])
    return jedi.Script(code=code, path=f"{root}/{BUFFER}", project=project)


class File:
    """One request's view of the file: its lines, the Python, and the map."""

    def __init__(self, req):
        self.root = req.get("root") or os.getcwd()
        self.path = req.get("path")  # the file's own, where its mu imports are found
        profile(self.root)
        self.lines = req["text"].split("\n")
        self.line, self.col = req["line"], req["col"]
        self.text = self.lines[self.line] if self.line < len(self.lines) else ""
        self.code = self.origin = None
        self.blanked = set()

    def build(self, lines=None):
        """Transpile (a variant of) the lines; True when something parsed."""
        got = repaired(self.lines if lines is None else lines, self.path)
        if got is None:
            return False
        self.code, self.origin, self.blanked = got
        return True

    def rows(self, line=None):
        return py_lines(self.origin, self.line if line is None else line)

    def at(self):
        """The Python (line, col) of the word under the cursor, and the word."""
        w = word_at(self.text, self.col)
        if not w or self.line in self.blanked:
            return None
        word, start, _ = w
        pos = nth(self.code, self.rows(), word, occurrence(self.text, word, start + 1))
        return (pos, word) if pos else None

    def back(self, name):
        """A Jedi name in the buffer as (path, 0-based line, col) in the mu
        file, or where a pasted helper is defined, or its spec line."""
        mu = self.origin[name.line - 1]
        if mu is None:
            return helper_location(name.name)
        line = mu - 1
        m = re.search(rf"\b{name.name}\b", self.lines[line])
        return (None, line, m.start() if m else 0)

    def head(self, name):
        """The mu def or memo line a buffer function is bound on, as markdown."""
        if name.type != "function" or name.module_path is None:
            return None
        if name.module_path.name != BUFFER:
            return None
        mu = self.origin[name.line - 1]
        if mu is None:
            return None
        text = self.lines[mu - 1].strip()
        if text.split(" ")[0] in ("def", "memo"):
            return f"```mu\n{text}\n```"
        return None

    def is_helper(self, name):
        return (
            name.module_path is not None
            and name.module_path.name == BUFFER
            and self.origin[name.line - 1] is None
            and name.name in docs()
        )


def helper_location(name):
    """Where a helper is defined: its source, else its documentation line."""
    if name in helper_defs():
        path, line = helper_defs()[name]
        return (path, line, 0)
    if name in documented_at():
        path, line = documented_at()[name]
        return (path, line, 0)
    return None


def hint(name):
    """The type of a Jedi name, written as a mu type."""
    try:
        h = name.get_type_hint()
    except Exception:
        return None
    if not h:
        return None
    try:
        return mu_type(ast.parse(h, mode="eval").body)
    except SyntaxError:
        return h


def describe(f, name, word):
    """Markdown for one Jedi name: a helper's spec lines, a def's own mu
    line, or `word: type`."""
    if f.is_helper(name) or (name.module_path is None and word in docs()):
        return "```mu\n" + "\n".join(docs()[word]) + "\n```"
    if name.type in ("function", "class", "module"):
        head = name.docstring().split("\n", 1)[0]
        if name.module_path and name.module_path.name == BUFFER:
            mu = f.origin[name.line - 1]
            if mu is not None:
                head = f.lines[mu - 1].strip()
        doc = name.docstring(raw=True).strip()
        return f"```mu\n{head}\n```" + (f"\n\n{doc}" if doc else "")
    t = hint(name)
    return f"```mu\n{word}: {t}\n```" if t else None


def hover(req):
    f = File(req)
    w = word_at(f.text, f.col)
    if not w:
        return None
    if not f.build() or f.at() is None:
        return spec_hover(w[0])
    (line, col), word = f.at()
    s = script(f.code, f.root)
    parts = []
    for n in s.goto(line, col):  # a def or memo of the file: its own line
        h = f.head(n)
        if h and h not in parts:
            parts.append(h)
    for n in [] if parts else s.infer(line, col) or s.goto(line, col):
        d = describe(f, n, word)
        if d and d not in parts:
            parts.append(d)
    if not parts:
        return spec_hover(word)
    return {"contents": "\n\n".join(parts)}


def spec_hover(word):
    if word not in docs():
        return None
    return {"contents": "```mu\n" + "\n".join(docs()[word]) + "\n```"}


MU_IMPORT = re.compile(r"^\s*from ([\w.]+) import (.+)$")


def native_location(f, name):
    """Where a name the file imports from a mu module is defined, or None."""
    here = str(Path(f.path).parent) if f.path else f.root
    for line in f.lines:
        m = MU_IMPORT.match(line)
        if not m or name not in {n.strip() for n in m.group(2).strip("() ").split(",")}:
            continue
        found = find_module(m.group(1), here)
        if found is None:
            return None
        try:
            h = NATIVE[load_module(found)[name]]
        except (MuError, KeyError):
            return None
        return (h.path, h.line, 0)
    return None


def definition(req):
    f = File(req)
    w = word_at(f.text, f.col)
    if w and native_location(f, w[0]):
        loc = native_location(f, w[0])
        return [{"path": loc[0], "line": loc[1], "col": loc[2]}]
    if not f.build() or f.at() is None:
        w = word_at(f.text, f.col)
        loc = helper_location(w[0]) if w else None
        return [{"path": loc[0], "line": loc[1], "col": loc[2]}] if loc else []
    (line, col), word = f.at()
    out = []
    for n in script(f.code, f.root).goto(line, col, follow_imports=True):
        if n.module_path is None:
            continue
        if n.module_path.name == BUFFER:
            loc = f.back(n)
        else:
            loc = (str(n.module_path), n.line - 1, n.column)
        if loc and loc not in out:
            out.append(loc)
    # a name of the registry that the file itself does not define: its own
    # source, not the module that re-exports it, and not nothing
    if not any(p is None for p, _, _ in out) and helper_location(word):
        out = [helper_location(word)]
    return [{"path": p, "line": ln, "col": c} for p, ln, c in out]


KINDS = {
    "module": "module",
    "class": "class",
    "instance": "variable",
    "function": "function",
    "param": "variable",
    "statement": "variable",
    "property": "property",
}


def complete(req):
    f = File(req)
    before = f.text[: f.col]
    indent = before[: len(before) - len(before.lstrip())]
    chain = CHAIN.search(before).group()
    dotted = "." in chain
    prefix = chain.rsplit(".", 1)[-1]
    probe = list(f.lines)
    probe[f.line] = indent + (chain or "x")
    items = []
    if f.build(probe) and f.line not in f.blanked:
        rows = f.rows()
        text = f.code.splitlines()
        hit = [(r, text[r - 1].find(chain or "x")) for r in rows]
        hit = [(r, c) for r, c in hit if c >= 0]
        if hit:
            row, c = hit[0]
            col = c + len(chain)
            for n in script(f.code, f.root).complete(row, col):
                if n.type == "keyword" or n.name.startswith("__") or n.name == "self":
                    continue
                items.append({"label": n.name, "kind": KINDS.get(n.type, "variable")})
    if dotted:
        return items
    seen = {i["label"] for i in items}
    for name in KEYWORDS:
        if name.startswith(prefix) and name not in seen:
            items.append({"label": name, "kind": "keyword"})
    for name, lines in docs().items():
        if name.startswith(prefix) and name not in seen:
            items.append(
                {
                    "label": name,
                    "kind": "function",
                    "detail": lines[0],
                    "doc": "\n".join(lines),
                }
            )
    return items


def call_site(before):
    """The innermost open call in the text before the cursor: (callee,
    argument index, the text up to its last top-level comma, closed), or
    None outside any call."""
    opens = []  # index of each unclosed bracket
    commas = {}  # open index -> indexes of the commas directly inside it
    for k, ch in enumerate(before):
        if ch in "([{":
            opens.append(k)
            commas[k] = []
        elif ch in ")]}":
            if opens:
                commas.pop(opens.pop())
        elif ch == "," and opens:
            commas[opens[-1]].append(k)
    close = {"(": ")", "[": "]", "{": "}"}
    for k in reversed(opens):
        m = re.search(r"([A-Za-z_]\w*)\s*$", before[:k])
        if before[k] != "(" or not m:
            continue
        cut = commas[k][-1] + 1 if commas[k] else k + 1
        closers = "".join(close[before[o]] for o in reversed(opens))
        return m.group(1), len(commas[k]), before[:cut] + closers
    return None


def signature(req):
    f = File(req)
    site = call_site(f.text[: f.col])
    if not site:
        return None
    callee, index, line = site
    probe = list(f.lines)
    probe[f.line] = line
    sigs = []
    if f.build(probe) and f.line not in f.blanked:
        text = f.code.splitlines()
        for row in f.rows():
            m = re.search(rf"\b{callee}\(", text[row - 1])
            if m:
                sigs = script(f.code, f.root).get_signatures(row, m.end())
                break
    doc = "\n".join(docs().get(callee, []))
    if not sigs:
        if not doc:
            return None
        return {
            "label": docs()[callee][0].split("#")[0].strip(),
            "params": [],
            "active": index,
            "doc": doc,
        }
    s = sigs[0]
    params = [p.to_string() for p in s.params]
    label = f"{s.name}({', '.join(params)})"
    return {
        "label": label,
        "params": params,
        "active": index,
        "doc": doc or s.docstring(raw=True).strip(),
    }


COMPS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


class Scope:
    """A Python scope: a module, def, lambda, class or comprehension, and
    the names bound in it."""

    def __init__(self, parent=None, kind="module"):
        self.parent, self.kind = parent, kind
        self.bound, self.globals, self.nonlocals = set(), set(), set()

    def owner(self, name):
        """The scope a name used here is bound in; None for a builtin. A
        class body is not seen from the scopes inside it."""
        s = self
        while s is not None:
            if name in s.globals:
                while s.parent is not None:
                    s = s.parent
                return s if name in s.bound else None
            if name in s.bound and name not in s.nonlocals:
                return s
            s = s.parent
            while s is not None and s.parent is not None and s.kind == "class":
                s = s.parent
        return None

    def inside(self, other):
        s = self
        while s is not None and s is not other:
            s = s.parent
        return s is other


def names(code):
    """[(name, 1-based line, col, scope)] for every name the Python binds
    or reads, col in characters as tokenize counts it. None when the
    Python does not parse."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    text = code.splitlines()
    out = []

    def chars(row, col):
        return len(text[row - 1].encode()[:col].decode(errors="ignore"))

    def use(scope, name, node):
        out.append((name, node.lineno, chars(node.lineno, node.col_offset), scope))

    def bind(scope, name, node):
        scope.bound.add(name)
        if hasattr(node, "lineno"):
            use(scope, name, node)

    def args(fn, scope, inner):
        a = fn.args
        for d in a.defaults + [d for d in a.kw_defaults if d is not None]:
            visit(d, scope)
        every = a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg]
        for arg in filter(None, every):
            if arg.annotation is not None:
                visit(arg.annotation, scope)
            bind(inner, arg.arg, arg)

    def visit(node, scope):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for d in node.decorator_list:
                visit(d, scope)
            inner = Scope(scope, "class" if isinstance(node, ast.ClassDef) else "def")
            if inner.kind == "class":
                for b in node.bases + node.keywords:
                    visit(b, scope)
            else:
                args(node, scope, inner)
                if node.returns is not None:
                    visit(node.returns, scope)
            m = re.search(rf"\b(?:def|class)\s+({node.name})\b", text[node.lineno - 1])
            scope.bound.add(node.name)
            if m:
                out.append((node.name, node.lineno, m.start(1), scope))
            for b in node.body:
                visit(b, inner)
        elif isinstance(node, ast.Lambda):
            inner = Scope(scope, "def")
            args(node, scope, inner)
            visit(node.body, inner)
        elif isinstance(node, COMPS):
            visit(node.generators[0].iter, scope)
            inner = Scope(scope, "comp")
            for k, g in enumerate(node.generators):
                if k:
                    visit(g.iter, inner)
                visit(g.target, inner)
                for c in g.ifs:
                    visit(c, inner)
            for part in ("elt", "key", "value"):
                if hasattr(node, part):
                    visit(getattr(node, part), inner)
        elif isinstance(node, ast.NamedExpr):
            visit(node.value, scope)
            s = scope
            while s.kind == "comp":
                s = s.parent
            bind(s, node.target.id, node.target)
        elif isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                use(scope, node.id, node)
            else:
                bind(scope, node.id, node)
        elif isinstance(node, ast.alias):
            bind(scope, (node.asname or node.name).split(".")[0], node)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            scope.bound.add(node.name)
            for child in ast.iter_child_nodes(node):
                visit(child, scope)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            decl = scope.globals if isinstance(node, ast.Global) else scope.nonlocals
            decl.update(node.names)
        else:
            for child in ast.iter_child_nodes(node):
                visit(child, scope)

    visit(tree, Scope())
    return out


LAYOUT = {tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE, tokenize.NL}


def tokens(code, origin):
    """The tokens of the Python's lines that a mu line made: not the
    helpers and runtime the transpiler adds, which follow the free names."""
    try:
        every = list(tokenize.generate_tokens(io.StringIO(code).readline))
    except (tokenize.TokenError, SyntaxError):
        return None
    return [
        t
        for t in every
        if t.type not in LAYOUT
        and t.start[0] <= len(origin)
        and origin[t.start[0] - 1] is not None
    ]


def swapped(a, b, word, new):
    """The (1-based line, col) of each `word` token of the Python a that is
    `new` in the Python b, every other token the same but the names the
    transpiler makes from a parameter, `_in_word` and `_w_word`; None when
    b is not a so renamed. a and b are (code, origin)."""
    ta, tb = tokens(*a), tokens(*b)
    if ta is None or tb is None or len(ta) != len(tb):
        return None
    made = {f"_{p}_{word}": f"_{p}_{new}" for p in ("in", "w")}
    out = set()
    for x, y in zip(ta, tb):
        if (x.type, x.string) == (y.type, y.string):
            continue
        if x.type != tokenize.NAME or y.type != tokenize.NAME:
            return None
        if (x.string, y.string) == (word, new):
            out.add(x.start)
        elif made.get(x.string) != y.string:
            return None
    return out


def renamed(f, new):
    """The edits that rename the name under the cursor to new, as
    {"edits": [{"line", "col", "end"}]}, or {"refused": why}.

    The variable is found on the Python by its scoping rules. Each mu
    occurrence of the word is renamed alone and transpiled: it is an edit
    when what changes in the Python is that variable only. The renamed mu
    must then transpile to the Python with that variable renamed, or
    nothing is changed."""
    bad = keyword.iskeyword(new) or new in KEYWORDS or new in mulang.KEYWORDS
    if not WORD.fullmatch(new) or bad:
        return {"refused": f"`{new}` is not a name mu can bind"}
    w = word_at(f.text, f.col)
    if not w or not f.build():
        return {"refused": "no name under the cursor"}
    word = w[0]
    rx = rf"\b{word}\b"
    for k in sorted(f.blanked):
        if re.search(rx, f.lines[k]):
            return {"refused": f"line {k + 1} does not transpile; fix it first"}
    base = ["" if k in f.blanked else t for k, t in enumerate(f.lines)]

    def changed(edits):
        """The Python tokens these mu edits change, or None."""
        lines = list(base)
        for ln, start, end in reversed(edits):
            lines[ln] = lines[ln][:start] + new + lines[ln][end:]
        try:
            got = transpile("\n".join(lines) + "\n", mapped=True, path=f.path)
        except Exception:  # any mu the edit breaks: the transpiler may not say MuError
            return None
        return swapped((f.code, f.origin), got, word, new)

    def alone(ln, start, end):
        return changed([(ln, start, end)])

    found = names(f.code)
    at = alone(f.line, w[1], w[2])
    if not found or not at:
        return {"refused": f"`{word}` is not a name here"}
    scope = {(r, c): s for n, r, c, s in found if n == word}
    if min(at) not in scope or scope[min(at)].owner(word) is None:
        return {"refused": f"`{word}` is not defined in this file"}
    own = scope[min(at)].owner(word)
    var = {p for p, s in scope.items() if s.owner(word) is own}
    if any(f.origin[r - 1] is None for r, _ in var):
        return {"refused": f"`{word}` is not defined in this file"}
    for n, r, c, s in found:
        # new already seen where the variable is, or a use of new that the
        # renamed variable would capture
        seen = n == word and s.owner(word) is own and s.owner(new)
        o = s.owner(new)
        caught = (
            n == new
            and s.owner(word) is own
            and not (o and o is not own and o.inside(own))
        )
        if seen or caught:
            return {"refused": f"`{new}` is taken where `{word}` is seen"}
    edits = []
    for ln, text in enumerate(base):
        for m in re.finditer(rx, text):
            hit = alone(ln, m.start(), m.end())
            if hit and hit <= var:
                edits.append((ln, m.start(), m.end()))
    if changed(edits) != var:
        return {"refused": f"`{word}` could not be renamed on the mu lines alone"}
    return {"edits": [{"line": ln, "col": a, "end": b} for ln, a, b in edits]}


def rename(req):
    return renamed(File(req), req["new"])


SEMANTIC = ("variable", "operator", "number", "string", "keyword")


def primes(req):
    """[line, col, length, kind] for each token of a stretch where `f'`, `r'`
    or `b'` reads as a prime. The grammar cannot see which names are bound,
    so it colours that stretch as a string; these tokens colour it again.
    Empty while the file does not parse."""
    text = req["text"]
    try:
        p, _ = mulang.parsed(text, mulang.Parser.program)
    except MuError:
        return []
    plain = {(n, v.col): v for k, v, n in mulang.tokenize(text) if k == "STR"}
    words = mulang.KEYWORDS | mulang.STATEMENT_WORDS
    out = []
    for line, col in sorted(p.primes):
        end = col + len(plain[(line, col)])
        for kind, val, n in mulang.tokenize(text, p.primes):
            if n != line or not isinstance(val, mulang.Col) or not col <= val.col < end:
                continue
            if kind == "NAME":
                kind = "keyword" if val in words else "variable"
            else:
                kind = {"NUM": "number", "STR": "string"}.get(kind, "operator")
            out.append([n - 1, val.col, len(val), kind])
    return out


COMMANDS = {
    "primes": primes,
    "hover": hover,
    "complete": complete,
    "definition": definition,
    "signature": signature,
    "rename": rename,
}


def answer(req):
    try:
        return {"id": req.get("id"), "result": COMMANDS[req["cmd"]](req)}
    except Exception as err:  # a bad request must not end the server
        return {"id": req.get("id"), "error": f"{type(err).__name__}: {err}"}


def serve(stdin=sys.stdin, stdout=sys.stdout):
    for line in stdin:
        if not line.strip():
            continue
        req = json.loads(line)
        stdout.write(json.dumps(answer(req)) + "\n")
        stdout.flush()


if __name__ == "__main__":
    try:
        import jedi  # noqa: F401
    except ImportError:
        sys.exit(
            "mu ide: jedi is not installed; run .venv/bin/pip install -r requirements.txt"
        )
    serve()
