"""A Pygments lexer for mu, for the docs site: Python's, plus the prime.

A `'` directly after a letter, a digit or `_` is a prime (`m'`), not the
start of a string. Registered as `mu` through the pygments.lexers entry
point, so a ```mu block in the docs uses it."""

from pygments.lexer import inherit
from pygments.lexers.python import PythonLexer
from pygments.token import Operator


class MuLexer(PythonLexer):
    name = "mu"
    aliases = ["mu"]
    filenames = ["*.mu"]

    tokens = {"expr": [(r"(?<=\w)'", Operator), inherit]}
