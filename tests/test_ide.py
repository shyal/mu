"""mulang.ide: hover, completion, go-to-definition and signature help answer
on a file that is mid-edit, through the transpiled Python and its line map."""

import json
import os
import subprocess
import sys

import pydsa.grid

from mulang import ide

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# line 7 is half typed, line 8 never closes its bracket
TEXT = """# a drill
def largestOfLastK(nums: [int], k: int) -> [int]
  res = []
  memo f(i, j) =
    | i == 0 -> 0
    | else -> f(i - 1, j)
  for >i, n in nums
    if x ==
    d = nbrs(grid, i,
    res <- f(i, 0)
  res

res = largestOfLastK([1], 1)
"""


def req(cmd, line, col, text=TEXT):
    return {"id": 1, "cmd": cmd, "root": REPO, "text": text, "line": line, "col": col}


def test_an_unclosed_bracket_blanks_its_logical_line():
    assert ide.unclosed(TEXT.split("\n"))[1] == {8}
    assert ide.unclosed("x = f(a,\n      b)\ny = 1".split("\n"))[1] == set()
    assert ide.unclosed("x = f(a,\n b))\ny = 1".split("\n"))[1] == {0, 1}


def test_repair_blanks_only_the_broken_lines():
    code, origin, blanked = ide.repaired(TEXT.split("\n"))
    assert blanked == {7, 8}
    assert "res.append(f(i, 0))" in code
    assert origin[-1] == 13  # `res = largestOfLastK([1], 1)` is mu line 13, 1-based


def test_hover_gives_mu_types():
    assert ide.hover(req("hover", 1, 20))["contents"] == "```mu\nnums: [int]\n```"
    assert ide.hover(req("hover", 1, 32))["contents"] == "```mu\nk: int\n```"
    assert ide.hover(req("hover", 2, 3))["contents"] == "```mu\nres: [int]\n```"
    assert ide.hover(req("hover", 6, 10))["contents"] == "```mu\nn: int\n```"


def test_hover_on_a_def_of_the_file_shows_its_line():
    assert ide.hover(req("hover", 9, 12))["contents"] == "```mu\nmemo f(i, j) =\n```"


def test_hover_on_a_helper_shows_its_def_and_docstring():
    got = ide.hover(req("hover", 8, 10))["contents"]
    assert got.startswith("```mu\nnbrs(grid, r, c=None, dirs=CARDINALS")
    assert "# The cells (r + dr, c + dc)" in got


def test_hover_on_nothing():
    assert ide.hover(req("hover", 0, 0)) is None  # a comment
    assert ide.hover(req("hover", 7, 4)) is None  # the half-typed `if`


def test_definition_of_a_name_is_its_mu_line():
    assert ide.definition(req("definition", 9, 4)) == [
        {"path": None, "line": 2, "col": 2}
    ]
    assert ide.definition(req("definition", 9, 12)) == [
        {"path": None, "line": 3, "col": 7}
    ]


def test_definition_of_a_helper_is_its_source():
    [d] = ide.definition(req("definition", 8, 10))
    # a module written in mu (pydsa sets __mu__) opens at its .mu source
    assert d["path"] == getattr(pydsa.grid, "__mu__", pydsa.grid.__file__)
    assert "def nbrs(" in open(d["path"]).read().split("\n")[d["line"]]


def test_completion_lists_scope_keywords_and_helpers():
    labels = {c["label"] for c in ide.complete(req("complete", 7, 4))}
    assert {"nums", "k", "res", "f", "n", "while", "memo", "nbrs", "table"} <= labels
    assert "self" not in labels


def test_preloaded_names_are_the_library_and_the_environment():
    names = ide.preloaded()
    assert {"nbrs", "heap", "build_tree", "ListNode"} <= set(names)
    assert not {"deque", "Counter", "cache", "inf"} & set(names)


def test_completion_lists_every_preloaded_name():
    labels = {c["label"] for c in ide.complete(req("complete", 7, 4))}
    missing = set(ide.preloaded()) - labels
    assert not missing, sorted(missing)


def test_completion_of_an_environment_name_shows_its_def():
    text = TEXT.replace("    if x ==\n", "    linked_list\n")
    got = ide.complete(req("complete", 7, 15, text))
    assert [c["label"] for c in got] == ["linked_list_reverse_k"]
    assert got[0]["detail"].startswith("linked_list_reverse_k(h, k=inf)  # Reverse")


def test_completion_filters_by_the_prefix():
    text = TEXT.replace("    if x ==\n", "    nb\n")
    got = ide.complete(req("complete", 7, 6, text))
    assert [c["label"] for c in got] == ["nbrs"]
    assert got[0]["detail"].startswith("nbrs(grid, r, c=None")


def test_signature_inside_a_call():
    got = ide.signature(req("signature", 8, 18))
    assert got["label"].startswith("nbrs(grid, r, c=None")
    assert got["active"] == 1
    assert got["doc"].startswith("nbrs(grid, r, c=None")
    assert ide.signature(req("signature", 2, 3)) is None


def rename(line, col, new, text):
    return ide.rename({**req("rename", line, col, text), "new": new})


def test_rename_edits_every_mu_reference_and_nothing_else():
    text = TEXT.replace("    if x ==\n", "").replace("    d = nbrs(grid, i,\n", "")
    # res inside the def, not the res the last line binds
    assert rename(2, 3, "out", text) == {
        "edits": [
            {"line": 2, "col": 2, "end": 5},
            {"line": 7, "col": 4, "end": 7},
            {"line": 8, "col": 2, "end": 5},
        ]
    }
    # the memo, from a call; the loop index marked >i
    assert rename(7, 11, "g", text)["edits"] == [
        {"line": 3, "col": 7, "end": 8},
        {"line": 5, "col": 14, "end": 15},
        {"line": 7, "col": 11, "end": 12},
    ]
    assert rename(6, 7, "j", text)["edits"] == [
        {"line": 6, "col": 7, "end": 8},
        {"line": 7, "col": 13, "end": 14},
    ]


def test_rename_refuses_what_it_cannot_do_on_the_mu_lines():
    assert "refused" in rename(2, 3, "for", TEXT)  # a keyword
    assert "refused" in rename(2, 3, "a b", TEXT)
    assert "refused" in rename(8, 10, "near", TEXT)  # a helper
    # i is on line 9, which does not transpile
    assert rename(6, 7, "j", TEXT) == {
        "refused": "line 9 does not transpile; fix it first"
    }


GRID = """def walk(edges: [[int]], n: int) -> int
  G = adjacency(edges, n, directed=False)
  edges = len(G)
  edges
"""


def test_rename_follows_a_rebound_grid_parameter():
    # edges is a helper's name too, and the transpiler wraps a grid
    # parameter in _in_edges and _w_edges
    assert rename(1, 16, "es", GRID)["edits"] == [
        {"line": 0, "col": 9, "end": 14},
        {"line": 1, "col": 16, "end": 21},
        {"line": 2, "col": 2, "end": 7},
        {"line": 3, "col": 2, "end": 7},
    ]


def test_rename_refuses_a_name_already_seen_there():
    assert rename(1, 16, "n", GRID) == {"refused": "`n` is taken where `edges` is seen"}
    assert rename(1, 16, "len", GRID) == {
        "refused": "`len` is taken where `edges` is seen"
    }


def test_a_bad_request_is_an_error_reply_not_a_crash():
    assert ide.answer({"id": 3, "cmd": "hover", "text": "", "line": 5, "col": 0}) == {
        "id": 3,
        "result": None,
    }
    assert "error" in ide.answer({"id": 4, "cmd": "nope", "text": ""})


def test_serve_round_trip():
    run = subprocess.run(
        [sys.executable, "-m", "mulang.ide"],
        input=json.dumps(req("hover", 1, 20)) + "\n",
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=60,
    )
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["result"]["contents"] == "```mu\nnums: [int]\n```"


def test_primes_colours_a_bound_f_read_as_a_prime():
    # two primes: the grammar sees f' - g' as an f-string
    text = (
        "def h(xs: [int]) -> int\n"
        "  f, g, best = 0, 0, 0\n"
        "  for x in xs\n"
        "    best = max(best, f' - g') = (max(f, x), min(g, x))\n"
        "  best\n"
    )
    assert ide.primes({"text": text}) == [
        [3, 21, 1, "variable"],  # f
        [3, 22, 1, "operator"],  # its prime
        [3, 24, 1, "operator"],  # -
        [3, 26, 1, "variable"],  # g
        [3, 27, 1, "operator"],  # its prime
    ]
    # f is the def's name, but f'{a}' reads only as a string
    assert ide.primes({"text": "def f(a: str) -> str\n  f'{a}'\n"}) == []


def test_definition_of_a_name_from_a_mu_module(tmp_path):
    """Go-to-definition on a name imported from a mu module opens the .mu
    at its def."""
    from mulang import ide

    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "nums.mu").write_text("X = 1\n\ndef evens(xs: [int]) -> [int]\n  xs\n")
    main = tmp_path / "main.mu"
    text = "from lib.nums import evens\n\nprint evens([1, 2])\n"
    main.write_text(text)
    req = {"root": str(tmp_path), "path": str(main), "text": text, "line": 2, "col": 7}
    assert ide.definition(req) == [
        {"path": str((tmp_path / "lib" / "nums.mu").resolve()), "line": 2, "col": 0}
    ]


def test_signature_and_definition_of_a_name_from_a_mu_package(tmp_path):
    """A name a package re-exports from its mu modules has signature help and
    a definition through `from pkg import name`, not only `from pkg.mod import`."""
    from mulang import ide

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("from .nums import *\n")
    (tmp_path / "pkg" / "nums.mu").write_text("X = 1\n\ndef evens(xs: [int], flip: bool=false) -> [int]\n  xs\n")
    main = tmp_path / "main.mu"
    text = "from pkg import evens\n\nprint evens(\n"
    main.write_text(text)
    req = {"root": str(tmp_path), "path": str(main), "text": text, "line": 2, "col": 12}
    got = ide.signature({**req, "cmd": "signature"})
    assert got and got["label"] == "evens(xs, flip=False)"
    loc = ide.definition({**req, "line": 2, "col": 8})
    assert loc == [{"path": str((tmp_path / "pkg" / "nums.mu").resolve()), "line": 2, "col": 0}]


# --- the language's own call forms, and the names the run time preloads ---

FORM_TEXT = "def f(nums: [int]) -> int\n  x = {}\n"


def form_req(cmd, line, col=None):
    col = len("  x = " + line) if col is None else col
    return req(cmd, 1, col, FORM_TEXT.format(line))


def test_signature_of_scan_while_the_call_is_incomplete():
    # scan is a form of the language: no Python def stands behind it
    params = ["op", "xs", "type=None", "initial=None", "reverse=False"]
    for line, active in (("scan(", 0), ("scan(+, ", 1), ("scan(max, nums, ", 2)):
        got = ide.signature(form_req("signature", line))
        assert got["label"] == f"scan({', '.join(params)})"
        assert (got["params"], got["active"]) == (params, active)


def test_hover_on_scan_shows_its_forms():
    got = ide.hover(form_req("hover", "scan(+, nums)", col=len("  x = s")))
    assert got["contents"].startswith("```mu\nscan(op, xs, type=None")
    assert "reverse=True" in got["contents"]


def test_every_form_the_transpiler_rewrites_is_documented():
    import inspect
    import re

    import mulang

    src = inspect.getsource(mulang)
    body = src[src.index("    def special(self") :]
    body = body[: body.index("\n    def ", 1)]
    named = set(re.findall(r'fn == "(\w+)"', body))
    for group in re.findall(r"fn in \(([^)]*)\)", body):
        named |= set(re.findall(r'"(\w+)"', group))
    assert named and (named | set(mulang.KEYED)) <= set(ide.FORMS)
    for name in ide.FORMS:
        assert ide.docs()[name][0].startswith(name + "(")


def test_signature_of_the_forms_that_take_another_python_name():
    # sort is sorted, counter is Counter, dict is Rec in the Python
    for line, label in (
        ("sort(nums, ", "sort(xs, by=None, reverse=False)"),
        ("counter(", "counter(xs)"),
        ("dict(", "dict(k=v)"),
    ):
        assert ide.signature(form_req("signature", line))["label"] == label


def test_scan_with_the_wrong_number_of_arguments_is_a_mu_error():
    import pytest

    import mulang

    for call in ("scan()", "scan(+)", "scan(+, nums, nums)"):
        with pytest.raises(mulang.MuError, match="scan takes an operator and an iterable"):
            mulang.transpile(FORM_TEXT.format(call))


def test_label_params_split_at_the_top_level_commas():
    assert ide.label_params("table(m, n, fill=0)") == ["m", "n", "fill=0"]
    assert ide.label_params("put(grid, at, scan(+, xs))") == ["grid", "at", "scan(+, xs)"]
    assert ide.label_params("f()") == [] and ide.label_params("inf") == []


def test_signature_and_hover_of_a_name_the_run_time_preloads(monkeypatch):
    # a sitecustomize puts deque and heappush into builtins: the Python the
    # transpiler writes has no import for them, so Jedi reads the live object
    import builtins
    from collections import deque
    from heapq import heappush

    monkeypatch.setattr(builtins, "deque", deque, raising=False)
    monkeypatch.setattr(builtins, "heappush", heappush, raising=False)
    got = ide.signature(form_req("signature", "heappush(nums, "))
    assert (got["label"], got["params"], got["active"]) == ("heappush(heap, item)", ["heap", "item"], 1)
    assert ide.signature(form_req("signature", "deque("))["label"].startswith("deque(")
    hover = ide.hover(form_req("hover", "deque(nums)", col=len("  x = d")))
    assert hover["contents"].startswith("```mu\ndeque(")


def test_no_signature_for_a_name_nothing_defines():
    assert ide.signature(form_req("signature", "nothing_has_this_name(")) is None
    assert ide.hover(form_req("hover", "nothing_has_this_name(1)", col=len("  x = n"))) is None
