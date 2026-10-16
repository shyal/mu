"""MkDocs hook: README.md gives each solve as a code block, then other
versions of the same solve, each folded in <details><summary>Label</summary>,
so GitHub shows them on a click. On the docs site the versions become tabs
of the solve. The first block, the unfolded one, is the first tab: mu +
pydsa on both sites."""

import re
from functools import partial

from markdown import Extension
from markdown.preprocessors import Preprocessor

FENCE = r"```\w*\n(?:(?!```).*\n)*```"
FOLD = rf"\n\n<details><summary>(.+)</summary>\n\n({FENCE})\n\n</details>"
SOLVE = re.compile(rf"^({FENCE})((?:{FOLD})+)$", re.M)


def indent(block):
    return "\n".join("    " + line if line else line for line in block.split("\n"))


def tabs(first, m):
    parts = [f'===! "{first}"\n\n{indent(m.group(1))}']
    parts += [
        f'=== "{name}"\n\n{indent(block)}'
        for name, block in re.findall(FOLD, m.group(2))
    ]
    return "\n\n".join(parts)


class Toggles(Preprocessor):
    def __init__(self, md, first):
        super().__init__(md)
        self.first = first

    def run(self, lines):
        return SOLVE.sub(partial(tabs, self.first), "\n".join(lines)).split("\n")


class TogglesExtension(Extension):
    def __init__(self, first):
        super().__init__()
        self.first = first

    def extendMarkdown(self, md):
        # after snippets (32) pastes README.md in, and before superfences
        # and the raw HTML stash read the text
        md.preprocessors.register(Toggles(md, self.first), "toggles", 31.5)


def on_config(config):
    config.markdown_extensions.append(TogglesExtension("mu + pydsa"))
    return config
