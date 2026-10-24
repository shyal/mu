"""A Pygments lexer for mu, for the docs site: Python's, plus mu's tokens.

A `'` directly after a letter, a digit or `_` is a prime (`m'`), not the
start of a string. `..` and `..<` are ranges, and dots after a value are a
repeat (`[]..`, `0...`), so `0..` is not the float `0.`. A number directly
before a name, `2x`, is a number and a name. Registered as `mu` through the pygments.lexers entry
point, so a ```mu block in the docs uses it."""

from pygments.lexer import inherit
from pygments.lexers.python import PythonLexer
from pygments.token import Number, Operator


class MuLexer(PythonLexer):
    name = "mu"
    aliases = ["mu"]
    filenames = ["*.mu"]

    tokens = {
        "expr": [
            (r"(?<=\w)'", Operator),
            (r"\.\.<|\.{2,}", Operator),  # a range, a repeat, the Ellipsis
            inherit,
        ],
        # `0..n`: the number ends before the dots
        "numbers": [(r"\d(?:_?\d)*(?=\.\.)", Number.Integer), inherit],
    }
