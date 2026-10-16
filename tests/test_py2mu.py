"""py2mu: one test per idiom, with the exact mu it writes and a run of the
transpiled result; then a few cached problems against their asserts."""

import builtins
import subprocess
import textwrap

import pytest

from mulang import transpile
from mulang.py2mu import PYTHON, Untranslatable, translate


def mu_of(body, sig="f(self, grid: List[List[int]]) -> int"):
    src = (
        "class Solution:\n    def "
        + sig
        + ":\n"
        + textwrap.indent(textwrap.dedent(body), "        ")
    )
    return translate(src)


def runs(mu_src, call, want):
    """The mu, transpiled, gives want for Solution().call."""
    code = transpile(mu_src) + f"\nassert Solution().{call} == {want!r}\n"
    r = subprocess.run([PYTHON, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


GRID = [[1, 0, 2], [0, 1, 1]]

CASES = {
    "syntax": (
        """
        out = []
        for i, x in enumerate(grid[0]):
            if x == True or x is None:
                out.append(i)
            elif len(out) > 0:
                out.pop()
        return len(out)
        """,
        """
        def f(grid: [[int]]) -> int
          out = []
          for >i, x in grid[0]
            if x == true or x is none
              out <- i
            elif len(out) > 0
              out .
          len(out)
        """,
        0,
    ),
    "backward": (
        """
        out = []
        for i in range(len(grid[0]) - 1, -1, -1):
            out.append(i)
        for i, x in zip(range(len(grid[0]) - 1, -1, -1), reversed(grid[0])):
            out.append(i * x)
        for i in reversed(range(len(grid))):
            out.append(i)
        return sum(out)
        """,
        """
        def f(grid: [[int]]) -> int
          out = [i for <i in grid[0]]
          for <i, x in grid[0]
            out <- i * x
          for <i in grid
            out <- i
          sum(out)
        """,
        8,
    ),
    "comprehension": (
        """
        out = []
        for row in grid:
            if row[0]:
                out.append(sum(row))
        return len(out) + out[0]
        """,
        """
        def f(grid: [[int]]) -> int
          out = [sum(row) for row in grid if row[0]]
          len(out) + out[0]
        """,
        4,
    ),
    "fold": (
        """
        total = 0
        for row in grid:
            total += row[0]
        best = 0
        for row in grid:
            best = max(best, row[-1])
        return total + best
        """,
        """
        def f(grid: [[int]]) -> int
          total = sum for row in grid: row[0]
          total + (max from 0 for row in grid: row[-1])
        """,
        3,
    ),
    "fold with a block": (
        """
        total = 0
        for row in grid:
            a = row[0]
            total += a * a
        return total
        """,
        """
        def f(grid: [[int]]) -> int
          sum for row in grid
            a = row[0]
            a * a
        """,
        1,
    ),
    "ternary": (
        """
        if grid[0][0] == 1:
            return 5
        return 6
        """,
        """
        def f(grid: [[int]]) -> int
          grid[0][0] == 1 ? 5 : 6
        """,
        5,
    ),
    "cells, like, merged": (
        """
        m, n = len(grid), len(grid[0])
        seen = [[False] * n for _ in range(m)]
        count = 0
        for r in range(m):
            for c in range(n):
                if grid[r][c] == 1:
                    seen[r][c] = True
                    count += 1
        return count
        """,
        """
        def f(grid: [[int]]) -> int
          seen = like(grid, fill=false)
          sum for r, c in cells(grid, eq=1)
            seen[r][c] = true
            1
        """,
        3,
    ),
    "nbrs and levels": (
        """
        m, n = len(grid), len(grid[0])
        q = deque([(0, 0)])
        seen = {(0, 0)}
        while q:
            r, c = q.popleft()
            for dr, dc in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
                nr, nc = r + dr, c + dc
                if 0 <= nr < m and 0 <= nc < n and (nr, nc) not in seen:
                    seen.add((nr, nc))
                    q.append((nr, nc))
        return len(seen)
        """,
        """
        def f(grid: [[int]]) -> int
          q, seen = deque([(0, 0)]), {(0, 0)}
          for _, (r, c) in levels(q)
            for nr, nc in nbrs(grid, r, c)
              if (nr, nc) not in seen
                seen.add((nr, nc))
                q <- (nr, nc)
          len(seen)
        """,
        6,
    ),
    "pairs": (
        """
        row = grid[1]
        n = len(row)
        count = 0
        for i in range(n):
            for j in range(i + 1, n):
                if row[i] == row[j]:
                    count += 1
        return count
        """,
        """
        def f(grid: [[int]]) -> int
          row = grid[1]
          n = len(row)
          count for i, j in pairs(n) if row[i] == row[j]
        """,
        1,
    ),
    "pairs back": (
        """
        row = grid[1]
        n = len(row)
        dp = [0] * n
        for i in range(n):
            for j in range(i):
                if row[j] < row[i]:
                    dp[i] += 1
        return dp[-1]
        """,
        """
        def f(grid: [[int]]) -> int
          row = grid[1]
          n = len(row)
          dp = [0] * n
          for j, i in pairs(n, back=true)
            if row[j] < row[i]
              dp[i] += 1
          dp[-1]
        """,
        1,
    ),
    "first true": (
        """
        lo, hi = 0, 100
        while lo < hi:
            mid = (lo + hi) // 2
            if mid * mid >= 50:
                hi = mid
            else:
                lo = mid + 1
        return lo
        """,
        """
        def f(grid: [[int]]) -> int
          lo, hi = 0, 100
          first mid in lo..<hi if mid * mid >= 50
        """,
        8,
    ),
    "adjacency": (
        """
        g = defaultdict(list)
        for u, v in grid:
            g[u].append(v)
            g[v].append(u)
        return len(g[1])
        """,
        """
        def f(grid: [[int]]) -> int
          g = adjacency(grid, directed=false)
          len(g[1])
        """,
        2,
    ),
}


@pytest.mark.parametrize("case", CASES, ids=list(CASES))
def test_idiom(case):
    if case == "nbrs and levels" and not hasattr(builtins, "deque"):
        pytest.skip("needs the leet harness, whose sitecustomize preloads deque")
    body, want, value = CASES[case]
    got = mu_of(body)
    want = textwrap.indent(textwrap.dedent(want).lstrip("\n"), "  ")
    assert got == "class Solution\n" + want
    edges = [[1, 0], [1, 2]]
    runs(got, f"f({edges if case == 'adjacency' else GRID})", value)


def test_nested_local_is_renamed_so_mu_keeps_it_local():
    """mu would make `left` inside the nested def the outer one."""
    got = mu_of("""
        def g(k):
            left = k
            while left > 3:
                left -= 1
            return left
        left = 1
        return g(5) + left
        """)
    assert "left_ = k" in got
    runs(got, f"f({GRID})", 4)


def test_attributes_stay_shared():
    src = """class Solution:
    def f(self, n: int) -> int:
        self.count = 0
        self.go(n)
        return self.count

    def go(self, n):
        if n:
            self.count += 1
            self.go(n - 1)
"""
    got = translate(src, ["f"])
    assert "count += 1" in got and "count_" not in got
    runs(got, "f(4)", 4)


def test_preloaded_imports_go():
    if not hasattr(builtins, "deque"):
        pytest.skip("needs the leet harness, whose sitecustomize preloads deque")
    got = mu_of("import heapq\nfrom collections import deque\nreturn 1")
    assert "import" not in got


def test_what_mu_cannot_say_is_refused():
    with pytest.raises(Untranslatable, match="Try"):
        mu_of("try:\n    return 1\nexcept ValueError:\n    return 2")


def test_lambdas_and_loop_else():
    got = mu_of("""
        d = defaultdict(lambda: [0, 0])
        xs = sorted(grid[0], key=cmp_to_key(lambda a, b: b - a))
        for x in xs:
            if x == 9:
                break
        else:
            d[1][0] = 3
        return d[1][0] + xs[0]
        """)
    assert "{:() -> [0, 0]}" in got
    assert "cmp_to_key((a, b) -> b - a)" in got
    assert "\n    else\n" in got
    runs("from functools import cmp_to_key\n" + got, f"f({GRID})", 5)


def test_mutations_keep_shape():
    import random

    from mulang.difftest import Pool, mutate

    rng = random.Random(1)
    grid = [[0, 1, 1], [1, 0, 0], [0, 0, 1]]
    rows = ["A.Z", "Z.A"]
    for _ in range(50):
        g = mutate(grid, Pool([grid]), rng)
        assert len(g) == len(g[0]) == 3 and {x for r in g for x in r} <= {0, 1}
        r = mutate(rows, Pool([rows]), rng)
        assert [len(x) for x in r] == [3, 3]


def test_differential_finds_a_wrong_translation():
    from mulang.difftest import differential

    right = "class Solution:\n    def f(self, xs):\n        return max(xs)\n"
    wrong = "class Solution:\n    def f(self, xs):\n        return xs[-1]\n"
    cases = [("f", ["[1, 2, 3]"]), ("f", ["[3, 1]"])]
    assert differential(right, right, cases, PYTHON) == {"compared": 2}
    assert "differs" in differential(right, wrong, cases, PYTHON)


@pytest.mark.parametrize(
    "py",
    [
        "return t if t < inf else -1",
        "return t if t != inf else -1",
        "return -1 if t == inf else t",
    ],
)
def test_or_if_forms_translate_to_one_line(py):
    body = f"t = min(grid[0])\n{py}"
    assert mu_of(body).splitlines()[-1] == "    t or -1 if inf"
    runs(mu_of(body), "f([[2, 1]])", 1)
    runs(mu_of(body), "f([[inf]])", -1)


def test_decorators_and_yields_translate():
    got = mu_of(
        """
        @wraps(len)
        @cache
        def depth(i):
            return 0 if i == 0 else 1 + depth(i - 1)
        for row in grid:
            yield depth(row[0]), row[1]
        yield from grid[0]
        yield
        """,
        sig="f(self, grid: List[List[int]])",
    )
    assert "    @wraps(len)\n    memo depth(i) = " in got
    assert (
        "      yield depth(row[0]), row[1]\n    yield from grid[0]\n    yield\n" in got
    )
    src = "class Solution:\n    @as_list\n    def f(self, n: int) -> List[int]:\n        yield n\n"
    assert (
        translate(src)
        == "class Solution\n  @as_list\n  def f(n: int) -> [int]\n    yield n\n"
    )
    runs(translate(src), "f(3)", [3])


@pytest.mark.parametrize(
    "decorators",
    ["@staticmethod", "@cache\n    @as_list"],
    ids=["takes no self", "cache is not nearest the def"],
)
def test_decorators_mu_cannot_say_are_refused(decorators):
    src = f"class Solution:\n    {decorators}\n    def f(n):\n        return n\n"
    with pytest.raises(Untranslatable, match="decorators of f"):
        translate(src)


def test_parameter_defaults_translate():
    got = mu_of(
        "return len(xs) if reverse else by(len(xs))",
        sig="f(self, xs: List[int], by=lambda x: x, reverse: bool = False) -> int",
    )
    assert "def f(xs: [int], by=x -> x, reverse: bool=false) -> int" in got
    runs(got, "f([1, 2], reverse=True)", 2)
    runs(got, "f([1, 2], by=lambda n: n * 10)", 20)
