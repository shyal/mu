"""The REPL: one input in, the printed lines out."""

import pytest

from mulang.repl import Repl, needs_more


@pytest.fixture
def repl():
    return Repl()


def test_expressions_and_names(repl):
    assert repl.run("1..5") == ["1..5"]
    assert repl.run("0..<3") == ["0..2"]
    assert repl.run("x = 3") == []
    assert repl.run("x * 2") == ["6"]
    assert repl.run("x > 2") == ["true"]


def test_first_and_folds(repl):
    assert repl.run("first x in 1..100 if x * x >= 50") == ["8"]
    assert repl.run("sum for i in 1..3: i * i") == ["14"]
    assert repl.run("max from 0 for >i, c in 'abca'\n  i") == ["3"]


def test_def_and_memo(repl):
    assert repl.run("def sq(a: int) -> int\n  a * a") == []
    assert repl.run("sq(4)") == ["16"]
    assert (
        repl.run("memo fib(n) =\n  | n < 2 -> n\n  | else -> fib(n - 1) + fib(n - 2)")
        == []
    )
    assert repl.run("fib(90)") == ["2880067194370816120"]


def test_deep_memo_fits(repl):
    repl.run("memo down(n) =\n  | n == 0 -> 0\n  | else -> 1 + down(n - 1)")
    assert repl.run("down(50000)") == ["50000"]


def test_helpers_load_on_use(repl):
    assert repl.run("g = [[1, 0], [1, 1]]") == []
    assert repl.run(
        "len(components({p for p in cells(g) if g[p[0]][p[1]] == 1}, p -> nbrs(g, p)))"
    ) == ["1"]


def test_errors(repl):
    assert repl.run("1 // 0") == [
        "ZeroDivisionError: integer division or modulo by zero"
    ]
    assert repl.run("x = ")[0].startswith("syntax:")
    assert repl.run("y")[0] == "NameError: name 'y' is not defined"


def test_show_py(repl):
    repl.command(":py")
    assert repl.run("1..3") == ["__mu_out__ = range(1, 3 + 1)", "1..3"]


@pytest.mark.parametrize(
    "lines, block, want",
    [
        (["1 + 2"], False, (False, False)),
        (["def f(a: int) -> int"], False, (True, True)),
        (["def f(a: int) -> int", "  a"], True, (True, True)),
        (["def f(a: int) -> int", "  a", ""], True, (False, True)),
        (["x = [1,"], False, (True, False)),
        (["x = [1,", "2]"], False, (False, False)),
        (["memo f(a) ="], False, (True, True)),
        (["x = "], False, (False, False)),
        (["s = '''a", ""], False, (True, False)),  # the blank line is the string's
        (["s = '''a", "", "b'''"], False, (False, False)),
    ],
)
def test_needs_more(lines, block, want):
    assert needs_more(lines, block) == want


def styles(line):
    from mulang.repl import highlight

    parts = highlight(line)
    assert "".join(t for _, t in parts) == line
    return {t: s.removeprefix("class:mu.") for s, t in parts if t.strip()}


def test_highlight_classes():
    got = styles("def firstTrue(lo: int, ok: int -> bool) -> int  # d115")
    assert got["def"] == "control"
    assert got["firstTrue"] == "defname"
    assert got["int"] == "type"
    assert got["->"] == "range"
    assert got["# d115"] == "comment"
    got = styles("max from 0 for i in 0..<len(nums) if nums[i] >= 7: f(i)")
    assert got["max"] == "fold" and got["from"] == "fold" and got["for"] == "control"
    assert got["..<"] == "range"
    assert got["len"] == "builtin" and got["f"] == "call"
    assert got["0"] == "num" and got[">="] == "op"
    assert styles("max(a, b)")["max"] == "builtin"
    assert styles("x = inf")["inf"] == "const"
    got = styles("sum for xs: _ * _x")
    assert got["_"] == "item" and got["_x"] == ""


def test_highlight_survives_half_typed_input():
    assert styles("s = 'abc")["'abc"] == "str"
    assert styles("  | a == 0 -> 0")["|"] == "range"


def test_push_at_the_prompt(repl):
    assert repl.run("s = []") == []
    assert repl.run("s <- 3") == []
    assert repl.run("s") == ["[3]"]
    assert styles("s <- 3")["<-"] == "range"


def test_pop_at_the_prompt(repl):
    repl.run("s = [1, 2]")
    assert repl.run("s .") == ["2"]
    assert repl.run("s.append(5)") == []  # None prints nothing
    assert styles("s . + 1")["."] == "range"
    assert styles("s.x")["."] == ""


def test_f_strings_at_the_prompt(repl):
    repl.run("a = 'xy'")
    assert repl.run("f' {a}'") == ["' xy'"]
    assert styles("f' {a}'")["f' {a}'"] == "str"
