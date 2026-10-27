"""mu: a small language that transpiles to Python.

python -m mulang file.mu        print the Python
python -m mulang --fmt file.mu  print the file, formatted
python -m mulang -              read from standard input
python -m mulang --version      print the language version

The language is specified in spec/v<VERSION>.md, one file per version.

A mu file calls helpers by name, and the transpiler pastes each one's
source into the output. The language owns a few (RUNTIME); the rest
come from a profile, a Python file named by `[tool.mu] profile` in the
project's pyproject.toml, which registers them with `library` and
`environment`. See `profile`.
"""

import ast
import builtins
import copy
import importlib
import importlib.util
import inspect
import os
import re
import symtable
import sys
from pathlib import Path
from typing import NamedTuple, Optional

VERSION = "0.14"

KEYWORDS = {"from", "in", "not", "and", "or", "if", "else", "is", "as"}
# names that end a call written without brackets
NOT_ARGS = {"from", "in", "not", "and", "or", "if", "else", "is", "for", "while"}
NOT_ARGS |= {"elif", "def", "memo", "return", "del", "assert", "pass", "break"}
NOT_ARGS |= {"continue", "import", "extends", "yield", "class"}
NOT_ARGS |= {"as", "with", "try", "except", "finally", "raise", "global", "nonlocal"}
NOT_ARGS |= {"async"}
AUGMENTED = {
    "=",
    "+=",
    "-=",
    "*=",
    "/=",
    "//=",
    "%=",
    "**=",
    "&=",
    "|=",
    "^=",
    "<<=",
    ">>=",
    "@=",
}
IN_PLACE = {
    "+=": "iadd",
    "-=": "isub",
    "*=": "imul",
    "/=": "itruediv",
    "//=": "ifloordiv",
    "%=": "imod",
    "**=": "ipow",
    "|=": "ior",
    "&=": "iand",
    "^=": "ixor",
    "<<=": "ilshift",
    ">>=": "irshift",
    "@=": "imatmul",
}
FOLDS = {"sum", "max", "min", "count"}
CONSTANTS = {"true": "True", "false": "False", "none": "None"}

TOKEN = re.compile(
    r"(?P<ws>[ \t]+)"
    r"|(?P<comment>#.*)"
    r"|(?P<num>0[xX][\da-fA-F_]+|0[oO][0-7_]+|0[bB][01_]+"
    r"|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][-+]?\d+)?)"
    r"|(?P<prime>(?<=\w)')"
    r"|(?P<str>[fFrRbB]{0,2}'[^'\n]*'|[fFrRbB]{0,2}\"[^\"\n]*\")"
    r"|(?P<name>[A-Za-z_]\w*)"
    r"|(?P<op>\.{4,}|\.\.\.|\.\.<|\.\.|->|<-|//=|<<=|>>=|\*\*=|==|!=|<=|>=|\+=|-=|\*=|/=|%=|&=|\|=|\^=|@=|:="
    r"|<<|>>|//|\*\*|[-+*/%<>=()\[\]{},:|.&^~?@])"
)


TRIPLE = re.compile(r"[fFrRbB]{0,2}('''|\"\"\")")


class MuError(Exception):
    def __init__(self, msg, eof=False):
        super().__init__(msg)
        self.eof = eof  # the input ended early: more lines may complete it


class Reprime(Exception):
    """`f'`, `r'` or `b'` met as a string while that name is bound: parse
    again with the name and a PRIME there."""

    def __init__(self, at):
        super().__init__(at)
        self.at = at  # (line, column)


def triple(lines, k, pos):
    """The triple-quoted string that starts at lines[k][pos], which may run
    over several lines: (text, k, pos) with k and pos just past its closing
    quotes. None when no triple quote starts there."""
    m = TRIPLE.match(lines[k], pos)
    if not m:
        return None
    quote, first, parts, at = m.group(1), k, [], m.end()
    while True:
        end = lines[k].find(quote, at)
        if end >= 0:
            parts.append(lines[k][pos : end + 3])
            return "\n".join(parts), k, end + 3
        parts.append(lines[k][pos:])
        k, pos, at = k + 1, 0, 0
        if k == len(lines):
            raise MuError(f"line {first + 1}: unclosed {quote} string", eof=True)


PREFIXED = re.compile(r"[fFrRbB]{1,2}'(?!'')")
# what a prime-marked name compiles to before the line that binds it again
# takes the mark off: `min_price'` is __mu_prime__min_price until then
PRIMED = "__mu_prime__"
# the head of a block lambda: its parameters and `->`
HEAD = r"(?:->|[A-Za-z_]\w*\s*->|\((?:\s*[A-Za-z_]\w*\s*,)*\s*(?:[A-Za-z_]\w*\s*)?\)\s*->)"
BLOCK_HEAD = re.compile(rf"\s*{HEAD}\s*(?:#.*)?$")  # `{p ->` ends the line
HEAD_LINE = re.compile(rf"^\s*{HEAD}\s*(?:#.*)?$")  # a line that is a head
BLANK = re.compile(r"\s*(?:#.*)?$")  # nothing, or a comment


def opens_block(line, pos, lines, k):
    """Whether the `{` just read at line[pos - 1] opens a block: the lambda
    head ends its line, `{p ->`, or the `{` ends the line and the next line
    of code is the head."""
    if BLOCK_HEAD.match(line, pos):
        return True
    if not BLANK.match(line, pos):
        return False
    for nxt in lines[k + 1 :]:
        if nxt.strip() and not nxt.lstrip().startswith("#"):
            return bool(HEAD_LINE.match(nxt))
    return False


class Col(str):
    """A token's text that remembers the column it starts at."""

    col = 0


def tokenize(src, primes=frozenset()):
    """Tokens are (kind, value, line). Blocks are marked by INDENT and DEDENT.
    A `'` directly after a letter, a digit or `_` is a PRIME token,
    `min_price'`. `f'`, `r'` and `b'` start a string, unless (line, column)
    of the name is in primes: the parser asks for that when the name is
    bound. Inside brackets lines run on, except after a `{` that ends its
    line as a lambda head, `{p ->`: that brace opens a block, and the lines
    under it are marked like any other."""
    toks, indents, depth = [], [0], 0
    stack = []  # each open bracket; "B" for a brace that opens a block
    lines, k = src.splitlines(), -1
    while k + 1 < len(lines):
        k += 1
        n, line = k + 1, lines[k]
        if depth == 0:
            body = line.strip()
            if not body or body.startswith("#"):
                continue
            ind = len(line) - len(line.lstrip(" "))
            if ind > indents[-1]:
                indents.append(ind)
                toks.append(("INDENT", "", n))
            while ind < indents[-1]:
                indents.pop()
                toks.append(("DEDENT", "", n))
            if ind != indents[-1]:
                raise MuError(f"line {n}: indentation does not match any block")
        pos = 0
        while pos < len(line):
            long = triple(lines, k, pos)
            if long:
                text, k, pos = long
                line = lines[k]
                toks.append(("STR", text, n))
                continue
            if (n, pos) in primes:
                m = NAME.match(line, pos)
            else:
                m = TOKEN.match(line, pos)
            if not m:
                raise MuError(f"line {n}: unexpected character {line[pos]!r}")
            start, pos = pos, m.end()
            kind = m.lastgroup or "name"
            if kind in ("ws", "comment"):
                continue
            val = Col(m.group())
            val.col = start
            if val in "([{":
                if val == "{" and opens_block(line, pos, lines, k):
                    stack.append("B")
                else:
                    depth += 1
                    stack.append(val)
            elif val in ")]}":
                if not (stack and stack.pop() == "B"):
                    depth -= 1
            toks.append((kind.upper(), val, n))
        if depth == 0 and toks and toks[-1][0] not in ("NEWLINE", "INDENT", "DEDENT"):
            toks.append(("NEWLINE", "", n))
    toks += [("DEDENT", "", 0)] * (len(indents) - 1) + [("EOF", "", 0)]
    return toks


class Py(str):
    """Python source for one expression, with facts the parser needs later."""

    div = None  # (a, b) when the expression is exactly a / b
    rng = None  # (lo, hi_exclusive) when the expression is a range
    parts = None  # the member types when a type is a parenthesized tuple
    call = False  # a call that dropped its brackets, `f x`
    pair = False  # a `k: v` or `**d` item of a dict
    repeats = None  # [(item, copies)] when a right side of = repeats, `[]..`
    is_async = False  # the name of an `async def`
    convert = None  # the line after `int(a), b = ...` that converts a
    orparts = None  # (x, d) when the expression is exactly x or d
    cmpparts: "tuple[str, str, str] | None" = (
        None  # (a, op, b) when the expression is exactly a op b
    )
    cond = False  # a conditional expression, `a if c else b`, unbracketed
    default = None  # a parameter's default value, `by=x -> x`
    star = ""  # a parameter's `*` or `**`: `*args`, `**kwargs`


class TargetError(MuError):
    """A `>` or `<` in the wrong place in a loop target. `names_a_target`
    lets it through, so the misuse is reported instead of read as `for xs`."""


class Hoisted(tuple):
    """A def the parser writes for a block lambda or a first block. It is
    placed before the statement that holds the expression. reads: the
    names its body reads from outside itself."""

    reads = frozenset()


class Decorated(tuple):
    """A def or memo statement with `@` lines above it."""

    decorators = ()  # (mu line, Python expression) for each `@` line


class Helper(NamedTuple):
    """A function, class or constant a mu file may name. Its source is
    pasted into the transpiled Python after its deps, with its imports at
    the top of the file. path and line say where it is defined."""

    imports: tuple
    deps: tuple
    source: str
    doc: str = ""
    path: Optional[str] = None
    line: Optional[int] = None


# the helpers the language itself emits
RUNTIME = {
    "Grid": Helper(
        (),
        (),
        '''class Grid(list):
    """A list of rows that also takes a (row, col) pair as an index."""

    def __getitem__(self, k):
        if type(k) is tuple:
            return list.__getitem__(self, k[0])[k[1]]
        return list.__getitem__(self, k)

    def __setitem__(self, k, v):
        if type(k) is tuple:
            list.__getitem__(self, k[0])[k[1]] = v
        else:
            list.__setitem__(self, k, v)''',
        "A list of rows that also takes a (row, col) pair as an index.",
    ),
    "Rec": Helper(
        (),
        (),
        '''class Rec(dict):
    """A dict that also gives its keys as attributes: d.k is d["k"]."""

    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k) from None

    def __setattr__(self, k, v):
        self[k] = v

    def __delattr__(self, k):
        try:
            del self[k]
        except KeyError:
            raise AttributeError(k) from None''',
        "A dict that also gives its keys as attributes: d.k is d[\"k\"].",
    ),
    "cache": Helper(
        (),
        (),
        '''def cache(fn):
    """Memoise fn on its arguments. Pure Python, so a deep memo recursion
    is not cut short by the C recursion limit of Python 3.12 and 3.13."""
    memo = {}

    def cached(*args, **kwargs):
        key = (args, tuple(sorted(kwargs.items()))) if kwargs else args
        if key not in memo:
            memo[key] = fn(*args, **kwargs)
        return memo[key]

    cached.__name__, cached.__doc__, cached.__wrapped__ = fn.__name__, fn.__doc__, fn
    return cached''',
        "Memoise fn on its arguments, in pure Python, so deep recursion fits.",
    ),
    "deep": Helper(
        ("import threading",),
        (),
        """def deep(fn):
    \"\"\"Run fn on a thread with a 256 MB stack, so deep memo recursion fits.\"\"\"
    out, err = [], []

    def target():
        try:
            out.append(fn())
        except BaseException as e:
            err.append(e)

    threading.stack_size(1 << 28)
    t = threading.Thread(target=target)
    t.start()
    t.join()
    if err:
        raise err[0]
    return out[0]""",
        "Run fn on a thread with a 256 MB stack, so deep memo recursion fits.",
    ),
    "_pushed": Helper(
        (),
        (),
        """def _pushed(d, pairs):
    for k, v in pairs:
        d[k].append(v)
    return d""",
    ),
    "_added": Helper(
        (),
        (),
        """def _added(d, pairs):
    for k, v in pairs:
        d[k].add(v)
    return d""",
    ),
    "_updated": Helper(
        (),
        (),
        """def _updated(d, pairs, op):
    for k, v in pairs:
        d[k] = op(d[k], v)
    return d""",
    ),
}

HELPERS = dict(RUNTIME)  # name -> Helper: RUNTIME plus the profile's library
BUILTINS = {}  # name -> object: what the run time preloads, never pasted
PATHS = []  # extra sys.path entries the profile's code lives under, for the ide
DOCS = []  # markdown files whose ```python blocks document the library, for the ide
REVISION = 0  # bumped by library and environment, so caches can follow


def _imports(tree):
    """alias -> the import line that binds it, for a module's top-level
    imports of other packages. Imports from the helper's own package are
    left out: those names are helpers themselves."""
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                out[a.asname or a.name.split(".")[0]] = f"import {a.name}" + (
                    f" as {a.asname}" if a.asname else ""
                )
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            for a in node.names:
                out[a.asname or a.name] = f"from {node.module} import {a.name}" + (
                    f" as {a.asname}" if a.asname else ""
                )
    return out


class _Strip(ast.NodeTransformer):
    """Annotations and docstrings out: the pasted source stays small and
    needs no typing imports."""

    def _body(self, node):
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        kept = [self.visit(s) for s in body]
        node.body = [s for s in kept if s is not None] or [ast.Pass()]
        return node

    def visit_FunctionDef(self, node):
        node.returns = None
        args = node.args
        for a in args.posonlyargs + args.args + args.kwonlyargs:
            a.annotation = None
        for a in (args.vararg, args.kwarg):
            if a is not None:
                a.annotation = None
        return self._body(node)

    def visit_ClassDef(self, node):
        node.bases = [
            b.value if isinstance(b, ast.Subscript) else b for b in node.bases
        ]
        return self._body(node)

    def visit_AnnAssign(self, node):
        if node.value is None:
            return None
        return ast.Assign([node.target], node.value, lineno=node.lineno)


def stripped(node):
    """A def, class or assignment as source, without annotations or docstrings."""
    node = _Strip().visit(copy.deepcopy(node))
    ast.fix_missing_locations(node)
    return ast.unparse(node)


def library(module, *names):
    """Register helpers from a module: the names given, or every public
    def and class, each with the module-level names it uses (private
    helpers, constants) and, through their own modules, the names it
    imports from its package. Returns the names registered."""
    global REVISION
    path = inspect.getsourcefile(module)
    tree = ast.parse(inspect.getsource(module))
    # a module written in mu (pydsa imports one through mu, and sets
    # __mu__): go-to-definition opens the .mu, at the line of the def
    written = Path(getattr(module, "__mu__", None) or Path(path).with_suffix(".mu"))
    lines = written.read_text().split("\n") if written.is_file() else None
    top = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            top[node.name] = node
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            if isinstance(node.targets[0], ast.Name):
                top[node.targets[0].id] = node
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if isinstance(node.target, ast.Name):
                top[node.target.id] = node
    imports = _imports(tree)
    constants = (ast.Assign, ast.AnnAssign)
    local = [n for n, node in top.items() if not isinstance(node, constants)]
    todo = list(names) or [n for n in local if not n.startswith("_")]
    done = []
    while todo:
        name = todo.pop(0)
        if name in HELPERS:
            continue
        if name not in top:
            obj = getattr(module, name)  # imported from a sibling module
            library(sys.modules[obj.__module__], name)
            continue
        node = top[name]
        source = stripped(node)
        free = {n.id for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Name)}
        free.discard(name)
        deps = tuple(sorted(n for n in free if n in top))
        # a name imported from a sibling module of the package: a helper too
        siblings = [
            n
            for n in free
            if n not in top
            and n not in imports
            and n not in dir(builtins)
            and hasattr(module, n)
            and getattr(getattr(module, n), "__module__", None)
        ]
        deps += tuple(sorted(siblings))
        imps = tuple(sorted(imports[n] for n in free if n in imports))
        doc = ""
        if not isinstance(node, constants):
            doc = (ast.get_docstring(node) or "").split("\n", 1)[0].strip()
        at = (path, node.lineno - 1)
        if lines is not None:
            head = re.compile(rf"^(?:async )?(?:def|class) {name}\b|^{name} =")
            line = next((k for k, ln in enumerate(lines) if head.match(ln)), None)
            if line is not None:
                at = (str(written), line)
        HELPERS[name] = Helper(imps, deps, source, doc, *at)
        done.append(name)
        todo += [d for d in deps if d not in HELPERS]
    REVISION += 1
    return done


# mu modules: `from m import a, b` where m.mu is found reads the defs from
# that file, transpiled, and pastes the ones the file calls, as a library
# helper is pasted. A file's own directory is searched first, then the
# working directory, then $MUPATH, then the directories in MU_PATH.
MU_PATH = []  # directories a profile adds with mu_path
NATIVE = {}  # "<path>:<name>" -> Helper, every def of every mu module read
_MODULES = {}  # path -> (mtime, {name: key}), the names each module defines
_LOADING = []  # the modules being read, to refuse an import cycle
_HERE = []  # the directory of each file being transpiled, innermost last


def mu_path(*dirs):
    """Add directories to search for mu modules."""
    global REVISION
    for d in dirs:
        if str(d) not in MU_PATH:
            MU_PATH.append(str(d))
    REVISION += 1


def helper_of(key):
    """The Helper for a name in HELPERS or a key in NATIVE."""
    return HELPERS[key] if key in HELPERS else NATIVE[key]


def find_module(name, here=None):
    """The path of the mu module `name` (a.b is a/b.mu), or None. here is
    the importing file's directory; the innermost transpile's by default."""
    rel = Path(*name.split(".")).with_suffix(".mu")
    dirs = ([here] if here else _HERE[-1:]) + [os.getcwd()]
    dirs += [d for d in os.environ.get("MUPATH", "").split(os.pathsep) if d]
    for d in dirs + MU_PATH:
        if (Path(d) / rel).is_file():
            return (Path(d) / rel).resolve()
    return None


def load_module(path):
    """{name: key in NATIVE} for the defs, classes and constants a mu module
    defines; its script (tests under the defs) is not part of it. Read again
    when the file changes."""
    path = Path(path).resolve()
    mtime = path.stat().st_mtime
    if path in _MODULES and _MODULES[path][0] == mtime:
        return _MODULES[path][1]
    if path in _LOADING:
        chain = " -> ".join(p.name for p in _LOADING[_LOADING.index(path) :])
        raise MuError(f"an import cycle: {chain} -> {path.name}")
    _LOADING.append(path)
    try:
        src = path.read_text()
        code, origin = transpile(src, mapped=True, path=path)
        script = parsed(src, Parser.program)[0].script_line
    except MuError as err:
        raise MuError(f"{path.name}: {err}") from None
    finally:
        _LOADING.pop()
    tree = ast.parse(code)
    top = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if isinstance(target, ast.Name) and getattr(node, "value", None):
                top[target.id] = node
    imports = _imports(tree)
    exported = {}
    for name, node in top.items():
        line = origin[node.lineno - 1]
        if line is not None and script is not None and line >= script:
            continue  # a script line, not part of the module
        source = stripped(node)
        free = {n.id for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Name)}
        free.discard(name)
        deps = tuple(f"{path}:{n}" for n in sorted(free) if n in top)
        imps = tuple(sorted(imports[n] for n in free if n in imports))
        doc = ""
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            doc = (ast.get_docstring(node) or "").split("\n", 1)[0].strip()
        at = (str(path), line - 1) if line is not None else (None, None)
        NATIVE[f"{path}:{name}"] = Helper(imps, deps, source, doc, *at)
        if line is not None:
            exported[name] = f"{path}:{name}"
    _MODULES[path] = (mtime, exported)
    return exported


def native_import(line, at):
    """{name: key} for an import line that names a mu module, else None."""
    if line.startswith("import "):
        for name in line[7:].split(","):
            if find_module(name.split(" as ")[0].strip()):
                raise MuError(
                    f"line {at}: {name.strip()} is a mu module; write `from {name.strip()} import` and the names"
                )
        return None
    module, names = line[5:].split(" import ", 1)
    if module.startswith("."):
        return None  # relative: an import inside a Python package
    path = find_module(module.strip())
    if path is None:
        return None
    defined = load_module(path)
    out = {}
    for name in (n.strip() for n in names.strip("() ").split(",")):
        if name == "*" or " as " in name:
            raise MuError(f"line {at}: a mu import names each def, with no `*` or `as`")
        if name not in defined:
            raise MuError(f"line {at}: {path.name} defines no {name}")
        out[name] = defined[name]
    return out


def environment(objects):
    """Register names the run time preloads, name -> object, so the ide
    completes and documents them. They are never pasted."""
    global REVISION
    BUILTINS.update(objects)
    REVISION += 1


def document(path):
    """Register a markdown file documenting the library: every line of its
    ```python blocks that starts with a helper's name, `call  # what it
    gives`, is that helper's hover text in the ide."""
    global REVISION
    DOCS.append(str(path))
    REVISION += 1


_PROFILED = set()


def profile(root=None):
    """Load the project's profile: `[tool.mu] profile` in <root>/pyproject.toml
    names a Python file under root, or an importable module, that calls
    library and environment. Once per root; a root without one changes
    nothing."""
    root = Path(root or os.getcwd()).resolve()
    if root in _PROFILED:
        return
    _PROFILED.add(root)
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        return
    try:
        import tomllib
    except ImportError:  # Python 3.10
        import tomli as tomllib
    name = (
        tomllib.loads(pyproject.read_text())
        .get("tool", {})
        .get("mu", {})
        .get("profile")
    )
    if not name:
        return
    target = root / name
    if target.exists():
        if target.stem in sys.modules:
            return
        spec = importlib.util.spec_from_file_location(target.stem, target)
        module = importlib.util.module_from_spec(spec)
        sys.modules[target.stem] = module
        spec.loader.exec_module(module)
    else:
        importlib.import_module(name)


def global_names():
    """Names a value side of an assignment may read without binding them."""
    out = set(dir(builtins)) | set(HELPERS) | {"inf", "self"}
    return out | {"ascii_lowercase", "ascii_uppercase", "ascii_letters"}


# Python annotations as mu types


def mu_type(node):
    """A Python annotation written as a mu type."""
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):  # a quoted annotation: 'Optional[Node]'
            return mu_type(ast.parse(node.value, mode="eval").body)
        return "none" if node.value is None else str(node.value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        sides = [node.left, node.right]
        rest = [
            n for n in sides if not (isinstance(n, ast.Constant) and n.value is None)
        ]
        if len(rest) == 1:  # TreeNode | None
            return f"{mu_type(rest[0])}?"
    if isinstance(node, ast.Name):
        return {"None": "none"}.get(node.id, node.id)
    if isinstance(node, ast.Subscript):
        head = ast.unparse(node.value).split(".")[-1].lower()
        args = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
        if head == "list":
            return f"[{mu_type(args[0])}]"
        if head == "dict":
            return f"{{{mu_type(args[0])}: {mu_type(args[1])}}}"
        if head == "set":
            return f"{{{mu_type(args[0])}}}"
        if head == "tuple":
            return "(" + ", ".join(mu_type(a) for a in args) + ")"
        if head == "optional":
            return f"{mu_type(args[0])}?"
        if head == "callable":
            params = [mu_type(a) for a in args[0].elts]
            takes = params[0] if len(params) == 1 else "(" + ", ".join(params) + ")"
            return f"{takes} -> {mu_type(args[1])}"
    return ast.unparse(node)


# The message of `assert a op b`: both sides through `pprint.pformat`, on one
# line each when both fit, else each side indented under its own label.
SIDES = r"""(lambda l, r: f"left : {l.lstrip()}\nright: {r.lstrip()}" if "\n" not in l + r else f"left :\n{l}\nright:\n{r}")(*(__import__("textwrap").indent(__import__("pprint").pformat(v, width=__import__("shutil").get_terminal_size().columns - 2, sort_dicts=False), "  ") for v in (_l, _r)))"""


class Parser:
    def __init__(self, src, primes=frozenset(), strings=frozenset()):
        self.toks = tokenize(src, primes)
        self.primes = primes  # (line, column) of each `f'` read as a prime
        self.strings = strings  # (line, column) of an `f'` that stays a string
        self.i = 0
        self.primed = {}  # name -> the first line that marks it with a prime
        self.at_line = {}  # id(stmt) -> the mu line it starts on, for the debugger
        self.bare = set()  # helper names used without a call: sort(ds, by=even)
        self.imports = set()
        self.helpers = []
        self.native = {}  # name -> NATIVE key, the names imported from mu modules
        self.imported = set()  # the names Python imports bind
        self.memo_used = False
        self.folds = []  # (op, from, if, block) for each fold, for folds() in asserts
        self.known = [set()]  # names bound so far: the file's, then each open def's
        self.pending = []  # Hoisted defs for the statement being parsed
        self.mu = []  # the hidden name of each open block lambda, innermost last
        self.hoisted = 0  # how many hidden defs are named so far

    # token helpers

    def peek(self, k=0):
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def at(self, val, k=0):
        t = self.peek(k)
        return t[1] == val and t[0] in ("OP", "NAME")

    def next(self):
        t = self.toks[self.i]
        self.i += 1
        return t

    def expect(self, val):
        t = self.next()
        if t[1] != val:
            got = t[1] or t[0]
            raise MuError(f"line {t[2]}: expected {val!r}, got {got!r}", t[0] == "EOF")
        return t

    def expect_kind(self, kind):
        t = self.next()
        if t[0] != kind:
            got = t[1] or t[0]
            raise MuError(
                f"line {t[2]}: expected {kind.lower()}, got {got!r}", t[0] == "EOF"
            )
        return t[1]

    def need(self, imp=None, helper=None):
        if imp and not imported(imp) <= self.imported:  # the file imports it already
            self.imports.add(imp)
        if helper and helper not in self.helpers:
            h = helper_of(helper)
            for d in h.deps:
                self.need(helper=d)
            self.imports.update(h.imports)
            self.helpers.append(helper)

    # statements

    def program(self):
        """imports, top-level assignments (constants, a Flag class), then
        the classes and defs of the file, in order, then a script: any
        statements, run after them. A def inside a class is a method; a
        top-level def is a plain function."""
        tops, self.globals, self.script = [], [], []
        self.script_line = None  # where the script starts
        self.docstring = None  # a string the file opens with, its __doc__
        if self.peek()[0] == "STR" and self.peek(1)[0] == "NEWLINE":
            self.docstring = self.next()[1]
            self.next()
        while self.peek()[0] != "EOF":
            t = self.peek()
            if self.script_line is None and (self.at("import") or self.at("from")):
                line = self.import_line()
                if line is not None:
                    self.imports.add(line)
            elif self.script_line is None and self.top_ahead():
                tops.append(self.stmt())
            else:
                for s in flat(self.stmt()):
                    # an assignment after a script line stays in the script,
                    # where it runs in its turn
                    lead = not tops and self.script_line is None
                    if lead and s[0] in ("assign", "annassign"):
                        self.globals.append(s)
                    else:
                        if self.script_line is None:
                            self.script_line = t[2]
                        self.script.append(s)
        return tops

    def top_ahead(self):
        """Whether a class or a def starts here, under any `@` lines."""
        j = self.i
        while self.toks[j][:2] == ("OP", "@"):
            while self.toks[j][0] not in ("NEWLINE", "EOF"):
                j += 1
            j += self.toks[j][0] == "NEWLINE"
        j += self.toks[j][:2] == ("NAME", "async")
        return self.toks[j][:2] in (("NAME", "def"), ("NAME", "class"))

    def import_line(self):
        """`import a.b` or `from a.b import c, d`, passed through to Python;
        None for an import from a mu module, whose defs are pasted."""
        at = self.peek()[2]
        words = []
        while self.peek()[0] != "NEWLINE":
            words.append(self.next()[1])
        self.next()
        if words[0] == "from":
            if "import" not in words or words[-1] == "import":
                raise MuError(f"line {at}: expected from module import names")
            k = words.index("import")
            names = " ".join(words[k + 1 :]).replace(" ,", ",")
            line = f"from {''.join(words[1:k])} import {names}"
        else:
            line = "import " + re.sub(r" ?\. ?", ".", " ".join(words[1:])).replace(" ,", ",")
        self.known[0] |= imported(line)
        names = native_import(line, at)
        if names is None:
            self.imported |= imported(line)  # bound by Python: never a helper
            return line
        self.native.update(names)
        return None

    def statements(self):
        """Any statements, for the REPL."""
        out = []
        while self.peek()[0] != "EOF":
            out += flat(self.stmt())
        return out

    def block(self):
        """An indented block, or one statement after ':' on the same line."""
        if self.at(":"):
            self.next()
            return flat(self.placed(self.simple(), len(self.pending)))
        self.expect_kind("NEWLINE")
        self.expect_kind("INDENT")
        body = []
        while self.peek()[0] != "DEDENT":
            body += flat(self.stmt())
        self.next()
        return body

    def stmt(self):
        decorators = self.decorators()
        line = self.peek()[2]
        mark = len(self.pending)
        node = self.stmt_at()
        if decorators:
            node = Decorated(node)
            node.decorators = decorators
        node = self.placed(node, mark)
        for s in flat(node):
            self.at_line.setdefault(id(s), line)
        return node

    def placed(self, node, mark):
        """node with the defs hoisted while it was parsed (from mark on)
        placed before it: a block lambda is built where its statement runs."""
        hoisted = self.pending[mark:]
        del self.pending[mark:]
        return ("stages", hoisted + flat(node)) if hoisted else node

    def hoisted_reads(self, mark, tgt, line):
        """A block lambda inside a comprehension is built before the
        statement, where the comprehension's variable is out of reach:
        refuse one whose body reads it."""
        names = set(NAME.findall(tgt))
        for d in self.pending[mark:]:
            for name in sorted(d.reads & names):
                raise MuError(
                    f"line {line}: the mu block reads {name} from the loop"
                    f" around it; take {name} as a parameter"
                )

    def decorators(self):
        """The `@f` lines above a def or a memo: (line, expression) for each.
        The expression passes through to Python."""
        out = []
        while self.at("@"):
            line = self.next()[2]
            out.append((line, self.expr()))
            self.expect_kind("NEWLINE")
        defs = self.at("def") or self.at("async") and self.at("def", 1)
        if out and not (defs or self.at("memo") or self.at("class")):
            t = self.peek()
            raise MuError(
                f"line {out[-1][0]}: a decorator goes above a def, a memo or a class",
                t[0] == "EOF",
            )
        return out

    def stmt_at(self):
        t = self.peek()
        if self.at("import") or self.at("from"):
            return ("import", self.import_line())
        if self.at("def"):
            return self.def_stmt()
        if self.at("async") and self.at("def", 1):
            # `async def f()`: Python's coroutine, its body may `await`
            self.next()
            s = self.def_stmt()
            s[1].is_async = True
            return s
        if self.at("class"):
            return self.class_stmt()
        if self.at("extends"):
            raise MuError(
                f"line {t[2]}: extends goes on the class line: class Solution extends Base"
            )
        if self.at("memo") and self.peek(1)[0] == "NAME":
            return self.memo_stmt()
        if self.at("for"):
            self.next()
            it = self.repeat()
            if it is None:
                tgt, wrap = self.target()
                self.expect("in")
                it = iterable(self.expr(), wrap)
            else:
                tgt = "_"
            self.known[-1] |= set(NAME.findall(tgt))
            return ("for", tgt, it, self.block(), self.loop_else())
        if self.at("while"):
            self.next()
            return ("while", self.expr(), self.block(), self.loop_else())
        if self.at("with"):
            return self.with_stmt()
        if self.at("try"):
            return self.try_stmt()
        if self.at("if"):
            arms = []
            self.next()
            arms.append((self.expr(), self.block()))
            while self.at("elif"):
                line = self.next()[2]
                arms.append((self.expr(), self.headed(self.block(), line)))
            return ("if", arms, self.loop_else())
        if (
            t[1] in FOLDS
            and t[0] == "NAME"
            and (self.at("for", 1) or self.at("from", 1))
        ):
            head = self.fold(allow_block=True)
            if isinstance(head, tuple):
                self.known[-1] |= set(NAME.findall(head[2]))
                return ("foldblock", *head, self.lifted(self.block(), head[2]))
            self.expect_kind("NEWLINE")
            return ("expr", head)
        if self.first_block_ahead():
            return ("expr", self.first(block=True))
        if (
            self.at("ret")
            and self.peek(1)[0] == "NAME"
            and (self.at("=", 2) or self.at(",", 2))
        ):
            # `ret res = [first]`, `ret res, k = [], 0`: res is the def's
            # return value
            self.next()
            name = self.peek()[1]
            s = self.simple(newline=True)
            if s[0] not in ("assign", "assign_fold"):
                raise MuError(f"line {t[2]}: a ret line takes one plain assignment")
            return (s[0], *s[1:], name)
        return self.simple(newline=True)

    def with_stmt(self):
        """`with a() as x, b()`, then its block: Python's with, no colon."""
        self.expect("with")
        items = []
        while True:
            e = self.expr()
            if self.at("as"):
                self.next()
                tgt = self.pattern()
                self.known[-1] |= set(NAME.findall(tgt))
                e = Py(f"{e} as {tgt}")
            items.append(e)
            if not self.at(","):
                break
            self.next()
        return ("with", ", ".join(items), self.block())

    def try_stmt(self):
        """`try`, its block, then `except [E [as e]]` blocks, an `else` and a
        `finally`, each a header on its own line, as in Python."""
        self.expect("try")
        body, handlers = self.block(), []
        while self.at("except"):
            line = self.next()[2]
            head = ""
            if not self.at(":") and self.peek()[0] != "NEWLINE":
                head = self.expr()
                if self.at("as"):
                    self.next()
                    name = self.expect_kind("NAME")
                    self.known[-1].add(name)
                    head = f"{head} as {name}"
            handlers.append((head, self.headed(self.block(), line)))
        other = self.loop_else()
        final = None
        if self.at("finally"):
            line = self.next()[2]
            final = self.headed(self.block(), line)
        if not handlers and final is None:
            t = self.peek()
            raise MuError(f"line {t[2]}: a try needs an except or a finally")
        return ("try", body, handlers, other, final)

    def loop_else(self):
        """The block of an `else`: a loop's, run when the loop ends without
        break, or an if's."""
        if self.at("else"):
            line = self.next()[2]
            return self.headed(self.block(), line)
        return None

    def headed(self, body, line):
        """A block whose header (`elif`, `else`) sits on its own mu line:
        the emitter maps the header to that line, not to the statement before."""
        self.at_line[id(body)] = line
        return body

    def simple(self, newline=False):
        if self.at("return"):
            self.next()
            val = None if self.peek()[0] == "NEWLINE" else self.exprlist()
            s = ("return", val)
        elif self.at("break") or self.at("continue") or self.at("pass"):
            s = (self.next()[1],)
        elif self.at("yield"):
            self.next()
            if self.at("from"):
                self.next()
                s = ("yield", f"from {self.expr()}")
            else:
                s = ("yield", None if self.peek()[0] == "NEWLINE" else self.exprlist())
        elif self.at("del"):
            self.next()
            s = ("del", self.exprlist())
        elif self.at("assert"):
            self.next()
            s = ("assert", self.exprlist())
        elif self.at("raise"):
            self.next()
            val = None if self.peek()[0] == "NEWLINE" else self.expr()
            if val is not None and self.at("from"):
                self.next()
                val = Py(f"{val} from {self.expr()}")
            s = ("raise", val)
        elif self.at("global") or self.at("nonlocal"):
            kind = self.next()[1]
            names = [self.expect_kind("NAME")]
            while self.at(","):
                self.next()
                names.append(self.expect_kind("NAME"))
            self.known[-1] |= set(names)
            s = (kind, ", ".join(names))
        else:
            line = self.peek()[2]
            lhs = self.exprlist()
            if self.at("<-"):
                # push: `stack <- x` appends x
                self.next()
                s = ("push", f"{lhs}.append({self.expr()})")
            elif self.at(":"):
                # `n_layer: int = 12`: a typed assignment, or a field `x: T`
                self.next()
                t = self.type()
                val = None
                if self.at("="):
                    self.next()
                    val = self.arg() if self.lambda_ahead() else self.exprlist()
                self.known[-1] |= targets(lhs)
                s = ("annassign", lhs, t, val)
            elif self.peek()[1] in AUGMENTED and self.peek()[0] == "OP":
                op = self.next()[1]
                if op == "=" and self.fold_block_ahead():
                    # `res = count for x in xs` then a block: the fold's value
                    head = self.fold(allow_block=True)
                    self.known[-1] |= set(NAME.findall(head[2])) | targets(lhs)
                    body = self.lifted(self.block(), head[2])
                    return ("assign_fold", lhs, op, head, body)
                rhs = (
                    self.arg()
                    if op == "=" and self.lambda_ahead()
                    else self.exprlist(repeat=op == "=")
                )
                sides = [lhs]
                while op == "=" and self.at("="):
                    # chained: `a = b = 0` keeps every target in lhs
                    self.next()
                    sides.append(rhs)
                    if rhs.repeats:
                        raise MuError(f"line {line}: a repeat `x..` cannot be in a chained `=`")
                    lhs, rhs = Py(f"{lhs} = {rhs}"), self.exprlist(repeat=True)
                    if rhs.repeats:
                        raise MuError(f"line {line}: a repeat `x..` cannot be in a chained `=`")
                if op == "=" and len(sides) == 1:
                    rhs = filled(lhs, rhs, line)
                # `int(a) = x` alone converts a, as `int(a), b = x` does
                alone = op == "=" and len(sides) == 1 and CONVERT.fullmatch(lhs)
                if op == "=" and not alone and any(computed(x) for x in sides):
                    s = self.stages(sides, rhs, line)
                else:
                    if op == "=":
                        lhs = converted(lhs)
                        self.known[-1] |= targets(lhs)
                    s = ("assign", lhs, op, rhs)
                    if alone:
                        # and a is the line's value, when the block ends here
                        s = ("stages", [s, ("tail", lhs)])
            else:
                s = ("expr", lhs)
        if newline or self.peek()[0] == "NEWLINE":
            self.expect_kind("NEWLINE")
        return s

    def fold_block_ahead(self):
        """Whether a fold with a block starts here: `sum for ...` or `sum
        from ...` with no `:` on the line, and a block under it."""
        t = self.peek()
        if not (t[0] == "NAME" and t[1] in FOLDS and (self.at("for", 1) or self.at("from", 1))):
            return False
        j, depth = self.i + 1, 0
        while self.toks[j][0] not in ("NEWLINE", "EOF"):
            val = self.toks[j][1]
            depth += val in "([{"
            depth -= val in ")]}"
            if depth == 0 and val == ":":
                return False
            j += 1
        return self.toks[j + 1][0] == "INDENT"

    def lifted(self, body, tgt):
        """body, less the block lambdas in it that read nothing the fold's
        block binds: those go to pending, to be built once before the fold.
        One that reads a name the block sets per item stays, built per item."""
        held = bound(body) | set(NAME.findall(tgt))
        kept = []
        for s in body:
            if isinstance(s, Hoisted) and not (s.reads - {s[1]}) & held:
                self.pending.append(s)
            else:
                kept.append(s)
        return kept

    def stages(self, sides, rhs, line):
        """`a * b = pair`: a side of `=` that is a value, not a target. Each
        such side binds its new names from the value on its right, one name
        to the whole value and several by unpacking, then is that value for
        the side on its left. The names are bound right to left."""
        seen = global_names().union(*self.known)
        out, waiting, val = [], [], rhs
        for side in reversed(sides):
            marked = re.findall(PRIMED + r"(\w+)", side)
            side = Py(side.replace(PRIMED, ""))
            if not computed(side, seen):
                if marked:
                    raise MuError(
                        f"line {line}: `{marked[0]}'` is a target; a prime marks"
                        " a name bound again on a value side of `=`"
                    )
                waiting.insert(0, side)
                continue
            for name in marked:
                if name not in set().union(*self.known):
                    raise MuError(
                        f"line {line}: `{name}'` marks a name bound again,"
                        f" but {name} is not bound yet"
                    )
            names = [n for n in free_names(side) if n not in seen or n in marked]
            if not names:
                read = ", ".join(free_names(side))
                why = f"{read} already defined" if read else "it names nothing"
                raise MuError(f"line {line}: `{side}` binds no new name: {why}")
            lhs = " = ".join(waiting + [", ".join(names)])
            out.append(("assign", Py(lhs), "=", val))
            self.known[-1] |= targets(" = ".join(waiting))
            waiting, val = [], side
        if waiting:
            self.known[-1] |= targets(" = ".join(waiting))
            out.append(("assign", Py(" = ".join(waiting)), "=", val))
        else:
            out.append(("expr", val))
        return ("stages", out)

    def def_stmt(self):
        self.expect("def")
        name = Py(self.expect_kind("NAME"))
        self.expect("(")
        params, keyword_only = [], False
        while not self.at(")"):
            star = self.next()[1] if self.at("*") or self.at("**") else ""
            if star == "*" and (self.at(",") or self.at(")")):
                # a bare `*`: the parameters after it are named in the call
                p = Py("")
                p.star, keyword_only = "*", True
                params.append((p, None))
                if not self.at(")"):
                    self.expect(",")
                continue
            p = Py(self.expect_kind("NAME"))
            p.star = star
            keyword_only = keyword_only or star == "*"
            t = None
            if self.at(":"):
                self.next()
                t = self.type()
            if self.at("=") and not star:
                # a default: `by=x -> x`, `reverse: bool = false`
                self.next()
                p.default = self.arg()
            elif not star and not keyword_only and any(
                q.default is not None for q, _ in params
            ):
                raise MuError(
                    f"line {self.peek()[2]}: `{p}` needs a default, as the parameters before it have one"
                )
            params.append((p, t))
            if not self.at(")"):
                self.expect(",")
        self.expect(")")
        ret = None
        if self.at("->"):
            self.next()
            ret = self.type()
        self.known[-1].add(name)
        self.known.append({p for p, _ in params})
        body = self.block()
        self.known.pop()
        if len([s for s in body if ret_name([s])]) > 1:
            raise MuError(f"{name} has more than one ret line")
        if any(ret_name(b) for s in body for b in children(s)):
            raise MuError("a ret line must be a line of the def's own body")
        return ("def", name, params, ret, body)

    def class_stmt(self):
        """`class Name`, or `class Name extends Base`, then its body: defs,
        which are its methods, and assignments, its attributes."""
        line = self.expect("class")[2]
        name = self.expect_kind("NAME")
        base = None
        if self.at("extends"):
            self.next()
            bases = [self.dotted()]
            while self.at(","):
                self.next()
                bases.append(self.dotted())
            base = ", ".join(bases)
        self.known[-1].add(name)
        self.known.append(set())
        body = self.block()
        self.known.pop()
        if any(s[0] == "class" for s in body):
            raise MuError(f"line {line}: a class holds no class")
        return ("class", name, base, body)

    def dotted(self):
        """A dotted name: `nn.Module`."""
        name = self.expect_kind("NAME")
        while self.at("."):
            self.next()
            name += "." + self.expect_kind("NAME")
        return name

    def memo_stmt(self):
        self.expect("memo")
        self.memo_used = True
        self.need(helper="cache")
        self.need("import sys")
        self.need(helper="deep")
        name = self.expect_kind("NAME")
        self.expect("(")
        params = []
        while not self.at(")"):
            params.append(self.expect_kind("NAME"))
            if not self.at(")"):
                self.expect(",")
        self.expect(")")
        self.known[-1].add(name)
        if not self.at("="):  # block form: a def, cached
            self.known.append(set(params))
            body = self.block()
            self.known.pop()
            if any(ret_name([s]) for s in body):
                raise MuError(f"memo {name} cannot have a ret line")
            return ("def", name, [(p, None) for p in params], None, body, "memo")
        self.expect("=")
        cases = []
        if self.peek()[0] != "NEWLINE":
            cases.append((None, self.expr()))
            self.expect_kind("NEWLINE")
            return ("memo", name, params, cases)
        self.expect_kind("NEWLINE")
        self.expect_kind("INDENT")
        cases = self.cases()
        self.expect_kind("DEDENT")
        return ("memo", name, params, cases)

    def cases(self):
        """`| guard -> value` lines, one case each; `| else ->` matches
        everything. (guard, value) for each, the guard None for else."""
        cases = []
        while self.at("|"):
            self.next()
            guard = None
            if self.at("else"):
                self.next()
            else:
                guard = self.expr()
            self.expect("->")
            cases.append((guard, self.expr()))
            self.expect_kind("NEWLINE")
        return cases

    def cases_body(self, cases):
        """A case list as the body of a def: each guarded case returns its
        value when the guard holds, an else case is the value."""
        body = []
        for guard, val in cases:
            if guard is None:
                body.append(("expr", val))
                break
            body.append(("if", [(guard, [("return", val)])], None))
        return body

    def type(self):
        """[T] list, {K: V} dict, {T} set, (A, B) tuple, T? optional,
        A -> B or (A, B) -> C function; any other name passes through."""
        t = self.base_type()
        while self.at("|"):  # int | str, Python's union
            self.next()
            t = Py(f"{t} | {self.base_type()}")
        if self.at("->") and not self.at("NEWLINE", 1):
            self.next()
            self.need("from typing import Callable")
            args = ", ".join(t.parts) if t.parts is not None else t
            return Py(f"Callable[[{args}], {self.type()}]")
        return t

    def base_type(self):
        if self.at("["):
            self.next()
            inner = self.type()
            self.expect("]")
            t = Py(f"list[{inner}]")
        elif self.at("{"):
            self.next()
            key = self.type()
            if self.at(":"):
                self.next()
                t = Py(f"dict[{key}, {self.type()}]")
            else:
                t = Py(f"set[{key}]")
            self.expect("}")
        elif self.at("("):
            self.next()
            parts = [self.type()]
            while self.at(","):
                self.next()
                parts.append(self.type())
            self.expect(")")
            t = Py(f"tuple[{', '.join(parts)}]")
            t.parts = parts
        elif self.peek()[0] == "STR":  # a class not defined yet: "GPT"
            t = Py(self.next()[1])
        else:
            name = self.dotted()
            t = Py({"char": "str", "none": "None"}.get(name, name))
            if self.at("["):  # a Python generic: Optional[str], Tensor[...]
                self.next()
                # Callable[[A, B], C]: its first argument is a list of types
                args = [self.generic_arg(name.split(".")[-1] == "Callable")]
                while self.at(","):
                    self.next()
                    args.append(self.generic_arg())
                self.expect("]")
                t = Py(f"{t}[{', '.join(args)}]")
        if self.at("?"):
            self.next()
            self.need("from typing import Optional")
            t = Py(f"Optional[{t}]")
        return t

    def generic_arg(self, types=False):
        """An argument of a Python generic: a type, or `...` as in
        Tuple[int, ...]. With types, `[A, B]` is a list of types, the
        parameters of Callable[[A, B], C]; elsewhere `[A]` is mu's list."""
        if self.at("..."):
            self.next()
            return Py("...")
        if types and self.at("["):
            self.next()
            args = [] if self.at("]") else [self.type()]
            while self.at(","):
                self.next()
                args.append(self.type())
            self.expect("]")
            return Py(f"[{', '.join(args)}]")
        return self.type()

    def repeat(self):
        """`for xs` with no `in`: the iterable of a loop that names no
        variable, or None when a target and `in` follow."""
        return None if self.names_a_target() else self.expr()

    def names_a_target(self):
        """True when a target and `in` come next, as in `for x in xs`."""
        start = self.i
        try:
            self.target()
            return self.at("in")
        except TargetError:
            return True
        except MuError:
            return False
        finally:
            self.i = start

    def target(self):
        """The names a loop binds, and how its iterable is wrapped: `x` or
        `a, b` (None, the iterable as written), `>i` ("range", the indices
        of the iterable), or `>i, x` ("enumerate", index and item). `>`
        marks the index and runs left to right; `<` marks it and runs right
        to left ("range_back", "enumerate_back"). Only the first name may
        carry the mark."""
        if self.at("~"):
            raise TargetError(
                f"line {self.peek()[2]}: ~ no longer marks the index: "
                "write >i for the index, <i for the index from the end"
            )
        if self.at(">") or self.at("<"):
            mark = self.next()[1]
            back = "_back" if mark == "<" else ""
            index = self.expect_kind("NAME")
            if not self.at(","):
                return index, "range" + back
            self.next()
            item = self.pattern()
            if self.at(","):
                raise TargetError(
                    f"line {self.peek()[2]}: {mark}{index}, a, b: "
                    f"write {mark}{index}, (a, b)"
                )
            return f"{index}, {item}", "enumerate" + back
        first = self.pattern()
        if not self.at(","):
            return first, None
        parts = [first]
        while self.at(","):
            self.next()
            if self.at(">") or self.at("<"):
                raise TargetError(
                    f"line {self.peek()[2]}: {self.peek()[1]} goes on the first name only"
                )
            parts.append(self.pattern())
        return ", ".join(parts), None

    def pattern(self):
        if self.at("("):
            self.next()
            parts = [self.pattern()]
            while self.at(","):
                self.next()
                parts.append(self.pattern())
            self.expect(")")
            return "(" + ", ".join(parts) + ")" if len(parts) > 1 else parts[0]
        return self.expect_kind("NAME")

    # expressions, lowest precedence first

    def exprlist(self, repeat=False):
        """Items apart by commas. With repeat, an item may end in dots, one
        copy of it per dot: `0..., 1..` is 0, 0, 0, 1, 1."""
        parts, counts = [self.starred()], [self.copies(repeat)]
        while self.at(","):
            self.next()
            if self.peek()[0] in ("NEWLINE", "EOF") or self.peek()[1] in ("=", ")"):
                parts[-1] += ","  # a trailing comma: `x = 1,` is a tuple
                break
            parts.append(self.starred())
            counts.append(self.copies(repeat))
        out = parts[0] if len(parts) == 1 else Py(", ".join(parts))
        if any(c > 1 for c in counts):
            out = Py(out)
            out.repeats = list(zip(parts, counts))
        return out

    def repeat_ahead(self):
        """The next token is dots that end an item: `[]..,` or `0...` at
        the end of the line."""
        t = self.peek()
        return (
            t[0] == "OP"
            and len(t[1]) >= 2
            and set(t[1]) == {"."}
            and (self.peek(1)[0] in ("NEWLINE", "EOF") or self.at(",", 1))
        )

    def copies(self, allowed):
        """The copies of the item just read: the count of its dots, else 1."""
        if not self.repeat_ahead():
            return 1
        t = self.next()
        if not allowed:
            raise MuError(f"line {t[2]}: `{t[1]}` repeats a value only on the right of `=`")
        return len(t[1])

    def starred(self):
        """An item of a comma list, which may unpack: `a, *rest = xs`."""
        if self.at("*"):
            self.next()
            return Py(f"*{self.expr()}")
        return self.expr()

    def expr(self):
        if self.peek()[0] == "NAME" and self.at(":=", 1):
            name = self.next()[1]
            self.next()
            self.known[-1].add(name)
            return Py(f"{name} := {self.expr()}")
        e = self.or_()
        if self.at("?"):
            return self.ternary(e)
        if self.at("if") and not self.at(":", 1):
            self.next()
            cond = self.or_()
            if e.orparts and not self.at("else"):
                return self.or_if(e.orparts, cond)
            self.expect("else")
            out = Py(f"{e} if {cond} else {self.expr()}")
            out.cond = True
            return out
        return e

    def ternary(self, cond):
        """`c ? a : b` is Python's `a if c else b`. It nests to the right:
        `c ? a : d ? b : e` is a when c, else b when d, else e. A conditional
        in the `a` branch is bracketed so Python reads it as the branch."""
        self.expect("?")
        a = self.arg() if self.lambda_ahead() else self.expr()
        self.expect(":")
        b = self.arg() if self.lambda_ahead() else self.expr()
        a = Py(f"({a})") if a.startswith("lambda") else a
        out = Py(f"{f'({a})' if a.cond else a} if {cond} else {b}")
        out.cond = True
        return out

    def or_if(self, parts, sentinel):
        """`x or d if v`: x, or d when x equals v. x is evaluated once."""
        x, d = parts
        if NAME.fullmatch(x):
            return Py(f"({d} if {x} == {sentinel} else {x})")
        return Py(f"({d} if (_v := {x}) == {sentinel} else _v)")

    def or_(self):
        e = self.and_()
        while self.at("or"):
            self.next()
            lhs, rhs = e, self.and_()
            e = Py(f"{lhs} or {rhs}")
            e.orparts = (lhs, rhs)
        return e

    def and_(self):
        e = self.not_()
        while self.at("and"):
            self.next()
            e = Py(f"{e} and {self.not_()}")
        return e

    def not_(self):
        if self.at("not"):
            self.next()
            return Py(f"not {self.not_()}")
        return self.cmp()

    def cmp(self):
        e = self.range_()
        while True:
            if (
                self.peek()[1] in ("==", "!=", "<", ">", "<=", ">=")
                and self.peek()[0] == "OP"
            ):
                op = self.next()[1]
            elif self.at("in"):
                self.next()
                op = "in"
            elif self.at("not") and self.at("in", 1):
                self.i += 2
                op = "not in"
            elif self.at("is"):
                self.next()
                op = "is"
                if self.at("not"):
                    self.next()
                    op = "is not"
            else:
                return e
            lhs, rhs = e, self.range_()
            e = Py(f"{lhs} {op} {rhs}")
            e.cmpparts = None if getattr(lhs, "cmpparts", None) else (lhs, op, rhs)

    def range_(self):
        e = self.bitor()
        if (self.at("..") and not self.repeat_ahead()) or self.at("..<"):
            inclusive = self.next()[1] == ".."
            hi = self.bitor()
            end = f"{hi} + 1" if inclusive else hi
            r = Py(f"range({e}, {end})")
            r.rng = (e, end)
            return r
        return e

    def binary(self, ops, operand):
        e = operand()
        while self.peek()[1] in ops and self.peek()[0] == "OP":
            op = self.next()[1]
            e = Py(f"{e} {op} {operand()}")
        return e

    def bitor(self):
        return self.binary(("|",), self.bitxor)

    def bitxor(self):
        return self.binary(("^",), self.bitand)

    def bitand(self):
        return self.binary(("&",), self.shift)

    def shift(self):
        return self.binary(("<<", ">>"), self.arith)

    def arith(self):
        e = self.term()
        while self.peek()[1] in ("+", "-") and self.peek()[0] == "OP":
            op = self.next()[1]
            e = Py(f"{e} {op} {self.term()}")
        return e

    def term(self):
        e = self.unary()
        while self.peek()[1] in ("*", "/", "//", "%", "@") and self.peek()[0] == "OP":
            op = self.next()[1]
            rhs = self.unary()
            a = e
            e = Py(f"{e} {op} {rhs}")
            if op == "/":
                e.div = (a, rhs)
        return e

    def unary(self):
        if self.at("-") or self.at("~"):
            op = self.next()[1]
            return Py(f"{op}{self.unary()}")
        if self.at("await"):
            self.next()
            return Py(f"await {self.unary()}")
        e = self.postfix()
        if self.at("**"):
            self.next()
            return Py(f"{e} ** {self.unary()}")
        return e

    def operand_next(self):
        """The next token can start an argument of a call without brackets."""
        t = self.peek()
        if t[0] in ("NUM", "STR"):
            return True
        if t[0] != "NAME" or t[1] in NOT_ARGS:
            return False
        if t[1] in FOLDS and (self.at("for", 1) or self.at("from", 1)):
            return False
        return not (t[1] == "first" and self.at("in", 2))

    def postfix(self, nested=False):
        """A call may drop its brackets when it has one argument: `f x` is
        f(x), `print len xs` is print(len(xs)), and it binds tighter than any
        operator, so `len xs - 1` is len(xs) - 1.

        A `for` after the argument makes it a generator: `set f y for y in
        xs` is set(f(y) for y in xs). The innermost call `f y` is the
        generator's element, so it leaves the `for` to the call around it."""
        kind = self.peek()[0]
        e = self.postfix_chain()
        # a name, or a method of a string: `", ".join parts`
        method = self.toks[self.i - 1][0] == "NAME" and self.toks[self.i - 2][1] == "."
        named = kind == "NAME" or kind == "STR" and method
        if named and self.operand_next():
            mark = len(self.pending)
            arg = self.postfix(nested=True)
            if self.at("for") and (arg.call or not nested):
                arg = Py(f"{arg}{self.comprehension(mark)}")
            out = self.special(e, [arg], [])
            out.call = True
            return out
        return e

    def postfix_chain(self):
        e = self.primary()
        while True:
            if self.at("("):
                e = self.call(e)
            elif self.at("["):
                self.next()
                idx = self.subscript()
                self.expect("]")
                e = Py(f"{e}[{idx}]")
            elif self.at("."):
                self.next()
                if self.peek()[0] == "NAME" and self.peek()[1] not in STATEMENT_WORDS:
                    e = Py(f"{e}.{self.next()[1]}")
                else:
                    e = Py(f"{e}.pop()")  # pop: `stack .` removes the last item
            else:
                return e

    def glued(self, num):
        """The token after the number num starts where num ends, and it is a
        name or `(`: `2x`, `2(a)`. A space between them is an error."""
        t = self.peek()
        if t[2] != num[2] or not (t[0] == "NAME" or t[1] == "("):
            return False
        if t[0] == "NAME" and (t[1] in NOT_ARGS or t[1] in KEYWORDS):
            return False  # `1if c else 0`
        return t[1].col == num[1].col + len(num[1])

    def subscript(self):
        """An index `i`, a slice `lo:hi:step` with parts optional, or several
        of them apart by commas: `x[:, -1]`, `x[..., :3]`."""
        items = [self.slice_item()]
        while self.at(","):
            self.next()
            if self.at("]"):
                items[-1] += ","
                break
            items.append(self.slice_item())
        return ", ".join(items)

    def slice_item(self):
        parts, cur = [], None
        while True:
            if self.at(":"):
                self.next()
                parts.append(cur or "")
                cur = None
            elif self.at("]") or self.at(","):
                parts.append(cur or "")
                break
            else:
                cur = self.starred()
        return ":".join(parts)

    def primary(self):
        t = self.peek()
        kind, val = t[0], t[1]
        if kind == "STR" and PREFIXED.match(val):
            name = val[: val.index("'")]
            at = (t[2], val.col)
            if name in set().union(*self.known) and at not in self.strings:
                raise Reprime(at)
        if kind == "STR":
            # adjacent strings are one, as in Python: "a" "b"
            parts = [self.next()[1]]
            while self.peek()[0] == "STR":
                parts.append(self.next()[1])
            return Py(" ".join(parts))
        if kind == "NUM":
            self.next()
            nxt = self.peek()
            if not self.glued(t):
                if nxt[0] == "NAME" and nxt[2] == t[2] and nxt[1] not in NOT_ARGS | KEYWORDS:
                    glue = "" if nxt[1][0] in "eE" else f"{val}{nxt[1]}, with no space, or "
                    raise MuError(f"line {t[2]}: write {glue}{val} * {nxt[1]}")
                return Py(val)
            # `2x` is 2 * x, `2(a + b)` is 2 * (a + b); `**` binds tighter,
            # so 2x**2 is 2 * x**2, and the product is one operand: 1/2x is
            # 1 / (2 * x)
            if nxt[0] == "NAME" and nxt[1][0] in "eE":
                raise MuError(
                    f"line {t[2]}: `{val}{nxt[1]}` reads as an exponent; write {val} * {nxt[1]}"
                )
            if nxt[0] == "NAME" and val == "0" and nxt[1][0] in "xXbBoO":
                raise MuError(
                    f"line {t[2]}: `0{nxt[1]}` reads as a hex, binary or octal number; write 0 * {nxt[1]}"
                )
            e = self.postfix_chain()
            if self.at("**"):
                self.next()
                e = Py(f"{e} ** {self.unary()}")
            return Py(f"({val} * {e})")
        if val == "..." and kind == "OP":
            self.next()
            return Py("...")
        if kind == "NAME" and self.peek(1)[0] == "PRIME":
            # `min_price'`: the new value of a bound name, on a value side
            self.next()
            self.next()
            self.primed.setdefault(val, t[2])
            return Py(PRIMED + val)
        if kind == "NAME":
            if val in FOLDS and (self.at("for", 1) or self.at("from", 1)):
                return self.fold()
            if val == "first" and self.peek(1)[0] == "NAME" and self.at("in", 2):
                return self.first()
            if val in KEYWORDS:
                raise MuError(f"line {t[2]}: unexpected {val!r}")
            if val == "memo":
                # `memo {n -> ...}`: a block lambda whose def is cached
                self.next()
                if not (self.at("{") and self.block_lambda_ahead()):
                    raise MuError(
                        f"line {t[2]}: memo goes before a name or a block `{{n ->`"
                    )
                return self.block_lambda(memo=True)
            if val == "mu":
                # the block lambda around it, by its hidden name
                if not self.mu:
                    raise MuError(f"line {t[2]}: mu names the block lambda around it; here there is none")
                self.next()
                return Py(self.mu[-1])
            self.next()
            if val == "inf":
                self.need("from math import inf")
            if (val in HELPERS and val not in self.imported) or val in self.native:
                self.bare.add(val)
            return Py(CONSTANTS.get(val, val))
        if val == "(":
            self.next()
            if self.at(")"):
                self.next()
                return Py("()")
            mark = len(self.pending)
            parts = [self.pushed(self.item("("))]  # (*e, 1): a starred item
            if self.at("for"):  # a generator: (e for x in xs)
                gen = self.comprehension(mark)
                self.expect(")")
                return Py(f"({parts[0]}{gen})")
            one = False  # (x,) is a one-element tuple
            while self.at(","):
                self.next()
                if self.at(")"):
                    one = len(parts) == 1
                    break
                parts.append(self.item("("))
            self.expect(")")
            return Py(f"({', '.join(parts)}{',' if one else ''})")
        if val == "[":
            return self.collection("[", "]")
        if val == "{":
            if self.block_lambda_ahead():
                return self.block_lambda()
            return self.collection("{", "}")
        raise MuError(f"line {t[2]}: unexpected {val or kind!r}")

    def block_lambda_ahead(self):
        """Whether `{` starts a block lambda: `{->`, `{x ->` or `{(a, b) ->`,
        the head on this line or on the line under the `{`."""
        j = self.i + 1
        if self.toks[j][0] == "NEWLINE" and self.toks[j + 1][0] == "INDENT":
            j += 2
        if self.toks[j][1] == "->":
            return True
        if self.toks[j][0] == "NAME" and self.toks[j + 1][1] == "->":
            return True
        if self.toks[j][1] != "(":
            return False
        j += 1
        while self.toks[j][0] == "NAME" or self.toks[j][1] == ",":
            j += 1
        return self.toks[j][1] == ")" and self.toks[j + 1][1] == "->"

    def block_lambda(self, memo=False):
        """`{p -> body}`: a function with no name of its own. The body is
        an expression on the line, or a block under the `->` closed by a
        `}` at the indent of the line that opened it; its last line is the
        value. Inside, `mu` is the function. It compiles to a def with a
        hidden name, placed before the statement that holds it, so a block
        in a one-line fold is built once and one in a fold's block once per
        item, where its names are bound. With memo, `memo {n ->`, the def is
        cached, as a `memo` statement is, and runs on the deep stack."""
        line = self.expect("{")[2]
        if memo:
            self.memo_used = True
            self.need(helper="cache")
            self.need("import sys")
            self.need(helper="deep")
        deep = self.peek()[0] == "NEWLINE"  # the head on its own line
        if deep:
            self.next()
            self.expect_kind("INDENT")
        names = []
        if self.at("("):
            self.next()
            while not self.at(")"):
                names.append(self.expect_kind("NAME"))
                if not self.at(")"):
                    self.expect(",")
            self.expect(")")
        elif self.peek()[0] == "NAME":
            names.append(self.next()[1])
        self.expect("->")
        self.hoisted += 1
        name = Py(f"_mu{self.hoisted}")
        name.hidden = True
        self.mu.append(name)
        self.known.append(set(names))
        if self.peek()[0] == "NEWLINE":
            self.next()
            self.expect_kind("INDENT")
            body = []
            if self.at("|"):  # a case list, as a memo's
                body = self.cases_body(self.cases())
                if self.peek()[0] != "DEDENT":
                    t = self.peek()
                    raise MuError(
                        f"line {t[2]}: a case list is the whole body of the block"
                    )
            while self.peek()[0] != "DEDENT":
                body += flat(self.stmt())
            self.next()
        else:
            body = [("expr", self.body())]
        if deep:
            self.expect_kind("DEDENT")
        self.known.pop()
        self.mu.pop()
        self.expect("}")
        d = ("def", name, [(Py(n), None) for n in names], None, body)
        d = Hoisted(d + (("memo",) if memo else ()))
        d.reads = reads_of(d)
        self.at_line[id(d)] = line
        self.pending.append(d)
        return Py(name)

    def collection(self, open_, close):
        self.expect(open_)
        if self.at(close):
            self.next()
            return self.rec("") if open_ == "{" else Py(open_ + close)
        if open_ == "{" and self.at(":"):  # {:list} is defaultdict(list)
            self.next()
            default = self.arg()
            self.need("from collections import defaultdict")
            if self.at(close):
                self.next()
                return Py(f"defaultdict({default})")
            return self.collected(default, close)
        mark = len(self.pending)
        first = self.pushed(self.item(open_))
        if self.at("for"):
            gen = self.comprehension(mark)
            self.expect(close)
            out = Py(f"{open_}{first}{gen}{close}")
            return self.rec(out) if first.pair else out
        items = [first]
        while self.at(","):
            self.next()
            if self.at(close):
                break
            items.append(self.item(open_))
        self.expect(close)
        out = Py(open_ + ", ".join(items) + close)
        return self.rec(out) if first.pair else out

    def rec(self, d):
        """A dict mu makes is a Rec, so `d.k` reads the key k: `{}` is
        Rec(), `{'a': 1}` is Rec({'a': 1})."""
        self.need(helper="Rec")
        return Py(f"Rec({d})")

    def item(self, open_):
        if self.at("*") or (open_ == "{" and self.at("**")):
            star = self.next()[1]
            e = Py(f"{star}{self.expr()}")
            e.pair = star == "**"
            return e
        e = self.expr()
        if open_ == "{" and self.at(":"):
            self.next()
            v = self.arg() if self.lambda_ahead() else self.expr()
            e = Py(f"{e}: {v}")
            e.pair = True
        return e

    def collected(self, default, close):
        """{:list: k <- v for ...} applies `<- v` to the entry at k for each
        item, and is the dict. The operator is a push or an augmented
        assignment: {:int: k += 1 for ...}."""
        self.expect(":")
        mark = len(self.pending)
        key = self.expr()
        kind, op, line = self.next()[:3]
        if op != "<-" and (kind != "OP" or op not in IN_PLACE):
            raise MuError(
                f"line {line}: expected <- or an operator like +=, got {op!r}"
            )
        val = self.expr()
        if not self.at("for"):
            raise MuError(f"line {line}: expected 'for' after {key} {op} {val}")
        pairs = f"(({key}, {val}){self.comprehension(mark)})"
        self.expect(close)
        d = f"defaultdict({default})"
        if op == "<-":
            helper = "_added" if default == "set" else "_pushed"
            self.need(helper=helper)
            return Py(f"{helper}({d}, {pairs})")
        self.need(f"from operator import {IN_PLACE[op]}", "_updated")
        return Py(f"_updated({d}, {pairs}, {IN_PLACE[op]})")

    def pushed(self, e):
        """The element of a comprehension may be a push: `[d[v] <- i for
        i, v in s]` appends i to d[v] for each item."""
        if not self.at("<-"):
            return e
        line = self.next()[2]
        e = Py(f"{e}.append({self.expr()})")
        if not self.at("for"):
            raise MuError(f"line {line}: a push in brackets needs a for")
        return e

    def comprehension(self, mark=None):
        """One or more `for target in iter [if cond]` clauses. mark: where
        the pending defs stood before the element was parsed, so a block
        lambda in it that reads a target is refused."""
        out, tgts = "", []
        line = self.peek()[2]
        while self.at("for"):
            self.next()
            tgt, wrap = self.target()
            tgts.append(tgt)
            self.expect("in")
            out += f" for {tgt} in {iterable(self.or_(), wrap)}"
            while self.at("if"):
                self.next()
                out += f" if {self.or_()}"
        if mark is not None:
            self.hoisted_reads(mark, ", ".join(tgts), line)
        return out

    def call(self, fn):
        self.expect("(")
        args, kwargs = [], []
        while not self.at(")"):
            if self.peek()[1] in ("+", "*") and self.peek(1)[1] in (",", ")"):
                args.append(Py(self.next()[1]))
            elif self.at("**"):
                self.next()  # kept among the named arguments, in its place
                kwargs.append((f"**{len(kwargs)}", self.expr()))
            elif self.at("*"):
                self.next()
                args.append(Py(f"*{self.expr()}"))
            elif self.peek()[0] == "NAME" and self.at("=", 1):
                k = self.next()[1]
                self.next()
                kwargs.append((k, self.arg()))
            else:
                args.append(self.arg())
            if not self.at(")"):
                self.expect(",")
        self.expect(")")
        return self.special(fn, args, kwargs)

    def lambda_ahead(self):
        """Whether `x ->`, `(a, b) ->` or a bare `->` starts here."""
        if self.at("->"):
            return True
        if self.peek()[0] == "NAME":
            return self.at("->", 1)
        if not self.at("("):
            return False
        j, depth = self.i, 0
        while True:
            v = self.toks[j][1]
            depth += v == "("
            depth -= v == ")"
            j += 1
            if depth == 0 or self.toks[j][0] in ("NEWLINE", "EOF"):
                break
        return depth == 0 and self.toks[j][1] == "->"

    def body(self):
        """The body of a lambda: an expression, or a push `xs <- e`."""
        e = self.expr()
        if self.at("<-"):
            self.next()
            e = Py(f"{e}.append({self.expr()})")
        return e

    def arg(self):
        """A call argument, the value of `f = x -> e` or a dict value: the
        three places a lambda may appear."""
        if self.at("->"):
            # -> e names no argument: _ is the argument, or the tuple of them
            self.next()
            inner = f"(lambda _: {self.body()})"
            return Py(f"lambda *_a: {inner}(_a[0] if len(_a) == 1 else _a)")
        if self.peek()[0] == "NAME" and self.at("->", 1):
            name = self.next()[1]
            self.next()
            return Py(f"lambda {name}: {self.body()}")
        if self.at("("):
            if self.lambda_ahead():
                self.next()
                names = []
                while not self.at(")"):
                    names.append(self.expect_kind("NAME"))
                    if not self.at(")"):
                        self.expect(",")
                self.expect(")")
                self.expect("->")
                if not names:  # () -> e takes nothing: defaultdict(() -> [0, 0])
                    return Py(f"lambda: {self.body()}")
                names = [f"_{k}" if n == "_" else n for k, n in enumerate(names)]
                # (a, b) -> e takes a and b, or one pair it unpacks
                inner = f"(lambda {', '.join(names)}: {self.body()})"
                return Py(f"lambda *_a: {inner}(*(_a[0] if len(_a) == 1 else _a))")
        mark = len(self.pending)
        e = self.expr()
        if self.at("for"):
            return Py(f"{e}{self.comprehension(mark)}")
        return e

    def special(self, fn, args, kwargs):
        kw = {k: v for k, v in kwargs}
        if fn == "dict":
            self.need(helper="Rec")
            fn = Py("Rec")
        if fn == "ceil" and len(args) == 1 and args[0].div:
            a, b = args[0].div
            return Py(f"-(-({a}) // ({b}))")
        if fn == "floor" and len(args) == 1 and args[0].div:
            a, b = args[0].div
            return Py(f"({a}) // ({b})")
        if fn == "sort":
            if "by" in kw:
                kw["key"] = kw.pop("by")
            fn = "sorted"
        if fn == "filter" and "by" in kw and len(args) == 1:
            # filter(xs, by=f): the items f holds for, as a list
            return Py(f"list(filter({kw['by']}, {args[0]}))")
        if fn == "scan":
            self.need("from itertools import accumulate")
            op, xs = args
            if op == "*":
                self.need("import operator")
                op = "operator.mul"
            # type=list or type=tuple builds the result; with no type it is an iterator
            kind = kw.pop("type", "None")
            if kw.pop("reverse", None) == "True":
                # scan from the right: fold the reversed list, then flip it back
                call = (
                    f"accumulate({xs}[::-1])"
                    if op == "+"
                    else f"accumulate({xs}[::-1], {op})"
                )
                if kind == "None":
                    return Py(f"reversed(list({call}))")
                return Py(f"{kind}({call})[::-1]")
            call = f"accumulate({xs})" if op == "+" else f"accumulate({xs}, {op})"
            return Py(call if kind == "None" else f"{kind}({call})")
        if fn == "counter":
            self.need("from collections import Counter")
            fn = "Counter"
        if fn in ("ceil", "floor"):
            self.need(f"from math import {fn}")
        if fn in self.native:
            self.need(helper=self.native[fn])
        elif fn in HELPERS and fn not in self.imported:
            self.need(helper=fn)
        parts = list(args) + [
            f"**{v}" if k.startswith("**") else f"{k}={v}" for k, v in kw.items()
        ]
        return Py(f"{fn}({', '.join(parts)})")

    def fold(self, allow_block=False):
        """sum|max|min|count [from e] for [target in] iter [if cond] (: body | block).
        With no `target in`, the item is `_`."""
        line = self.peek()[2]
        op = self.next()[1]
        start = None
        if self.at("from"):
            self.next()
            start = self.arith()
        self.expect("for")
        if self.names_a_target():
            tgt, wrap = self.target()
            self.expect("in")
            it = iterable(self.or_(), wrap)
        else:
            tgt, it = "_", self.or_()
        where, mark = None, len(self.pending)
        if self.at("if"):
            self.next()
            where = self.or_()
            if self.at("?"):
                raise MuError(
                    f"line {self.peek()[2]}: no `? :` in a fold's if; bracket it"
                )
        block = (
            allow_block and self.peek()[0] == "NEWLINE" and self.peek(1)[0] == "INDENT"
        )
        self.folds.append((op, start is not None, where is not None, block))
        if block:
            return (op, start, tgt, it, where)
        body = None
        if self.at(":"):
            self.next()
            body = self.expr()
        self.hoisted_reads(mark, tgt, line)
        cond = f" if {where}" if where else ""
        # a list, not a generator: sum, max and min are C functions, and a
        # generator they pull from puts every level of a memo's recursion
        # through C, which Python 3.12 and 3.13 cut short whatever
        # sys.setrecursionlimit says. A comprehension runs inline.
        if op == "count":
            test = " and ".join(f"({c})" for c in (where, body) if c)
            gen = f"[1 for {tgt} in {it}" + (f" if {test}" if test else "") + "]"
            return Py(f"({start} + sum({gen}))" if start else f"sum({gen})")
        if body is None:
            raise MuError(f"line {self.peek()[2]}: {op} for needs ': value' or a block")
        gen = f"[{body} for {tgt} in {it}{cond}]"
        if op == "sum":
            return Py(f"({start} + sum({gen}))" if start else f"sum({gen})")
        if start is None:
            return Py(f"{op}({gen})")
        return Py(f"{op}({start}, {op}({gen}, default={start}))")

    def first_block_ahead(self):
        """Whether `first k in lo..hi` with no `if` ends the line and a block
        follows: the block form, its last line the test."""
        if not (self.at("first") and self.peek(1)[0] == "NAME" and self.at("in", 2)):
            return False
        j, depth = self.i + 3, 0
        while self.toks[j][0] not in ("NEWLINE", "EOF"):
            kind, val = self.toks[j][:2]
            depth += val in "([{"
            depth -= val in ")]}"
            if depth == 0 and (kind, val) == ("NAME", "if"):
                return False
            j += 1
        return self.toks[j + 1][0] == "INDENT"

    def first(self, block=False):
        """first k in lo..hi if pred: the least k with pred true (pred
        monotone). With block, the test may be a block under the line, its
        last line the value, compiled to a def with a hidden name."""
        line = self.expect("first")[2]
        k = self.expect_kind("NAME")
        self.expect("in")
        rng = self.range_()
        if not rng.rng:
            raise MuError(f"line {self.peek()[2]}: first needs a range lo..hi")
        self.need("from bisect import bisect_left")
        lo = rng.rng[0]
        if block and self.peek()[0] == "NEWLINE" and self.peek(1)[0] == "INDENT":
            self.next()
            self.next()
            self.hoisted += 1
            name = Py(f"_first{self.hoisted}")
            name.hidden = True
            self.known.append({k})
            body = []
            while self.peek()[0] != "DEDENT":
                body += flat(self.stmt())
            self.next()
            self.known.pop()
            if not body or body[-1][0] != "expr":
                raise MuError(f"line {line}: the last line of a first block must be the test")
            d = Hoisted(("def", name, [(Py(k), None)], None, body))
            self.at_line[id(d)] = line
            self.pending.append(d)
            return Py(f"{lo} + bisect_left({rng}, True, key={name})")
        self.expect("if")
        pred = self.expr()
        return Py(f"{lo} + bisect_left({rng}, True, key=lambda {k}: {pred})")


NAME = re.compile(r"[A-Za-z_]\w*")


# names a mu file reads without binding them
def filled(lhs, rhs, line):
    """The right side of `targets = rhs`, one value per target, when rhs
    repeats (`[].., 0` is [], [], 0) or is one scalar literal for several
    targets (`a, b, c = 0` is 0, 0, 0). Each copy is its own value: the
    lists of `[]..` are not the same list."""
    names = split_top(lhs) if not lhs.endswith(",") else [lhs]
    if rhs.repeats:
        if any(n.strip().startswith("*") for n in names):
            raise MuError(f"line {line}: a repeat `x..` cannot fill a starred target")
        values = [v for v, c in rhs.repeats for _ in range(c)]
        if len(values) != len(names):
            raise MuError(f"line {line}: {len(names)} names, {len(values)} values")
        return Py(", ".join(values))
    if len(names) > 1 and scalar(rhs):
        return Py(", ".join([rhs] * len(names)))
    return rhs


def scalar(text):
    """Whether text is a literal that Python cannot unpack: a number, inf,
    True, False or None, with a sign or not."""
    try:
        node = ast.parse(text.strip(), mode="eval").body
    except SyntaxError:
        return False
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        node = node.operand
    if isinstance(node, ast.Name):
        return node.id == "inf"
    return isinstance(node, ast.Constant) and not isinstance(node.value, (str, bytes))


def split_top(text):
    """Split at commas outside brackets."""
    parts, depth, cur = [], 0, ""
    for ch in text:
        depth += (ch in "([{") - (ch in ")]}")
        if ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    return parts + [cur.strip()]


CONVERT = re.compile(r"([A-Za-z_][\w.]*)\(([A-Za-z_]\w*)\)")


def converted(lhs):
    """`int(a), b = ...` assigns a and b, then passes a through int. The
    left side comes back as `a, b`, with the second line in `convert`."""
    parts = split_top(lhs)
    calls = [(p, CONVERT.fullmatch(p)) for p in parts]
    if " = " in lhs or not any(m for _, m in calls):
        return lhs
    out = Py(", ".join(m.group(2) if m else p for p, m in calls))
    names = ", ".join(m.group(2) for _, m in calls if m)
    out.convert = f"{names} = {', '.join(p for p, m in calls if m)}"
    return out


TARGETS = (ast.Name, ast.Tuple, ast.List, ast.Subscript, ast.Attribute, ast.Starred)


def computed(side, seen=None):
    """Whether one side of `=` is a value: an operation, a call, a
    comparison, anything Python could not assign to. With seen, the names
    bound so far, `x[::-1]` is a value too when x is not among them."""
    try:
        node = ast.parse(side.strip(), mode="eval").body
    except SyntaxError:
        return False
    if seen is not None and isinstance(node, (ast.Subscript, ast.Attribute)):
        while isinstance(node, (ast.Subscript, ast.Attribute)):
            node = node.value
        return isinstance(node, ast.Name) and node.id not in seen
    return not isinstance(node, TARGETS)


def free_names(side):
    """The names an expression reads, in the order they first appear. A name
    that is called is left out, and so is one the expression binds itself:
    the argument of a lambda, the variable of a comprehension."""
    found = []

    def visit(node, own):
        if isinstance(node, ast.Name):
            if node.id not in own:
                found.append((node.lineno, node.col_offset, node.id))
        elif isinstance(node, ast.Lambda):
            a = node.args
            args = a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg]
            visit(node.body, own | {x.arg for x in args if x})
        elif isinstance(
            node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)
        ):
            for gen in node.generators:
                visit(gen.iter, own)
                own = own | {
                    n.id for n in ast.walk(gen.target) if isinstance(n, ast.Name)
                }
                for cond in gen.ifs:
                    visit(cond, own)
            for part in ("elt", "key", "value"):
                if hasattr(node, part):
                    visit(getattr(node, part), own)
        else:
            for child in ast.iter_child_nodes(node):
                if not (
                    isinstance(node, ast.Call)
                    and child is node.func
                    and isinstance(child, ast.Name)
                ):
                    visit(child, own)

    visit(ast.parse(side.strip(), mode="eval").body, frozenset())
    return list(dict.fromkeys(name for _, _, name in sorted(found)))


def reads_of(stmt):
    """The names a def reads from outside itself, its nested defs included:
    what Python's symbol table marks free or global in it. Empty when the
    def does not compile, which the transpile then reports on its own."""
    e = Emitter()
    e.stmt(stmt, 0, False)
    try:
        table = symtable.symtable("\n".join(e.lines), "<mu>", "exec").get_children()[0]
    except SyntaxError:
        return frozenset()
    out = set()

    def walk(t, bound):
        own = {
            s.get_name()
            for s in t.get_symbols()
            if s.is_assigned() or s.is_parameter() or s.is_imported()
        }
        for s in t.get_symbols():
            if s.is_referenced() and s.get_name() not in own | bound:
                out.add(s.get_name())
        for c in t.get_children():
            walk(c, bound | own)

    walk(table, frozenset())
    return frozenset(out)


def imported(line):
    """The names an import line binds."""
    names = line.split(" import ")[-1] if line.startswith("from ") else line[7:]
    return {n.split(" as ")[-1].strip().split(".")[0] for n in names.split(",")}


def flat(node):
    """The statements one parsed line stands for: `a * b = pair` is two."""
    return node[1] if node[0] == "stages" else [node]


def iterable(it, wrap):
    """The Python iterable of a loop whose target was parsed by `target`."""
    if wrap == "range":
        return Py(f"range(len({it}))")
    if wrap == "enumerate":
        return Py(f"enumerate({it})")
    if wrap == "range_back":
        return Py(f"range(len({it}) - 1, -1, -1)")
    if wrap == "enumerate_back":
        return Py(f"zip(range(len({it}) - 1, -1, -1), reversed({it}))")
    return it


def targets(lhs):
    """The plain names an assignment's left side binds: `a, b = c = ...`."""
    return {
        t for side in lhs.split(" = ") for t in split_top(side) if NAME.fullmatch(t)
    }


def assigned(stmts):
    """Plain names a block assigns with = or op=, not inside nested defs."""
    out = set()
    for s in stmts:
        if s[0] in ("assign", "annassign", "assign_fold"):
            out |= targets(s[1])
        for body in children(s):
            out |= assigned(body)
    return out


def bound(stmts, params=False):
    """Every name a block binds: assignments, loop targets, defs. With
    params, the parameters of every def too."""
    out = set()
    for s in stmts:
        if s[0] in ("assign", "annassign"):
            out |= targets(s[1])
        elif s[0] == "assign_fold":
            out |= targets(s[1]) | set(NAME.findall(s[3][2]))
        elif s[0] == "with":
            out |= {n for m in re.findall(r" as ([^,]+)", s[1]) for n in NAME.findall(m)}
        elif s[0] in ("def", "memo", "class"):
            out.add(s[1])
            if params and s[0] != "class":
                out |= {p if s[0] == "memo" else p[0] for p in s[2]}
        elif s[0] == "for":
            out |= set(NAME.findall(s[1]))
        elif s[0] == "foldblock":
            out |= set(NAME.findall(s[3]))
        for body in children(s):
            out |= bound(body, params)
    return out


def is_memo(s):
    """A memo statement in either form: cases, or a cached def."""
    return s[0] == "memo" or (s[0] == "def" and len(s) > 5)


def ret_name(stmts):
    """The name a `ret` line of this block declares, or None."""
    for s in stmts:
        if s[0] == "assign" and len(s) == 5 or s[0] == "assign_fold" and len(s) == 6:
            return s[-1]
    return None


def yields(stmts):
    """Whether a block has a yield line of its own, not in a nested def."""
    return any(s[0] == "yield" or any(yields(b) for b in children(s)) for s in stmts)


def children(s):
    """The blocks inside a statement, except a nested def's own body."""
    if s[0] == "for":
        return [s[3]] + ([s[4]] if s[4] else [])
    if s[0] == "while":
        return [s[2]] + ([s[3]] if s[3] else [])
    if s[0] == "if":
        return [b for _, b in s[1]] + ([s[2]] if s[2] else [])
    if s[0] == "foldblock":
        return [s[6]]
    if s[0] == "assign_fold":
        return [s[4]]
    if s[0] == "with":
        return [s[2]]
    if s[0] == "try":
        _, body, handlers, other, final = s
        return [body] + [b for _, b in handlers] + [b for b in (other, final) if b]
    return []


DOC = re.compile(r'^[rRuU]?("""|\'\'\')')  # a triple-quoted string


def docstring(text, ind):
    """A multi-line string that stands as a statement, a docstring, with its
    lines after the first moved from mu's indent to the Python one."""
    first, *rest = text.split("\n")
    margin = min(
        (len(ln) - len(ln.lstrip(" ")) for ln in rest if ln.strip()), default=0
    )
    pad = "    " * ind
    return "\n".join([first] + [pad + ln[margin:] if ln.strip() else "" for ln in rest])


class Emitter:
    """Writes Python. A block's last value goes through the format string
    `last`: "return {}" in a def, something else at the REPL's top level.
    A nested def that assigns a name an enclosing def binds gets `nonlocal`.
    Inside a class body (`method`), a def directly under it is a method."""

    def __init__(self, at_line=None):
        self.lines = []
        self.origin = []  # the mu line each output line comes from, or None
        self.at_line = at_line or {}
        self.src_line = None
        self.fresh = 0
        self.method = False  # inside a class body: a def is a method
        self.scopes = []  # names bound by each enclosing def
        self.rets = []  # the ret name of each enclosing def, or None

    def out(self, ind, s):
        self.lines.append("    " * ind + s)
        self.origin.append(self.src_line)

    def block(self, stmts, ind, returns=False):
        for k, s in enumerate(stmts):
            self.stmt(s, ind, returns if k == len(stmts) - 1 else False)

    def stmt(self, s, ind, last):
        self.src_line = self.at_line.get(id(s), self.src_line)
        kind = s[0]
        if kind == "def":
            self.def_(s, ind)
        elif kind == "class":
            self.class_(s, ind)
        elif kind == "memo":
            _, name, params, cases = s
            self.decorators(s, ind)
            self.out(ind, "@cache")
            self.out(ind, f"def {name}({', '.join(params)}):")
            for guard, val in cases:
                if guard is None:
                    self.out(ind + 1, f"return {val}")
                else:
                    self.out(ind + 1, f"if {guard}:")
                    self.out(ind + 2, f"return {val}")
        elif kind == "for":
            _, tgt, it, body, other = s
            self.out(ind, f"for {tgt} in {it}:")
            self.block(body, ind + 1)
            self.loop_else(other, ind)
        elif kind == "while":
            self.out(ind, f"while {s[1]}:")
            self.block(s[2], ind + 1)
            self.loop_else(s[3], ind)
        elif kind == "if":
            _, arms, other = s
            for k, (cond, body) in enumerate(arms):
                self.src_line = self.at_line.get(id(body), self.src_line)
                self.out(ind, f"{'if' if k == 0 else 'elif'} {cond}:")
                self.block(body, ind + 1, returns=last)
            self.loop_else(other, ind, returns=last)
        elif kind == "with":
            self.out(ind, f"with {s[1]}:")
            self.block(s[2], ind + 1, returns=last)
        elif kind == "try":
            self.try_(s, ind, last)
        elif kind == "raise":
            self.out(ind, "raise" if s[1] is None else f"raise {s[1]}")
        elif kind in ("global", "nonlocal"):
            self.out(ind, f"{kind} {s[1]}")
        elif kind == "annassign":
            _, lhs, t, val = s
            self.out(ind, f"{lhs}: {t}" + ("" if val is None else f" = {val}"))
        elif kind == "assign":
            self.out(ind, f"{s[1]} {s[2]} {s[3]}")
            if getattr(s[1], "convert", None):
                self.out(ind, s[1].convert)
        elif kind == "return":
            val = s[1] if s[1] is not None else (self.rets or [None])[-1]
            self.out(ind, "return" if val is None else f"return {val}")
        elif kind in ("break", "continue", "pass"):
            self.out(ind, kind)
        elif kind in ("import", "push"):
            self.out(ind, "pass" if s[1] is None else s[1])  # None: a mu import
        elif kind == "yield":
            self.out(ind, "yield" if s[1] is None else f"yield {s[1]}")
        elif kind == "del":
            self.out(ind, f"del {s[1]}")
        elif kind == "assert":
            parts = getattr(s[1], "cmpparts", None)
            if parts:
                # both sides are evaluated once, so the message on a fail
                # shows the values the assert saw, not a second run of them
                lhs, op, rhs = parts
                self.out(ind, f"_l, _r = {lhs}, {rhs}")
                tail = "" if op == "==" else f' + "\\nop   : {op}"'
                self.out(ind, f"assert _l {op} _r, {SIDES}{tail}")
            else:
                self.out(ind, f"assert {s[1]}")
        elif kind == "expr":
            if not last and DOC.match(s[1]) and "\n" in s[1]:
                self.out(ind, docstring(s[1], ind))
            else:
                self.out(ind, last.format(s[1]) if last else s[1])
        elif kind == "tail" and last:
            # the value of `int(a) = x`, wanted only as a block's last line
            self.out(ind, last.format(s[1]))
        elif kind == "foldblock":
            self.foldblock(s, ind, last)
        elif kind == "assign_fold":
            _, lhs, op, head, body = s[:5]
            self.foldblock(("foldblock", *head, body), ind, f"{lhs} {op} {{}}")

    def try_(self, s, ind, last):
        """A try returns the last line of its handlers and its else; of its
        body too when there is no else, which would run after it."""
        _, body, handlers, other, final = s
        self.out(ind, "try:")
        self.block(body, ind + 1, returns=False if other else last)
        for head, b in handlers:
            self.src_line = self.at_line.get(id(b), self.src_line)
            self.out(ind, f"except {head}:" if head else "except:")
            self.block(b, ind + 1, returns=last)
        self.loop_else(other, ind, returns=last)
        if final:
            self.src_line = self.at_line.get(id(final), self.src_line)
            self.out(ind, "finally:")
            self.block(final, ind + 1)

    def loop_else(self, other, ind, returns=False):
        if other:
            self.src_line = self.at_line.get(id(other), self.src_line)
            self.out(ind, "else:")
            self.block(other, ind + 1, returns=returns)

    def decorators(self, s, ind):
        """The `@` lines of a def or a memo, each mapped to its own mu line."""
        line = self.src_line
        for at, d in getattr(s, "decorators", ()):
            self.src_line = at
            self.out(ind, f"@{d}")
        self.src_line = line

    def class_(self, s, ind):
        """A class: its defs are methods, given a `self`; a def nested in a
        method is a plain function. Methods are set one blank line apart."""
        _, name, base, body = s
        self.decorators(s, ind)
        self.out(ind, f"class {name}({base}):" if base else f"class {name}:")
        was, self.method = self.method, True
        for k, b in enumerate(body):
            if k and (b[0] in ("def", "memo") or body[k - 1][0] in ("def", "memo")):
                self.out(0, "")
            self.stmt(b, ind + 1, False)
        if not body:
            self.out(ind + 1, "pass")
        self.method = was

    def def_(self, s, ind):
        _, name, params, ret, body = s[:5]
        top = not self.scopes  # a method, or a top-level function
        names = {p for p, _ in params}
        marks = {d for _, d in getattr(s, "decorators", ())}
        first = "cls" if "classmethod" in marks else "self"
        # a staticmethod takes no object, a classmethod takes the class
        hidden = getattr(name, "hidden", False)  # a block lambda's def
        sig = [first] * (self.method and top and not hidden and "staticmethod" not in marks)
        sig += [param(p, t) for p, t in params]
        arrow = f" -> {ret}" if ret else ""
        self.decorators(s, ind)
        if is_memo(s):
            self.out(ind, "@cache")
        word = "async def" if getattr(name, "is_async", False) else "def"
        self.out(ind, f"{word} {name}({', '.join(sig)}){arrow}:")
        if self.scopes:
            shared = assigned(body) & set().union(*self.scopes) - names
            if shared:
                self.out(ind + 1, f"nonlocal {', '.join(sorted(shared))}")
        grids = []
        if top:
            for p, t in params:
                if t and t.startswith("list[list["):
                    grids.append(p)
                    self.out(ind + 1, f"_in_{p}, {p} = {p}, Grid({p})")
                    self.out(ind + 1, f"_w_{p} = {p}")
        if grids:
            # the rows go back into the caller's list, so an in-place
            # sort, reverse or append on the grid reaches the caller
            self.out(ind + 1, "try:")
            ind += 1
        self.scopes.append(names | bound(body))
        res = ret_name(body)
        self.rets.append(res)
        # a def typed `-> none`, and __init__, return nothing on their own
        quiet = res or ret == "None" or name == "__init__" or yields(body)
        returns = False if quiet else "return {}"
        if top and any(is_memo(b) for b in body):
            # memo recursion can run 10^4+ deep: evaluate on a big stack
            self.out(ind + 1, "def run():")
            shared = assigned(body) & names
            if shared:
                self.out(ind + 2, f"nonlocal {', '.join(sorted(shared))}")
            self.block(body, ind + 2, returns=returns)
            if res:
                self.out(ind + 2, f"return {res}")
            if yields(body):
                # run is a generator: exhaust it on the big stack, then
                # yield its items from here
                self.out(ind + 1, "yield from deep(lambda: list(run()))")
            else:
                self.out(ind + 1, "return deep(run)")
        else:
            self.block(body, ind + 1, returns=returns)
            if res:
                self.out(ind + 1, f"return {res}")
        self.scopes.pop()
        self.rets.pop()
        if grids:
            self.out(ind, "finally:")
            for p in grids:
                self.out(ind + 1, f"_in_{p}[:] = _w_{p}")

    def foldblock(self, s, ind, last):
        _, op, start, tgt, it, where, body = s
        if not body or body[-1][0] != "expr":
            raise MuError(f"the last line of a {op} for block must be a value")
        self.fresh += 1
        acc = "acc" if self.fresh == 1 else f"acc{self.fresh}"
        if start is None:
            start = "0" if op in ("sum", "count") else "None"
        self.out(ind, f"{acc} = {start}")
        self.out(ind, f"for {tgt} in {it}:")
        if where:
            self.out(ind + 1, f"if not ({where}):")
            self.out(ind + 2, "continue")
        self.block(body[:-1], ind + 1)
        val = body[-1][1]
        if op == "sum":
            self.out(ind + 1, f"{acc} += {val}")
        elif op == "count":
            self.out(ind + 1, f"{acc} += bool({val})")
        elif start == "None":
            self.out(ind + 1, f"{acc} = {val} if {acc} is None else {op}({acc}, {val})")
        else:
            self.out(ind + 1, f"{acc} = {op}({acc}, {val})")
        if last:
            self.out(ind, last.format(acc))


def param(p, t):
    """One parameter of a def's signature: `xs`, `xs: list[int]`, `by=None`,
    `reverse: bool = False`."""
    head = getattr(p, "star", "") + p
    head = head if t is None else f"{head}: {t}"
    d = getattr(p, "default", None)
    if d is None:
        return head
    return f"{head}={d}" if t is None else f"{head} = {d}"


def bound_anywhere(stmts):
    """Every name the whole file binds, def bodies and parameters included."""
    out = bound(stmts, params=True)
    for s in stmts:
        if s[0] == "def":
            out |= bound_anywhere(s[4])
        elif s[0] == "class":
            out |= bound_anywhere(s[3])
        for body in children(s):
            out |= bound_anywhere(body)
    return out


def spread(e):
    """e.origin with one entry per output line: an emitted string may hold
    a newline of its own (the blank line after the globals)."""
    out = []
    for line, o in zip(e.lines, e.origin):
        out += [o] * (line.count("\n") + 1)
    return out


def transpile(src, mapped=False, path=None, library=True):
    """The Python for a mu file. With mapped, also a list with one entry per
    output line: the mu line it comes from, or None for a pasted helper.
    path is the file's own, where its mu imports are looked for first.
    With library=False, no profile helper is pasted, only the language's
    own: the Python of a library module is the same in every process."""
    global HELPERS
    _HERE.append(str(Path(path).resolve().parent) if path else os.getcwd())
    kept = HELPERS
    if not library:
        HELPERS = dict(RUNTIME)
    try:
        return _transpile(src, mapped)
    finally:
        HELPERS = kept
        _HERE.pop()


def _transpile(src, mapped):
    p, tops = parsed(src, Parser.program)
    if any(t and t.startswith("list[list[") for d in grid_defs(tops) for _, t in d[2]):
        p.need(helper="Grid")
    # a helper passed by name, `sort(ds, by=even)`, is pasted like one called,
    # unless the file binds that name itself
    for name in sorted(p.bare - bound_anywhere(p.globals + tops + p.script)):
        p.need(helper=p.native.get(name, name))
    e = Emitter(at_line=p.at_line)
    for g in p.globals:
        e.stmt(g, 0, False)
    if p.globals and tops:
        e.out(0, "\n")
    for k, d in enumerate(tops):
        if k:
            e.out(0, "\n")
        e.stmt(d, 0, False)
    parts = [p.docstring] if p.docstring else []
    if p.imports:
        parts.append("\n".join(sorted(p.imports)))
    if p.memo_used:
        parts.append("sys.setrecursionlimit(1 << 20)")
    # two mu modules may paste the same library helper: once is enough
    parts += list(dict.fromkeys(helper_of(h).source for h in p.helpers))
    origins = [[None] * (part.count("\n") + 1) for part in parts]
    if e.lines or not p.script:  # a file of only a script has no defs part
        parts.append("\n".join(e.lines))
        origins.append(spread(e) or [None])
    if p.script:
        script = Emitter(at_line=p.at_line)
        for k, st in enumerate(p.script):
            # a def or class among the script's lines is set apart, as above
            kinds = {st[0], p.script[k - 1][0]} if k else set()
            if kinds & {"def", "class"}:
                script.out(0, "\n")
            script.stmt(st, 0, False)
        parts.append("\n".join(script.lines))
        origins.append(spread(script))
    code = "\n\n\n".join(parts) + "\n"
    if not mapped:
        return code
    origin = []
    for k, o in enumerate(origins):
        if k:
            origin += [None, None]  # the two blank lines between parts
        origin += o
    return code, origin


def parsed(src, run, primes=frozenset(), strings=frozenset()):
    """(parser, run(parser)) for src. A string `f'...'` met where f is bound
    is read as a prime instead: the file is tokenized again with that `'`
    split out, and parsed again. If that reading fails, the string stays a
    string. A prime left outside a value side of `=` is an error."""
    while True:
        p = Parser(src, primes, strings)
        try:
            out = run(p)
        except Reprime as again:
            try:
                return parsed(src, run, primes | {again.at}, strings)
            except MuError:
                strings |= {again.at}
                continue
        left = repr((out, getattr(p, "globals", []), getattr(p, "script", [])))
        for name, line in p.primed.items():
            if PRIMED + name in left:
                raise MuError(
                    f"line {line}: `{name}'` marks a name bound again;"
                    " it goes on a value side of `=`"
                )
        return p, out


def grid_defs(stmts):
    """The defs that take grids: top-level defs and the methods of classes."""
    out = []
    for s in stmts:
        if s[0] == "def":
            out.append(s)
        elif s[0] == "class":
            out += [b for b in s[3] if b[0] == "def"]
    return out


def compile_stmts(src, last="_ = {}"):
    """Top-level statements to Python, for the REPL: (imports, helpers, code).
    The value of a final expression goes through `last`."""
    p, stmts = parsed(src, Parser.statements)
    for key in p.native.values():  # the REPL defines what it imports at once
        p.need(helper=key)
    if any(t and t.startswith("list[list[") for d in grid_defs(stmts) for _, t in d[2]):
        p.need(helper="Grid")
    e = Emitter()
    e.block(stmts, 0, returns=last)
    return sorted(p.imports), list(p.helpers), "\n".join(e.lines) + "\n"


# formatter

STATEMENT_WORDS = {
    "def", "memo", "for", "in", "if", "elif", "else", "while", "return", "and",
    "or", "not", "from", "first", "break", "continue", "yield", "class", "extends",
    "with", "as", "try", "except", "finally", "raise", "global", "nonlocal",
    "async", "await",
}  # fmt: skip
TIGHT = {"..", "..<", "."}


def gap(prev, cur, unary, subscript, paren=False):
    """The spacing written between two tokens on one line. Inside round
    brackets `=` names an argument and is tight: f(xs, by=key)."""
    (pk, pv), (ck, cv) = prev, cur
    if paren and "=" in (pv, cv) and "OP" in (pk, ck):
        if (pk, pv) == ("OP", "=") or (ck, cv) == ("OP", "="):
            return ""
    if ck == "PRIME":
        return ""
    if ck == "COMMENT" or ck == "POP":
        return "  " if ck == "COMMENT" else " "
    if "TERN" in (pk, ck):
        return " "
    if subscript and (pk, pv) == ("OP", ",") and cv == ":":
        return " "  # x[:, :3]
    if ck == "OP" and cv in (",", ":", ")", "]", "}", "?"):
        return ""
    if pk == "DECO" or pk == "OP" and pv in "([{":
        return ""
    if pk == "POP":
        return " "  # a pop inside a comprehension: `[h . for _ in xs]`
    if cv in TIGHT or pv in TIGHT or unary:
        return ""
    value = pk in ("NUM", "STR") or pk == "NAME" and pv not in NOT_ARGS | KEYWORDS
    if ck == "OP" and set(cv) == {"."} and (value or pv in ")]}"):
        return ""  # a repeat: `[]..`, `0...`
    if subscript and pk == "OP" and pv == ":":
        return ""  # a slice: a[i:j], a[::-1]
    if ck == "OP" and cv in "([":
        if pk in ("NUM", "STR") or (pk == "NAME" and pv not in STATEMENT_WORDS):
            return ""
        if pk == "OP" and pv in ")]}?":
            return ""
    return " "


def pop_dots(toks):
    """A `.` not followed by a plain name is a pop; mark it so the
    formatter keeps it apart: `stack .`, never `stack.`."""
    out = list(toks)
    for k, t in enumerate(toks):
        nxt = toks[k + 1] if k + 1 < len(toks) else None
        attribute = nxt and nxt[0] == "NAME" and nxt[1] not in STATEMENT_WORDS
        if t == ("OP", ".") and not attribute:
            out[k] = ("POP", ".")
    return out


TYPE_AFTER_Q = {",", ")", "]", "}", "=", "->", ":"}


def mark_ternaries(toks):
    """A `?` that starts an expression, and the `:` that answers it, are
    marked TERN so the formatter spaces them: `c ? a : b`. A `?` that ends a
    type, `int?`, is followed by nothing or by a bracket, a comma, `=`,
    `->` or `:`, and stays an OP."""
    out, open_at, depth = list(toks), [], 0
    for k, (kind, val) in enumerate(toks):
        if kind != "OP":
            continue
        if val in "([{":
            depth += 1
        elif val in ")]}":
            depth -= 1
        elif val == "?":
            nxt = toks[k + 1] if k + 1 < len(toks) else None
            if nxt and nxt[0] != "COMMENT" and nxt[1] not in TYPE_AFTER_Q:
                out[k] = ("TERN", "?")
                open_at.append(depth)
        elif val == ":" and open_at and open_at[-1] == depth:
            open_at.pop()
            out[k] = ("TERN", ":")
    return out


def render(toks, level=0):
    """One logical line. An NL token is a line break the author made inside
    brackets: it is kept, and the next line is indented one step for each
    earlier line that left a bracket open, `f(g(` counting once. A line
    that starts by closing brackets sits with the line that opened them."""
    out, prev, unary, stack = [], None, False, []
    opened, row = [], 0  # the row each open bracket was opened on
    if toks and toks[0] == ("OP", "@"):
        toks = [("DECO", "@")] + list(toks[1:])  # a decorator, not a matmul
    for t in pop_dots(mark_ternaries(toks)):
        if t[0] == "NL":
            out.append("\n")
            prev, unary, row = None, False, row + 1
            continue
        if prev is None and out:  # the first token after a kept line break
            rows = set(opened)
            if t[0] == "OP" and t[1] in ")]}" and opened:
                rows.discard(opened[-1])
            out.append("  " * (level + len(rows)))
        pk, pv = prev or ("OP", "(")
        if prev is not None:
            sub = bool(stack) and stack[-1] is True
            out.append(gap(prev, t, unary, sub, bool(stack) and stack[-1] == "("))
        if t[0] == "OP" and t[1] in "([{":
            # `[` right after a value opens a subscript, where `:` is tight
            after_value = pk in ("NAME", "NUM", "STR") and pv not in STATEMENT_WORDS
            after_value = after_value or pk == "OP" and pv in ")]}"
            sub = t[1] == "[" and prev is not None and after_value
            stack.append(True if sub else t[1])
            opened.append(row)
        elif t[0] == "OP" and t[1] in ")]}" and stack:
            stack.pop()
            opened.pop()
        opens = prev is None or pk == "OP" and pv not in ")]}" or pv in STATEMENT_WORDS
        opens = opens or pk == "TERN"
        unary = t[0] == "OP" and (
            t[1] in ("-", "~")
            and opens
            or t[1] in ("<", ">")
            and pv == "for"  # the loop mark: for >i, x in xs
            or t[1] in ("*", "**")
            and (prev is None or pv in "([{," or (pk, pv) == ("NAME", "return"))
            or t[1] == ":"
            and (pk, pv) == ("OP", "{")  # {:list} keeps the default tight
        )
        out.append(t[1])
        prev = t
    return "".join(out)


BLOCK_WORDS = {"if", "elif", "else", "for", "while", "with", "try", "except", "finally"}


def split_inline(toks):
    """(header, body) for `if cond: stmt` and the like, so the body goes on
    its own line; (toks, []) when the line has no one-line body. The block
    colon is the first one outside brackets that no fold (`sum for x in
    xs: e`) on the line claims."""
    if not toks or toks[0][0] != "NAME" or toks[0][1] not in BLOCK_WORDS:
        return toks, []
    depth, folds = 0, 0
    for k, (kind, val) in enumerate(mark_ternaries(toks)):
        if kind == "OP" and val in "([{":
            depth += 1
        elif kind == "OP" and val in ")]}":
            depth -= 1
        elif depth == 0 and kind == "NAME" and val in FOLDS and k + 1 < len(toks):
            folds += toks[k + 1][1] in ("for", "from")
        elif depth == 0 and (kind, val) == ("OP", ":"):
            if folds:
                folds -= 1
                continue
            body = toks[k + 1 :]
            if not body or body[0][0] == "COMMENT":
                return toks, []
            return toks[:k], body
    return toks, []


def top_arrow(text):
    """Index of the first ` -> ` outside brackets, or -1."""
    depth = 0
    for i, ch in enumerate(text):
        depth += ch in "([{"
        depth -= ch in ")]}"
        if depth == 0 and text.startswith(" -> ", i):
            return i
    return -1


def align_cases(lines):
    """Pad the guards of consecutive `| guard -> value` lines to one width."""
    i = 0
    while i < len(lines):
        j = i
        while j < len(lines) and lines[j] and lines[j][1].startswith("| "):
            j += 1
        run = [k for k in range(i, j) if top_arrow(lines[k][1]) >= 0]
        if run:
            width = max(top_arrow(lines[k][1]) for k in run)
            for k in run:
                level, text = lines[k]
                a = top_arrow(text)
                lines[k] = (level, text[:a].ljust(width) + text[a:])
        i = max(j, i + 1)
    return lines


ONE_LINE_DEF = re.compile(r"def \w+\(.*\)(?: -> [^:]+)?: \S")


def set_apart(prev, level, body, in_class):
    """Whether a blank line goes above this line: a top-level def or class,
    its decorators and its comments, and a method of a class are set apart,
    except right under a block header or under a comment or decorator."""
    if prev is None:
        return False
    if ONE_LINE_DEF.match(body) and ONE_LINE_DEF.match(prev[1]) and prev[0] == level:
        return False  # one-line defs in a row stay together
    top = body.startswith(("def ", "memo ", "class ", "#", "@")) and (
        level == 0 or level == 1 and in_class
    )
    return top and not prev[1].startswith(("#", "@")) and prev[0] >= level


WIDTH = 88  # a bracketed expression joined onto one line must fit in this


def without_trailing_commas(toks):
    """A comma right before a closing bracket goes, unless it makes a
    one-element tuple, `(x,)`: a paren that is not a call's and holds no
    other comma of its own."""
    drop, stack = set(), []  # stack: [open index, commas inside, is a tuple]
    for k, (kind, val) in enumerate(toks):
        if kind != "OP":
            continue
        if val in "([{":
            prev = toks[k - 1] if k else ("OP", "(")
            call = prev[0] in ("NAME", "STR") or prev[1] in (")", "]")
            stack.append([k, 0, val == "(" and not call])
        elif val == "," and stack:
            stack[-1][1] += 1
        elif val in ")]}" and stack:
            _, commas, tuple_ = stack.pop()
            if toks[k - 1] == ("OP", ",") and not (tuple_ and commas == 1):
                drop.add(k - 1)
    return [t for k, t in enumerate(toks) if k not in drop]


def kept_breaks(toks, level):
    """The line breaks an author made inside brackets go when the line,
    joined, fits in WIDTH and holds no comment that would swallow the code
    after it. Otherwise they stay."""
    if not any(t[0] == "NL" for t in toks):
        return toks
    joined = [t for t in toks if t[0] != "NL"]
    joined = without_trailing_commas(joined)
    inside = any(t[0] == "COMMENT" for t in joined[:-1])
    if not inside and 2 * level + len(render(joined)) <= WIDTH:
        return joined
    return toks


def fmt(src):
    """Reprint a mu program: two-space blocks, one space around binary
    operators, no space in ranges, `->` aligned down a memo's cases, one
    blank line between top-level defs and classes and between the methods
    of a class, and every one-line body (`if c: x`) moved onto its own
    indented line. A bracketed expression split over several lines is
    joined into one when it fits in WIDTH and holds no comment; otherwise
    its line breaks stay, each line indented one step per open bracket.
    fmt(fmt(s)) == fmt(s)."""
    lines, indents, buf, depth, blank, level = [], [0], [], 0, False, 0
    in_class = False  # the current top-level block is a class
    stack = []  # each open bracket; "B" for a brace that opens a block
    rows, k = src.splitlines(), -1
    while k + 1 < len(rows):
        k += 1
        n, line = k + 1, rows[k]
        if depth == 0:
            body = line.strip()
            if not body:
                blank = bool(lines)
                continue
            ind = len(line) - len(line.lstrip(" "))
            if body.startswith("#"):
                # a comment opening a block sits one level in
                level = sum(1 for d in indents[1:] if d <= ind) + (ind > indents[-1])
            else:
                if ind > indents[-1]:
                    indents.append(ind)
                while ind < indents[-1]:
                    indents.pop()
                if ind != indents[-1]:
                    raise MuError(f"line {n}: indentation does not match any block")
                level = len(indents) - 1
            if level == 0 and not body.startswith(("#", "@")):
                in_class = body.startswith("class ")
            if lines and (blank or set_apart(lines[-1], level, body, in_class)):
                lines.append(None)
            blank = False
        pos, glue = 0, -1  # glue: where a number just ended
        while pos < len(line):
            long = triple(rows, k, pos)
            if long:
                text, k, pos = long
                line = rows[k]
                buf.append(("STR", text))
                continue
            m = TOKEN.match(line, pos)
            if not m:
                raise MuError(f"line {n}: unexpected character {line[pos]!r}")
            pos = m.end()
            if m.lastgroup == "ws":
                continue
            val = m.group()
            if m.lastgroup == "op" and val in "([{":
                if val == "{" and opens_block(line, pos, rows, k):
                    stack.append("B")  # `{p ->` opens a block: its lines stay
                else:
                    depth += 1
                    stack.append(val)
            elif m.lastgroup == "op" and val in ")]}":
                if not (stack and stack.pop() == "B"):
                    depth -= 1
            if m.lastgroup == "name" and buf and buf[-1][0] == "NUM" and glue == m.start():
                buf[-1] = ("NUM", buf[-1][1] + val)  # `2x` stays one token
                continue
            buf.append((m.lastgroup.upper(), val))
            glue = pos if m.lastgroup == "num" else -1
        if depth and buf and buf[-1][0] != "NL":
            buf.append(("NL", ""))  # a line break inside brackets is kept
        if depth == 0 and buf:
            at = level
            while buf:  # `if c: stmt` becomes a header and an indented body
                head, buf = split_inline(buf)
                lines.append((at, render(kept_breaks(head, at), at)))
                at += 1
    if depth:
        raise MuError("unclosed bracket at end of file")
    lines = align_cases(lines)
    text = ["" if ln is None else "  " * ln[0] + ln[1] for ln in lines]
    return "\n".join(text).strip("\n") + "\n"
