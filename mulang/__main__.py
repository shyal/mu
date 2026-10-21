"""mu FILE|- [ARG...]   run the file, ARGs in sys.argv
mu --py FILE|-       the Python
mu --fmt FILE|-      the file, formatted
mu --version
Also python -m mulang."""

import ast
import linecache
import sys
import traceback
import types

from . import BUILTINS, VERSION, MuError, fmt, profile, transpile

# the filename of the helpers the transpiler pastes in, in a traceback
HELPERS = "<mu helpers>"


def mu_line(origin, k):
    """The mu line behind Python line k (1-based): the nearest one before
    it that has an origin, else the nearest one after it."""
    before = [o for o in origin[:k] if o is not None]
    if before:
        return before[-1]
    after = [o for o in origin[k:] if o is not None]
    return after[0] if after else 1


def compiled(src, filename):
    """A mu file as two code objects, (helpers, program). The program is the
    file's own statements at their mu lines under filename, so a traceback
    shows the mu line; the helpers are the imports and pasted functions."""
    code, origin = transpile(src, mapped=True, path=None if filename == "<stdin>" else filename)
    try:
        tree = ast.parse(code)
    except SyntaxError as err:
        raise MuError(f"line {mu_line(origin, err.lineno or 1)}: {err.msg}") from None
    own = [
        s
        for s in tree.body
        if isinstance(s, ast.ClassDef) or origin[s.lineno - 1] is not None
    ]
    helpers = [s for s in tree.body if s not in own]
    program = ast.Module(own, type_ignores=[])
    for node in ast.walk(program):
        if hasattr(node, "lineno"):
            node.lineno = mu_line(origin, node.lineno)
            if getattr(node, "end_lineno", None) is not None:
                node.end_lineno = max(node.lineno, mu_line(origin, node.end_lineno))
    linecache.cache[HELPERS] = (len(code), None, code.splitlines(True), HELPERS)
    return (
        compile(ast.Module(helpers, type_ignores=[]), HELPERS, "exec"),
        compile(program, filename, "exec"),
    )


def run(src, filename, args=()):
    """Run a mu file as the main module, the way `python FILE` runs one."""
    if filename == "<stdin>":
        linecache.cache[filename] = (len(src), None, src.splitlines(True), filename)
    helpers, program = compiled(src, filename)
    sys.argv = [filename, *args]
    # a fresh __main__ whose file is the mu file, so `inspect` finds a class
    # the file defines in it and not in this module
    main = types.ModuleType("__main__")
    main.__file__ = filename
    main.__dict__.update(BUILTINS)
    sys.modules["__main__"] = main
    exec(helpers, main.__dict__)
    exec(program, main.__dict__)


def main(args):
    if args == ["--version"]:
        sys.exit(print(f"mu {VERSION}"))
    if not args or args[0] in ("-h", "--help"):
        sys.exit(print(__doc__))
    mode = args[0] if args[0] in ("--py", "--fmt") else None
    if mode:
        args = args[1:]
        if len(args) != 1:
            sys.exit(__doc__)
    name, rest = args[0], args[1:]
    try:
        src = sys.stdin.read() if name == "-" else open(name).read()
        if mode == "--fmt":
            return sys.stdout.write(fmt(src))
        profile()
        if mode == "--py":
            return sys.stdout.write(transpile(src, path=None if name == "-" else name))
        run(src, "<stdin>" if name == "-" else name, rest)
    except MuError as err:
        sys.exit(f"{name}: {err}")
    except Exception as err:
        # the traceback starts at the mu file, as python's starts at the script
        tb = err.__traceback__
        while tb and tb.tb_frame.f_code.co_filename == __file__:
            tb = tb.tb_next
        traceback.print_exception(type(err), err, tb)
        sys.exit(1)


def cli():
    main(sys.argv[1:])


if __name__ == "__main__":
    cli()
