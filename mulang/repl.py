"""mu REPL.

    python -m mulang.repl

A bare expression prints its value. A line that opens a block (def, for,
if, memo f(a) =, a fold header) reads on until a blank line.

    :py        show the Python each input becomes (toggle)
    :load F    run a .mu file; its defs become plain functions
    :reset     forget every name
    :q         quit (or ctrl-D)
"""

import atexit
import ctypes
import os
import re
import sys
import threading
from collections import Counter
from pathlib import Path

from . import TOKEN, MuError, compile_stmts, helper_of, profile, tokenize

OUT = "__mu_out__"
# same rule as the VS Code extension: these lines open a block
OPENS_BLOCK = re.compile(
    r"^\s*(def\b.*|else\s*|memo\b(?:[^=]*|.*=)\s*|(for|while|if|elif)\b[^:]*"
    r"|(sum|max|min|count)\s+(from\s+.+\s+)?for\b[^:]*)$"
)


# highlighting: the same classes as vscode/syntaxes/mu.tmLanguage.json
CONTROL = {"def", "memo", "for", "in", "if", "elif", "else", "while", "return"}
CONTROL |= {"break", "continue", "and", "or", "not", "is", "pass", "del"}
CONTROL |= {"import", "extends", "assert", "yield"}
FOLD_WORDS = {"from", "first"}
CONSTANTS = {"true", "false", "none", "inf"}
TYPES = {"int", "str", "char", "bool", "float"}
BUILTINS = {"len", "max", "min", "sum", "abs", "sort", "scan", "counter", "ceil"}
BUILTINS |= {"floor", "cells", "nbrs", "components", "graph", "dijkstra", "print"}
STYLE = {
    "mu.control": "#c678dd bold",
    "mu.fold": "#ff79c6 bold",
    "mu.const": "#56b6c2 bold",
    "mu.item": "#e06c75 italic",
    "mu.num": "#d19a66",
    "mu.str": "#98c379",
    "mu.comment": "#7f848e italic",
    "mu.type": "#e5c07b",
    "mu.defname": "#61afef bold",
    "mu.builtin": "#e5c07b",
    "mu.call": "#61afef",
    "mu.range": "#ff5555 bold",
    "mu.op": "#abb2bf",
    "prompt": "#61afef bold",
    "error": "#ff5555",
}


def highlight(line):
    """[(style class, text)] for one line of mu; the texts join to the line."""
    toks, pos = [], 0
    while pos < len(line):
        m = TOKEN.match(line, pos)
        if not m:  # e.g. an unclosed quote while typing
            rest = line[pos:]
            toks.append(("str" if rest[0] in "'\"" else "", rest))
            break
        toks.append((m.lastgroup, m.group()))
        pos = m.end()
    words = [k for k, (g, _) in enumerate(toks) if g != "ws"]
    out = []
    for n, k in enumerate(words):
        g, v = toks[k]
        nxt = toks[words[n + 1]][1] if n + 1 < len(words) else ""
        prev = toks[words[n - 1]][1] if n else ""
        style = {"num": "num", "str": "str", "comment": "comment"}.get(g, "")
        if g == "name":
            if v in ("sum", "max", "min", "count") and nxt in ("for", "from"):
                style = "fold"
            elif v in FOLD_WORDS:
                style = "fold"
            elif v in CONTROL:
                style = "control"
            elif v in CONSTANTS:
                style = "const"
            elif v == "_":
                style = "item"
            elif v in TYPES:
                style = "type"
            elif prev in ("def", "memo"):
                style = "defname"
            elif (
                nxt == "(" or v in BUILTINS and (nxt[:1].isalnum() or nxt[:1] in "'\"_")
            ):
                style = "builtin" if v in BUILTINS else "call"
        elif g == "op":
            pop = v == "." and not (
                nxt.isidentifier() and nxt not in CONTROL | FOLD_WORDS
            )
            if v in ("..", "..<", "->", "<-") or v == "|" and n == 0 or pop:
                style = "range"
            elif v not in "()[]{},:.":
                style = "op"
        toks[k] = (style, v)
    for g, v in toks:
        out.append(("" if g == "ws" else f"class:mu.{g}" if g else "", v))
    return out


def open_brackets(src):
    depth = 0
    for m in TOKEN.finditer(src):
        if m.lastgroup == "op":
            depth += (m.group() in "([{") - (m.group() in ")]}")
    return depth


def needs_more(lines, block):
    """(read another line?, in block mode?) for the lines typed so far."""
    src = "\n".join(lines)
    try:
        tokenize(src)
    except MuError as err:
        if err.eof:  # an open triple-quoted string owns its blank lines
            return True, block
    if open_brackets(src) > 0:
        return True, block
    if block:
        return lines[-1].strip() != "", True
    try:
        compile_stmts(src)
    except MuError as err:
        if err.eof:
            return True, True
    return False, False


def show(v):
    """A value as mu writes it."""
    if isinstance(v, bool) or v is None:
        return {True: "true", False: "false", None: "none"}[v]
    if isinstance(v, range) and v.step == 1:
        return f"{v.start}..{v.stop - 1}" if len(v) else f"{v.start}..<{v.stop}"
    if isinstance(v, Counter):
        return repr(dict(v))
    if hasattr(v, "__next__"):
        return repr(list(v))
    return repr(v)


def on_big_stack(fn):
    """Run fn on a 256 MB stack; ctrl-C interrupts it."""
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as e:
            box["error"] = e

    threading.stack_size(1 << 28)
    t = threading.Thread(target=target, daemon=True)
    t.start()
    while t.is_alive():
        try:
            t.join(0.05)
        except KeyboardInterrupt:
            ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(t.ident), ctypes.py_object(KeyboardInterrupt)
            )
    if "error" in box:
        raise box["error"]
    return box.get("value")


def terminal_input():
    """A prompt function (prompt, indent) -> line with live highlighting, or
    None when prompt_toolkit is missing or stdin is not a terminal."""
    if not sys.stdin.isatty():
        return None
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.document import Document
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.lexers import Lexer
        from prompt_toolkit.styles import Style
    except ImportError:
        return None

    class MuLexer(Lexer):
        def lex_document(self, document: Document):
            lines = document.lines
            return lambda i: highlight(lines[i]) if i < len(lines) else []

    keys = KeyBindings()

    @keys.add("tab")
    def _(event):
        event.current_buffer.insert_text("  ")

    session = PromptSession(
        history=FileHistory(os.path.expanduser("~/.mu_history")),
        lexer=MuLexer(),
        style=Style.from_dict(STYLE),
        key_bindings=keys,
    )

    def ask(prompt, indent):
        return session.prompt([("class:prompt", prompt)], default=" " * indent)

    return ask


def plain_input():
    """input() with readline: history, and the indent pre-filled."""
    history = os.path.expanduser("~/.mu_readline_history")
    try:
        import readline

        readline.parse_and_bind("tab: insert-tab")
        try:
            readline.read_history_file(history)
        except OSError:  # no history yet, or one libedit cannot parse
            pass
        atexit.register(readline.write_history_file, history)
    except ImportError:
        pass

    def ask(prompt, indent):
        set_indent(indent)
        return input(prompt)

    return ask


def print_python(code):
    """The :py output, coloured as Python when the terminal allows it."""
    if sys.stdout.isatty():
        try:
            from prompt_toolkit import print_formatted_text
            from prompt_toolkit.formatted_text import PygmentsTokens
            from pygments import lex
            from pygments.lexers import PythonLexer

            tokens = list(lex(code, lexer=PythonLexer()))
            print_formatted_text(PygmentsTokens(tokens), end="")
            return
        except ImportError:
            pass
    print(code)


class Repl:
    def __init__(self, ask=None):
        self.reset()
        self.show_py = False
        self.ask = ask or (lambda prompt, indent: input(prompt))

    def reset(self):
        self.ns = {"__name__": "__mu__"}
        self.loaded = set()
        exec("import sys\nsys.setrecursionlimit(1 << 20)", self.ns)

    def run(self, src):
        """Run one input; return the lines to print."""
        out = []
        try:
            imports, helpers, code = compile_stmts(src, last=OUT + " = {}")
        except MuError as err:
            return [f"syntax: {err}"]
        if self.show_py:
            out.append(Python(code.rstrip()))
        for imp in imports:
            exec(imp, self.ns)
        for h in helpers:
            if h not in self.loaded:
                exec(helper_of(h).source, self.ns)
                self.loaded.add(h)
        self.ns.pop(OUT, None)
        try:
            on_big_stack(lambda: exec(code, self.ns))
        except KeyboardInterrupt:
            return out + ["interrupted"]
        except Exception as err:
            return out + [f"{type(err).__name__}: {err}"]
        value = self.ns.pop(OUT, None)
        if value is not None:  # as in Python, a None result prints nothing
            out.append(show(value))
        return out

    def command(self, line):
        cmd, _, arg = line.strip().partition(" ")
        if cmd == ":py":
            self.show_py = not self.show_py
            return [f"show python: {show(self.show_py)}"]
        if cmd == ":reset":
            self.reset()
            return ["every name forgotten"]
        if cmd == ":load":
            try:
                src = Path(arg.strip()).expanduser().read_text()
            except OSError as err:
                return [str(err)]
            return self.run(src)
        return [__doc__.split("\n\n", 2)[2].rstrip()]

    def read(self):
        """One input, or None at end of input."""
        lines, block = [], False
        prompt, indent = "mu> ", 0
        while True:
            try:
                lines.append(self.ask(prompt, indent))
            except EOFError:
                return "\n".join(lines) if lines else None
            if lines[0].startswith(":"):
                return lines[0]
            more, block = needs_more(lines, block)
            if not more:
                return "\n".join(lines)
            prompt = ".. "
            last = lines[-1]
            indent = len(last) - len(last.lstrip(" "))
            if OPENS_BLOCK.match(last):
                indent += 2


def set_indent(n):
    """Pre-fill the next prompt with n spaces, where readline allows it."""
    try:
        import readline

        def hook():
            readline.insert_text(" " * n)
            readline.redisplay()
            readline.set_pre_input_hook(None)

        readline.set_pre_input_hook(hook)
    except (ImportError, AttributeError):
        pass


class Python(str):
    """Generated Python in the REPL's output, printed with colour."""


def main():
    profile()
    repl = Repl(terminal_input() or plain_input())
    print("mu REPL. :help for commands, ctrl-D to quit.")
    while True:
        try:
            src = repl.read()
        except KeyboardInterrupt:
            print()
            continue
        if src is None or src.strip() in (":q", ":quit"):
            break
        if not src.strip():
            continue
        lines = repl.command(src) if src.startswith(":") else repl.run(src)
        for line in lines:
            if isinstance(line, Python):
                print_python(line)
            else:
                print(line)
    print()


if __name__ == "__main__":
    main()
