"""The language: what each construct transpiles to, and what it refuses.

Programs run with `-S`, so nothing the test interpreter preloads leaks in:
what passes here is what the transpiled Python does on its own.
"""

import re
import subprocess
from pathlib import Path

import pytest

from mulang import SIDES, VERSION, MuError, fmt, ide, transpile

HERE = Path(__file__).parent
REPO = HERE.parent


@pytest.mark.parametrize(
    "src, message",
    [
        ("def f(a: int) -> int\n  sum for x in a\n    y = x\n", "must be a value"),
        ("def f(a: int) -> int\n  first k in a if k\n", "needs a range"),
        ("def f(a: int) -> int\n    x = 1\n  x\n", "indentation"),
        ("def f(a: int) -> int\n  ret x = 1\n  ret y = 2\n", "more than one ret"),
        ("def f(a: int) -> int\n  for i in a\n    ret x = i\n", "own body"),
        ("@as_list\nx = 1\n", "a decorator goes above a def, a memo or a class"),
        ("def f(a: int) -> int\n  a\n@as_list\n", "a decorator goes above a def"),
    ],
)
def test_errors(src, message):
    with pytest.raises(MuError, match=re.escape(message)):
        transpile(src)


def test_ret_names_the_return_value():
    src = (
        "def decode(encoded: [int], first: int) -> [int]\n"
        "  ret res = [first]\n"
        "  for i in range len encoded\n"
        "    if encoded[i] < 0\n"
        "      return\n"
        "    res <- encoded[i] ^ res[i]\n"
        "  ret = 0\n"
    )
    ns: dict = {}
    exec(transpile(src), ns)
    decode = ns["decode"]
    assert decode([1, 2, 3], 1) == [1, 0, 2, 1]
    assert decode([1, -1, 3], 1) == [1, 0]


def test_ret_takes_the_first_name_of_a_tuple_assignment():
    src = "def f() -> int\n  ret res, foo = 0, 10\n  res += foo\n"
    ns: dict = {}
    exec(transpile(src), ns)
    assert ns["f"]() == 10


def test_fmt_messy():
    messy = (
        "# 322. Coin Change\n"
        "class Solution\n"
        "    def coinChange(coins:[int],amount:int)->int\n"
        "        memo f(a)=\n"
        "            |a==0->0\n"
        "            |a<0->inf\n"
        "            |else->1+min for c in coins:f(a-c)\n"
        "        f(amount) or -1 if inf\n"
    )
    want = (HERE / "examples" / "0322.mu").read_text()
    assert fmt(messy) == want
    assert fmt(want) == want


def test_fmt_ranges_unary_and_joined_brackets():
    src = "def f(a: [int]) -> int\n  x = [ -1 ,\n     2 ]\n  sum for i in 0 ..< len( a ): a[ i ] * -x[0]\n"
    assert fmt(src) == (
        "def f(a: [int]) -> int\n  x = [-1, 2]\n  sum for i in 0..<len(a): a[i] * -x[0]\n"
    )


def test_a_range_takes_a_step_with_by():
    src = (
        "def f(n: int, s: int) -> [[int]]\n"
        "  a = [j for j in 2..<n by 3]\n"
        "  b = [j for j in 0..n by 5]\n"
        "  c = [j for j in n..0 by -3]\n"
        "  d = [j for j in 0..n by s]\n"
        "  e = [j for j in n..0 by -s]\n"
        "  [a, b, c, d, e]\n"
    )
    out = transpile(src)
    assert "range(2, n, 3)" in out and "range(0, n + 1, 5)" in out
    # an inclusive end moves the way the step goes: a literal step at compile
    # time, any other step at run time
    assert "range(n, 0 - 1, -3)" in out
    assert "range(0, n + (1 if s > 0 else -1), s)" in out
    want = [[2, 5, 8], [0, 5, 10], [10, 7, 4, 1], [0, 4, 8], [10, 6, 2]]
    assert run(src, 10, 4) == want
    assert fmt(src) == src
    assert fmt("def f(n: int) -> int\n  sum for j in 0 .. n by  - 2: j\n") == (
        "def f(n: int) -> int\n  sum for j in 0..n by -2: j\n"
    )


def test_push_operator():
    src = "def f(a: [int], x: int) -> [int]\n  if x < -1: a <- -x\n  a <- x\n"
    out = transpile(src)
    assert "if x < -1:" in out
    assert "a.append(-x)" in out
    assert "return a.append" not in out  # a push is never the block's value
    assert fmt("def f(a: [int]) -> int\n  a<-  -1\n  a[0]\n") == (
        "def f(a: [int]) -> int\n  a <- -1\n  a[0]\n"
    )


def test_pop_dot():
    src = "def f(stack: [int], res: [int]) -> int\n  res[stack .] = 1\n  stack .\n  x = stack . + 1\n  stack.count(x)\n"
    out = transpile(src)
    assert "res[stack.pop()] = 1" in out
    assert "    stack.pop()" in out
    assert "x = stack.pop() + 1" in out
    assert "return stack.count(x)" in out  # a name after the dot is an attribute
    assert "w = s.pop() if s else 0" in transpile(
        "def f(s: [int]) -> int\n  w = s . if s else 0\n  w\n"
    )
    assert fmt("def f(s: [int]) -> int\n  res[s.] = 1\n  s .\n  s.x\n") == (
        "def f(s: [int]) -> int\n  res[s .] = 1\n  s .\n  s.x\n"
    )


def test_a_push_is_a_comprehension_element():
    src = "def f(s: str) -> dict\n  pos = {:list}\n  [pos[v] <- i for >i, v in s]\n  pos\n"
    assert "[pos[v].append(i) for i, v in enumerate(s)]" in transpile(src)
    assert fmt(src.replace(" <- ", "<-")) == src
    assert "(a.append(x * 2) for x in a)" in transpile(
        "def f(a: [int]) -> int\n  g = (a <- x * 2 for x in a)\n  0\n"
    )
    assert "[out.append(st.pop()) for _ in range(0, 2)]" in transpile(
        "def f(st: [int], out: [int]) -> int\n  [out <- st . for _ in 0..<2]\n  0\n"
    )


def run_f(src, *args):
    ns = {}
    exec(transpile(src), ns)
    return ns["f"](*args)


def test_a_default_dict_collects_under_each_key():
    src = "def f(s: str) -> dict\n  {:list: c <- i for >i, c in s}\n"
    assert "_pushed(defaultdict(list), ((c, i) for i, c in enumerate(s)))" in transpile(
        src
    )
    assert run_f(src, "egg") == {"e": [0], "g": [1, 2]}
    assert fmt(src.replace(": c <- i", ":c<-i")) == src
    src = "def f(es: [[int]]) -> dict\n  {:set: b <- a for (a, b) in es}\n"
    assert run_f(src, [[1, 0], [2, 0], [1, 0]]) == {0: {1, 2}}
    src = "def f(xs: [int]) -> dict\n  {:() -> [9]: x % 2 <- x for x in xs if x != 2}\n"
    assert run_f(src, [0, 1, 2, 3]) == {0: [9, 0], 1: [9, 1, 3]}


def test_a_default_dict_updates_each_key_in_place():
    src = "def f(s: str) -> dict\n  {:int: c += 1 for c in s}\n"
    assert "_updated(defaultdict(int), ((c, 1) for c in s), iadd)" in transpile(src)
    assert run_f(src, "egg") == {"e": 1, "g": 2}
    assert fmt(src) == src
    src = "def f(ps: [(int, {int})]) -> dict\n  {:set: k |= xs for (k, xs) in ps}\n"
    assert run_f(src, [(1, {2}), (1, {3})]) == {1: {2, 3}}


@pytest.mark.parametrize(
    "body, message",
    [
        ("{:list: v <- 1}", "expected 'for' after v <- 1"),
        ("{:list: v for v in s}", "expected <- or an operator like \\+=, got 'for'"),
        ("{:list: v = 1 for v in s}", "expected <- or an operator like \\+=, got '='"),
    ],
)
def test_a_collecting_default_dict_says_what_is_missing(body, message):
    with pytest.raises(MuError, match=f"line 2: {message}"):
        transpile(f"def f(s: str) -> dict\n  {body}\n")


def test_a_push_in_brackets_needs_a_for():
    with pytest.raises(MuError, match="line 2: a push in brackets needs a for"):
        transpile("def f(a: [int]) -> int\n  [a <- 1, 2]\n")


def test_fmt_keeps_a_space_after_a_pop_in_a_comprehension():
    src = "def f(h: [int]) -> [int]\n  [h .for _ in 0..1]\n"
    assert fmt(src) == "def f(h: [int]) -> [int]\n  [h . for _ in 0..1]\n"


def test_fmt_moves_one_line_bodies_onto_their_own_line():
    src = (
        "def f(a: [int]) -> int\n"
        "  for x in a: if x > 0: a <- x  # kept\n"
        "  if s[1:2] == {1: 2}: return 1\n"
        "  elif sum for x in a: x > 3\n"
        "    return 2\n"
        "  else: return 3\n"
    )
    assert fmt(src) == (
        "def f(a: [int]) -> int\n"
        "  for x in a\n"
        "    if x > 0\n"
        "      a <- x  # kept\n"
        "  if s[1:2] == {1: 2}\n"
        "    return 1\n"
        "  elif sum for x in a: x > 3\n"
        "    return 2\n"
        "  else\n"
        "    return 3\n"
    )
    assert fmt(fmt(src)) == fmt(src)


def test_one_argument_calls_drop_their_brackets():
    src = (
        "def f(nums: [int], grid: [[int]], v: int) -> int\n"
        "  x = len nums - 1\n"
        "  print len nums\n"
        "  y = max(x, 3) + int '7'\n"
        "  count for (i, j) in cells grid if grid[i][j] == v\n"
    )
    out = transpile(src)
    assert "x = len(nums) - 1" in out
    assert "print(len(nums))" in out
    assert "y = max(x, 3) + int('7')" in out
    assert "for (i, j) in cells(grid) if" in out
    # keywords, operators and brackets end the argument
    out = transpile(
        "def g(a: [int], s: [int]) -> int\n  helper(1)\n  a[0] if a else s -1\n"
    )
    assert "a[0] if a else s - 1" in out


def test_a_bracketless_call_takes_a_generator():
    src = "def f(a: [int]) -> int\n  return len set self.find y for y in a\n"
    out = transpile(src)
    assert "len(set(self.find(y) for y in a))" in out
    src = "def g(a: [int]) -> int\n  sum x for x in a if x > 0\n"
    assert "sum(x for x in a if x > 0)" in transpile(src)


def test_string_prefixes():
    out = transpile("def f(a: str) -> str\n  b = f' {a}'\n  r'\\d' + b\n")
    assert "b = f' {a}'" in out
    assert "return r'\\d' + b" in out
    assert (
        fmt("def f(a: str) -> str\n  f' {a}'\n") == "def f(a: str) -> str\n  f' {a}'\n"
    )


def test_chained_assignment():
    src = (
        "def f(n: int) -> int\n  a = b = n\n  def g()\n    a = b = 0\n  g()\n  a + b\n"
    )
    out = transpile(src)
    assert "a = b = n" in out
    assert "nonlocal a, b" in out  # every target of the chain is bound
    assert "return a + b" in out
    assert (
        fmt("def f() -> int\n  a=b  =1\n  a\n") == "def f() -> int\n  a = b = 1\n  a\n"
    )
    with pytest.raises(MuError):
        transpile("def f() -> int\n  a += b = 1\n  a\n")


def test_assignment_converts_targets():
    src = "def f(log: str) -> int\n  int(id), ev, int(t) = log.split ':'\n  id + t\n"
    out = transpile(src)
    assert "id, ev, t = log.split(':')" in out
    assert "id, t = int(id), int(t)" in out
    ns = {}
    exec(out, ns)
    assert ns["f"]("0:start:3") == 3
    assert fmt(src) == src


def run(src, *args):
    ns = {}
    exec(transpile(src), ns)
    return ns["f"](*args)


def test_a_value_left_of_assignment_binds_its_new_names():
    src = "def f(nums: [int]) -> int\n  a * b = counter(nums).values()\n"
    out = transpile(src)
    assert "a, b = Counter(nums).values()" in out
    assert "return a * b" in out
    assert run(src, [1, 1, 2, 2, 2]) == 6
    assert fmt(src) == src


def test_a_value_side_reads_what_is_called_or_already_bound():
    # n is a parameter and abs is called: only r and c are new
    src = "def f(k: int, n: int) -> int\n  abs(n - r - 1) * n + c = divmod(k, n)\n"
    assert "r, c = divmod(k, n)" in transpile(src)
    assert run(src, 7, 3) == 1
    # a builtin passed by name is read, and so is a comprehension's variable
    src = "def f(p: [[int]]) -> int\n  sum(map(abs, a)) * len([x for x in b]) = p\n"
    assert "a, b = p" in transpile(src)
    assert run(src, [[-1, 2], [0, 0, 0]]) == 9


def test_value_sides_chain_right_to_left():
    src = (
        "def f(nums: [int]) -> int\n"
        "  double = x -> x * 2\n"
        "  a * 10 = int(a) * double(float(b)) = counter(nums).values()\n"
    )
    out = transpile(src)
    assert "a, b = Counter(nums).values()" in out
    assert "a = int(a) * double(float(b))" in out
    assert "return a * 10" in out
    assert run(src, [1, 1, 2, 2, 2]) == 120
    assert fmt(src) == src


def test_a_target_left_of_a_value_side_is_assigned_it():
    src = (
        "def f(h: [int]) -> int\n"
        "  ret best = 0\n"
        "  for i in 0..<len(h)\n"
        "    best = max(best, area) = x * i = h[i]\n"
    )
    out = transpile(src)
    assert "x = h[i]" in out and "area = x * i" in out
    assert "best = max(best, area)" in out
    assert run(src, [5, 1, 4]) == 8


def test_a_subscript_of_a_new_name_is_a_value_side():
    src = "def f() -> int\n  sum(x[:3]) = x[::-1] = 0..10\n"
    out = transpile(src)
    assert "x = range(0, 10 + 1)" in out and "x = x[::-1]" in out
    assert run(src) == 27
    # a subscript of a bound name is still a target
    src = "def f(xs: [int]) -> [int]\n  xs[0] = a + b = xs[1:]\n  xs\n"
    assert "xs[0] = a + b" in transpile(src)
    assert run(src, [0, 2, 3]) == [5, 2, 3]
    # and so is every subscript on a line with no value side
    assert "x[0] = y[0] = 5" in transpile("def f() -> int\n  x[0] = y[0] = 5\n")


def test_a_value_side_needs_a_new_name():
    with pytest.raises(MuError, match="line 2: `k \\* n` binds no new name"):
        transpile("def f(k: int, n: int) -> int\n  k * n = divmod(k, n)\n")
    with pytest.raises(MuError, match="a ret line takes one plain assignment"):
        transpile("def f(p: [int]) -> int\n  ret r = a * b = p\n")


def test_a_prime_binds_a_bound_name_again_on_a_value_side():
    # 121. Best Time to Buy and Sell Stock
    src = (
        "def f(prices: [int]) -> int\n"
        "  min_price, max_profit = inf, 0\n"
        "  for price in prices\n"
        "    max_profit = max(price - min_price', max_profit) = min(min_price, price)\n"
        "  max_profit\n"
    )
    out = transpile(src)
    assert "min_price = min(min_price, price)" in out
    assert "max_profit = max(price - min_price, max_profit)" in out
    assert run(src, [7, 1, 5, 3, 6, 4]) == 5
    assert fmt(src) == src


def test_a_prime_after_f_r_or_b_needs_the_name_bound():
    # f is bound: f' is a prime, even with a second quote on the line
    src = (
        "def f(xs: [int]) -> int\n"
        "  f, best = 0, 0\n"
        "  for x in xs\n"
        "    best = max(best, f' - x) = max(f, x)\n"
        "  best\n"
    )
    out = transpile(src)
    assert "f = max(f, x)" in out and "best = max(best, f - x)" in out
    assert run(src, [3, 1, 4]) == 2
    # nothing binds r: r'...' is a raw string, as before
    assert "r'a\\b'" in transpile("def f() -> str\n  r'a\\b'\n")
    # a string after a name with a space stays a string
    assert run("def f(s: str) -> str\n  s + 'x'\n", "a") == "ax"


def test_a_prime_is_refused_off_a_value_side():
    with pytest.raises(MuError, match="line 2: `n'` marks a name bound again"):
        transpile("def f(n: int) -> int\n  n' + 1\n")
    with pytest.raises(MuError, match="`n'` is a target"):
        transpile("def f(n: int) -> int\n  n' = a + b = 3\n")
    with pytest.raises(MuError, match="`k'` marks a name bound again, but k is not"):
        transpile("def f(n: int) -> int\n  a = k' + n = 3\n")


def test_a_parameter_may_have_a_default():
    src = (
        "def f(xs: [int], by=x -> x, reverse: bool=false) -> [int]\n"
        "  ys = [by(x) for x in xs]\n"
        "  if reverse\n"
        "    ys = [-y for y in ys]\n"
        "  [*scan(max, map(sub, ys[1:], scan(min, ys)))]\n"
    )
    out = transpile(src)
    assert "by=lambda x: x, reverse: bool = False" in out
    ns = {}
    exec("from operator import sub\n" + out, ns)
    assert ns["f"]([12, 6, 1, 2, 7]) == [-6, -5, 1, 6]
    assert ns["f"]([12, 6, 1, 2, 7], reverse=True) == [6, 11, 11, 11]
    assert fmt(src) == src
    with pytest.raises(MuError, match="`k` needs a default"):
        transpile("def f(n=1, k) -> int\n  n + k\n")


def test_one_conversion_alone_is_the_value_of_a_last_line():
    src = "def f(n: int) -> int\n  sum(ds) = to_digits(n)\n"
    assert run(src, 123) == 6
    # anywhere else it converts and goes on, as before
    src = "def f(s: str) -> int\n  int(a) = s\n  a + 1\n"
    out = transpile(src)
    assert "a = int(a)" in out and "return a + 1" in out
    assert run(src, "41") == 42


def test_a_loop_that_names_no_variable_reads_the_item_as_underscore():
    src = "def f(ps: [(int, int)]) -> int\n  ret s = 0\n  for ps\n    s += _[0]\n"
    out = transpile(src)
    assert "for _ in ps:" in out
    ns = {}
    exec(out, ns)
    assert ns["f"]([(1, 9), (2, 9)]) == 3
    assert fmt(src) == src


def test_sum_from_is_the_start_value_so_non_numbers_add_up():
    src = "def f(rows: [[int]]) -> Counter\n  sum from Counter() for row in rows: Counter(row)\n"
    out = transpile(src)
    assert "sum([Counter(row) for row in rows], Counter())" in out
    ns = {"Counter": __import__("collections").Counter}
    exec(out, ns)
    assert ns["f"]([[1, 2], [2, 3]]) == {1: 1, 2: 2, 3: 1}


@pytest.mark.parametrize(
    "body, want",
    [
        ("sum for xs: _ * _", 14),
        ("max from 0 for xs if _ < 3: _", 2),
        ("count for xs if _ > 1", 2),
        ("sum for xs\n    y = _ * 2\n    y", 12),
    ],
)
def test_a_fold_that_names_no_variable_reads_the_item_as_underscore(body, want):
    src = f"def f(xs: [int]) -> int\n  {body}\n"
    out = transpile(src)
    assert "for _ in xs" in out
    ns = {}
    exec(out, ns)
    assert ns["f"]([1, 2, 3]) == want
    assert fmt(src) == src


def test_a_fold_over_an_attribute_call_names_no_variable():
    src = "def f(d: {str: int}) -> int\n  sum for d.values(): _\n"
    out = transpile(src)
    assert "sum([_ for _ in d.values()])" in out
    ns = {}
    exec(out, ns)
    assert ns["f"]({"a": 2, "b": 5}) == 7


def test_a_loop_may_name_no_variable():
    src = "def f(n: int) -> int\n  k = 0\n  for 0..<n\n    k += 2\n  k\n"
    out = transpile(src)
    assert "for _ in range(0, n):" in out
    ns = {}
    exec(out, ns)
    assert ns["f"](3) == 6
    assert fmt(src) == src
    assert "for i, x in enumerate(xs):" in transpile(
        "def f(xs: [int]) -> int\n  k = 0\n  for >i, x in xs\n    k += i\n  k\n"
    )


def test_a_mark_gives_the_index_of_a_loop():
    def loop(head):
        src = f"def f(xs: [int], es: [[int]]) -> int\n  k = 0\n  for {head}\n    k += 1\n  k\n"
        return next(ln.strip() for ln in transpile(src).splitlines() if "for " in ln)

    assert loop(">i in xs") == "for i in range(len(xs)):"
    assert loop(">i, x in xs") == "for i, x in enumerate(xs):"
    assert loop(">i, (a, b) in es") == "for i, (a, b) in enumerate(es):"
    assert loop("<i in xs") == "for i in range(len(xs) - 1, -1, -1):"
    assert (
        loop("<i, x in xs")
        == "for i, x in zip(range(len(xs) - 1, -1, -1), reversed(xs)):"
    )
    assert loop("a, b in es") == "for a, b in es:"
    assert loop("(a, b) in es") == "for (a, b) in es:"
    assert loop("x in xs") == "for x in xs:"
    assert (
        transpile("def f(xs: [int]) -> int\n  sum for >i, x in xs: i * x\n").count(
            "enumerate(xs)"
        )
        == 1
    )
    assert (
        transpile("def f(xs: [int]) -> [int]\n  [x for <i, x in xs if i]\n").count(
            "reversed(xs)"
        )
        == 1
    )
    with pytest.raises(MuError, match="~ no longer marks the index"):
        loop("~i, x in xs")
    with pytest.raises(MuError, match="> goes on the first name only"):
        loop("a, >i in es")
    with pytest.raises(MuError, match=r"write <i, \(a, b\)"):
        loop("<i, a, b in es")
    assert fmt("for > i,x in xs\n  k += i\n") == "for >i, x in xs\n  k += i\n"
    assert fmt("for < i in xs\n  k += i\n") == "for <i in xs\n  k += i\n"
    assert fmt("if a<b\n  k += i\n") == "if a < b\n  k += i\n"


def test_a_backward_loop_runs_the_indices_down():
    src = (
        "def f(s: str) -> [int]\n  out = []\n  for <i, c in s\n"
        "    out <- i if c == 'a' else -1\n  out\n"
    )
    assert run(src, "aba") == [2, -1, 0]
    src = "def f(s: str) -> [int]\n  [i for <i in s]\n"
    assert run(src, "abc") == [2, 1, 0]


def test_a_lambda_may_be_assigned_to_a_name():
    src = "def f(a: int, b: int) -> int\n  add = (x, y) -> x + y\n  add(a, b)\n"
    ns = {}
    exec(transpile(src), ns)
    assert ns["f"](2, 3) == 5
    assert fmt(src) == src


def test_a_brace_literal_starting_with_a_colon_is_a_defaultdict():
    src = (
        "def f(xs: [int]) -> int\n"
        "  d = {:list}\n"
        "  e = {:() -> [0, 0]}\n"
        "  s = {1}\n"
        "  for x in xs\n"
        "    d[x % 2] <- x\n"
        "    e[x][0] += 1\n"
        "  len(d[0]) + e[3][0] + len(s)\n"
    )
    py = transpile(src)
    assert "from collections import defaultdict" in py
    assert "defaultdict(list)" in py and "defaultdict(lambda: [0, 0])" in py
    ns = {}
    exec(py, ns)
    assert ns["f"]([2, 3, 4]) == 4
    assert fmt(src) == src


def test_heap_pushes_and_pops_with_the_list_operators():
    src = (
        "def f(xs: [int]) -> [int]\n"
        "  lo, hi = heap(xs), heap(xs, type=max)\n"
        "  lo <- 0\n"
        "  hi <- 99\n"
        "  [lo ., hi ., hi.peek(), len(lo)]\n"
    )
    ns = {}
    exec(transpile(src), ns)
    assert ns["f"]([5, 1, 7]) == [0, 99, 7, 3]


def test_triple_quoted_strings_span_lines():
    src = (
        "def f() -> str\n"
        "  a = '''one\n"
        '  two\'\'\' + """x"""\n'
        '  b = f"""{a}\n'
        "\n"
        'it\'s "quoted" """\n'
        "  b\n"
    )
    out = transpile(src)
    ns = {}
    exec(out, ns)
    assert ns["f"]() == 'one\n  twox\n\nit\'s "quoted" '
    assert fmt(src) == src  # the string's own lines are never touched
    assert fmt(fmt(src)) == fmt(src)
    with pytest.raises(MuError, match="line 2: unclosed ''' string"):
        transpile("def f() -> str\n  '''abc\n")


def test_vscode_grammar_highlights_every_helper():
    """The VS Code grammar's builtin rule names every helper and every
    name the environment preloads, so a new helper cannot ship without
    highlighting."""
    import json

    grammar = json.loads(
        (REPO / "vscode" / "syntaxes" / "mu.tmLanguage.json").read_text()
    )
    rule = grammar["repository"]["builtin"]["match"]
    listed = set(re.search(r"\\b\((.*?)\)\\b", rule).group(1).split("|"))
    # Grid and deep are pasted in by the transpiler, never written in mu
    helpers = set(ide.library())
    assert helpers <= listed, sorted(helpers - listed)


def test_vscode_grammar_highlights_the_item_underscore():
    import json

    grammar = json.loads(
        (REPO / "vscode" / "syntaxes" / "mu.tmLanguage.json").read_text()
    )
    rule = re.compile(grammar["repository"]["item"]["match"])
    assert rule.search("sum for xs: _ * 2")
    assert not rule.search("_x = 0") and not rule.search("x_ = 0")
    assert {"include": "#item"} in grammar["patterns"]


def test_vscode_grammar_colours_a_run_of_dots_as_one_operator():
    """`0...,` is a repeat, not a range and a pop; `2x` is a number then a name."""
    import json

    grammar = json.loads(
        (REPO / "vscode" / "syntaxes" / "mu.tmLanguage.json").read_text()
    )
    ops = grammar["repository"]["operator"]["patterns"]
    rng = re.compile(next(p for p in ops if p["name"] == "keyword.operator.range.mu")["match"])
    assert [m.group() for m in rng.finditer("a, b = 0..., 1.., 0..<n, xs[...]")] == [
        "...", "..", "..<", "...",
    ]
    number = re.compile(grammar["repository"]["number"]["match"])
    assert [m.group() for m in number.finditer("1 + 8n, 0..n, x2")] == ["1", "8", "0"]


def test_pygments_lexer_reads_ranges_repeats_and_a_number_before_a_name():
    from pygments.token import Name, Number, Operator

    from mulang.pygments_lexer import MuLexer

    toks = [(t, v) for t, v in MuLexer().get_tokens("a, b = 0..., 1..\nfor i in 0..<n\ny = 8n\n")]
    assert (Number.Integer, "0") in toks and (Operator, "...") in toks
    assert (Operator, "..") in toks and (Operator, "..<") in toks
    assert (Number.Integer, "8") in toks and (Name, "n") in toks
    assert not any(t in Number.Float for t, _ in toks)


def test_transpile_maps_every_python_line_to_its_mu_line():
    """transpile(src, mapped=True) gives, per output line, the mu line it
    came from; pasted helpers, blank lines and the class line have none."""
    src = (HERE / "examples" / "2231.mu").read_text()
    code, origin = transpile(src, mapped=True)
    lines = code.split("\n")
    assert len(origin) == len(lines) - 1  # the trailing newline
    mu = src.split("\n")
    for line, o in zip(lines, origin):
        if o is None:
            continue
        assert mu[o - 1].strip() and not mu[o - 1].startswith("#"), (line, o)
    assert origin[lines.index("class Solution:")] == 2
    assert origin[lines.index("    def largestInteger(self, num: int) -> int:")] == 3
    assert origin[lines.index("        ds = to_digits(num)")] == 4
    assert (
        origin[lines.index("        return to_int([by[d % 2].pop() for d in ds])")] == 6
    )
    # a file with globals and a script maps the same way
    src = "N = 3\ndef f(a: int) -> int\n  a + N\n\nprint(f(1))\n"
    code, origin = transpile(src, mapped=True)
    lines = code.split("\n")
    assert len(origin) == len(lines) - 1
    assert origin[lines.index("N = 3")] == 1
    assert origin[lines.index("    return a + N")] == 3
    assert origin[lines.index("print(f(1))")] == 5


def test_vscode_debug_adapter():
    """The debug adapter's own tests (vscode/adapter.test.js): the
    protocol rewriting, and a live run against the bundled debugpy when the
    Python Debugger extension is installed."""
    import shutil

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    run = subprocess.run(
        [node, "--test", str(REPO / "vscode" / "adapter.test.js")],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-3000:]


def test_a_helper_passed_by_name_is_pasted():
    """`even` in `sort(ds, by=even)` or `[even, odd]` is a use of the helper,
    so its source goes into the output; a file that binds the name keeps its
    own."""
    src = "def f(ds: [int]) -> [int]\n  sort(ds, by=even)\n"
    assert "def even(n):" in transpile(src)
    src = "def f(ds: [int]) -> [int]\n  [r for r in [even, odd]]\n"
    out = transpile(src)
    assert "def even(n):" in out and "def odd(n):" in out
    src = "def f(ds: [int], odd: int) -> int\n  even = 1\n  even + odd\n"
    out = transpile(src)
    assert "def even(n):" not in out and "def odd(n):" not in out


def test_filter_by_is_the_items_the_predicate_holds_for():
    src = "def f(n: int) -> [int]\n  filter(0..n-1, by=odd)\n"
    out = transpile(src)
    assert "return list(filter(odd, range(0, n - 1 + 1)))" in out
    assert "def odd(n):" in out
    assert run(src, 10) == [1, 3, 5, 7, 9]
    src = "def f(xs: [int]) -> [int]\n  filter(xs, by=x -> x > 2)\n"
    assert run(src, [1, 2, 3, 4]) == [3, 4]
    # a value that is not a function indexes, as on the keyed calls
    src = "def f(flags: [bool]) -> [int]\n  filter(0..<len flags, by=flags)\n"
    assert run(src, [True, False, True]) == [0, 2]
    # Python's own filter, positional, is left alone
    plain = "def f(xs: [int]) -> [int]\n  filter(odd, xs)\n"
    assert "return filter(odd, xs)" in transpile(plain)


def test_scan_reverse_folds_from_the_right():
    """`scan(max, xs, reverse=True)` is the running max of every suffix."""
    src = "def f(xs: [int]) -> [int]\n  scan(max, xs, reverse=True)\n"
    assert "reversed(list(accumulate(xs[::-1], max)))" in transpile(src)
    src = "def f(xs: [int]) -> [int]\n  scan(+, xs, reverse=True, type=list)\n"
    assert "list(accumulate(xs[::-1]))[::-1]" in transpile(src)
    src = "def f(xs: [int]) -> [int]\n  scan(max, xs)\n"
    assert "accumulate(xs, max)" in transpile(src)


def test_scan_type_builds_a_list_or_a_tuple():
    """`type=list` or `type=tuple` builds the scan; with no type it is an iterator."""
    src = "def f(xs: [int]) -> [int]\n  scan(min, xs, type=list)\n"
    assert "list(accumulate(xs, min))" in transpile(src)
    src = "def f(xs: [int]) -> [int]\n  scan(min, xs, reverse=true, type=tuple)\n"
    assert "tuple(accumulate(xs[::-1], min))[::-1]" in transpile(src)
    src = "def f(xs: [int]) -> [int]\n  scan(min, xs, type=none)\n"
    assert "return accumulate(xs, min)" in transpile(src)


def test_a_dict_mu_makes_gives_its_keys_as_attributes():
    """`dict(a=1)`, `{k: v}`, `{}` and a dict comprehension are a Rec, so
    `d.k` is `d['k']`; a set and a default dict stay as Python has them."""
    src = (
        "a = dict(a=1)\nb = {'x': 2}\nb.y = 3\nc = {}\n"
        "d = {k: k for k in range(2)}\ns = {1, 2}\ng = {:list}\n"
    )
    out = transpile(src)
    assert "a = Rec(a=1)\n" in out
    assert "b = Rec({'x': 2})\n" in out
    assert "c = Rec()\n" in out
    assert "d = Rec({k: k for k in range(2)})\n" in out
    assert "s = {1, 2}\n" in out
    assert "g = defaultdict(list)\n" in out
    ns = {}
    exec(out, ns)
    assert (ns["a"].a, ns["b"], ns["d"][1]) == (1, {"x": 2, "y": 3}, 1)
    assert not hasattr(ns["c"], "q")


def test_every_spec_version_is_linked_from_the_readme():
    """The current version has a spec, and the README links every spec."""
    specs = sorted(p.name for p in (REPO / "spec").glob("v*.md"))
    assert f"v{VERSION}.md" in specs
    readme = (REPO / "README.md").read_text()
    linked = re.findall(r"\(spec/(v[\d.]+\.md)\)", readme)
    assert sorted(linked) == specs


def test_memo_block_form_is_a_cached_def_on_a_deep_stack():
    src = (
        "def f(n: int) -> int\n"
        "  memo g(i)\n"
        "    m = []\n"
        "    if i < n\n"
        "      m <- g(i + 1)\n"
        "    return max(m) + 1 if m else 0\n"
        "  return g(0)\n"
    )
    py = transpile(src)
    assert "@cache\n        def g(i):" in py.replace("    def run():\n", "").replace(
        "            ", "        "
    )
    ns = {}
    exec(py, ns)
    assert ns["f"](20000) == 20000
    assert fmt(src) == src


def test_a_fold_in_a_memo_recurses_deep_on_every_python():
    # 322. Coin Change: the fold's min would pull a generator through C at
    # every level, which Python 3.12 and 3.13 stop short of depth 10000
    src = (
        "def f(coins: [int], amount: int) -> int\n"
        "  memo g(a) = a == 0 ? 0 : 1 + min from inf for c in coins if c <= a: g(a - c)\n"
        "  g(amount) or -1 if inf\n"
    )
    assert "min([g(a - c) for c in coins if c <= a], default=inf)" in transpile(src)
    assert run(src, [1], 20000) == 20000
    assert run(src, [7, 11, 13], 10000) == 770


def test_memo_block_form_rejects_ret():
    src = "def f(n: int) -> int\n  memo g(i)\n    ret x = i\n  g(n)\n"
    with pytest.raises(MuError):
        transpile(src)


def test_or_if_is_x_or_d_when_x_equals_v():
    out = transpile(
        "def f(t: int, dp: [int]) -> int\n  a = t or -1 if inf\n  dp[t] or -1 if inf\n"
    )
    assert "a = (-1 if t == inf else t)" in out
    assert "return (-1 if (_v := dp[t]) == inf else _v)" in out
    ns = {}
    exec(out, ns)
    assert ns["f"](1, [0, 3]) == 3
    assert ns["f"](1, [0, float("inf")]) == -1
    out = transpile("def g(t: int) -> int\n  t or -1 if inf\n")
    exec(out, ns)
    assert ns["g"](4) == 4
    assert ns["g"](float("inf")) == -1


def test_or_if_needs_an_or_and_keeps_the_ternary():
    assert "x if c else y" in transpile(
        "def f(x: int, c: bool, y: int) -> int\n  x if c else y\n"
    )
    assert "(x or y) if c else 0" in transpile(
        "def f(x: int, c: bool, y: int) -> int\n  (x or y) if c else 0\n"
    ) or "x or y if c else 0" in transpile(
        "def f(x: int, c: bool, y: int) -> int\n  x or y if c else 0\n"
    )
    with pytest.raises(MuError):
        transpile("def f(x: int, c: bool) -> int\n  x if c\n")


def test_as_list_returns_the_yields_of_a_def_as_a_list():
    src = (
        "# 78. Subsets\n"
        "@as_list\n"
        "def subsets(nums: [int]) -> [[int]]\n"
        "  for mask in 0..<(1 << len(nums))\n"
        "    yield [x for >i, x in nums if mask >> i & 1]\n"
    )
    py = transpile(src)
    assert "@as_list\ndef subsets(nums" in py
    assert "def as_list(f):" in py and "from functools import wraps" in py
    ns = {}
    exec(py, ns)
    assert ns["subsets"]([1, 2]) == [[], [1], [2], [1, 2]]
    assert fmt(src) == src
    assert fmt(src.replace("@as_list", "@ as_list")) == src


def test_decorators_stack_and_take_arguments():
    src = (
        "def f(n: int) -> int\n"
        "  def add(k)\n"
        "    def wrap(fn)\n"
        "      h = x -> fn(x) + k\n"
        "      h\n"
        "    wrap\n"
        "  def double(fn)\n"
        "    h = x -> fn(x) * 2\n"
        "    h\n"
        "  @double\n"
        "  @add(1)\n"
        "  def g(x)\n"
        "    x\n"
        "  g(n)\n"
    )
    py = transpile(src)
    assert "    @double\n    @add(1)\n    def g(x):" in py
    ns = {}
    exec(py, ns)
    assert ns["f"](5) == 12  # add runs first, then double
    assert fmt(src) == src


def test_a_decorator_above_a_memo_wraps_the_cached_function():
    src = (
        "def f(n: int) -> int\n"
        "  def double(fn)\n"
        "    h = x -> fn(x) * 2\n"
        "    h\n"
        "  @double\n"
        "  memo g(i) =\n"
        "    | i == 0 -> 1\n"
        "    | else   -> g(i - 1)\n"
        "  @double\n"
        "  memo h(i)\n"
        "    return i\n"
        "  g(n) + h(n)\n"
    )
    py = transpile(src)
    assert py.count("@double\n        @cache\n        def ") == 2
    ns = {}
    exec(py, ns)
    assert ns["f"](2) == 8 + 4
    assert fmt(src) == src


def test_yield_is_a_statement():
    src = (
        "def f(n: int)\n"
        "  for i in 0..<n\n"
        "    if i == 2: yield\n"
        "    yield i, -i\n"
        "  yield from [7]\n"
        "  yield n\n"
    )
    py = transpile(src)
    assert "return yield" not in py  # a last line that yields is not returned
    ns = {}
    exec(py, ns)
    got = list(ns["f"](3))
    assert got == [(0, 0), (1, -1), None, (2, -2), 7, 3]


def test_a_def_that_yields_and_defines_a_memo_runs_on_the_deep_stack():
    src = (
        "@as_list\n"
        "def f(g: [[int]]) -> [int]\n"
        "  memo depth(i) =\n"
        "    | i == 0 -> 0\n"
        "    | else   -> 1 + depth(i - 1)\n"
        "  for (i, j) in cells(g)\n"
        "    yield depth(g[i, j])\n"
    )
    ns = {}
    exec(transpile(src), ns)
    assert ns["f"]([[20000, 1], [2, 3]]) == [20000, 1, 2, 3]


def test_decorator_lines_map_to_their_own_mu_lines():
    src = "@as_list\ndef f(n: int) -> [int]\n  yield n\n"
    code, origin = transpile(src, mapped=True)
    at = {line.strip(): o for line, o in zip(code.split("\n"), origin) if o}
    assert at["@as_list"] == 1
    assert at["def f(n: int) -> list[int]:"] == 2
    assert at["yield n"] == 3


def test_elif_and_else_headers_map_to_their_own_mu_lines():
    src = (
        "def f(c: str) -> int\n"
        "  if c == 'R'\n"
        "    1\n"
        "  elif c == 'L'\n"
        "    2\n"
        "  else\n"
        "    3\n"
    )
    code, origin = transpile(src, mapped=True)
    at = {line.strip(): o for line, o in zip(code.split("\n"), origin) if o}
    assert at["if c == 'R':"] == 2
    assert at["elif c == 'L':"] == 4
    assert at["else:"] == 6
    src = "def f(n: int) -> int\n  for i in range n\n    break\n  else\n    0\n"
    code, origin = transpile(src, mapped=True)
    at = {line.strip(): o for line, o in zip(code.split("\n"), origin) if o}
    assert at["else:"] == 4


def test_a_lambda_may_be_a_dict_value_and_its_body_a_push():
    src = (
        "def f(x: int) -> [int]\n"
        "  stack = []\n"
        "  ops = {\n"
        "    '+': n -> stack <- n,\n"
        "    '-': (s, n) -> s <- -n,\n"
        "    '*': (a, b) -> a * b\n"
        "  }\n"
        "  ops['+'](x)\n"
        "  ops['-'](stack, x)\n"
        "  stack <- ops['*'](x, x)\n"
        "  stack\n"
    )
    ns: dict = {}
    exec(transpile(src), ns)
    assert ns["f"](3) == [3, -3, 9]
    src = "def f(d: [int]) -> int\n  {k: (k, 1) for k in d}\n"
    assert "{k: (k, 1) for k in d}" in transpile(src)


def test_a_lambda_may_name_no_argument():
    """`-> e` reads one argument as `_`, several as the tuple `_`."""
    src = (
        "def f(xs: [int]) -> [int]\n"
        "  stack = []\n"
        "  ops = {'+': -> _[0] <- _[1], '2': -> _ * 2}\n"
        "  ops['+'](stack, ops['2'](xs[0]))\n"
        "  double = -> _ * 2\n"
        "  stack <- double(xs[1])\n"
        "  stack + sorted(xs, key=-> -_)\n"
    )
    ns: dict = {}
    exec(transpile(src), ns)
    assert ns["f"]([1, 5]) == [2, 10, 5, 1]
    assert fmt(src) == src


def test_assert_compares_values_it_saw_once():
    """`assert a == b` evaluates each side once and names both values on a
    fail, so a side with side effects is not run a second time."""
    out = transpile(
        "def f(x: int) -> int\n  x + 1\n\nassert f(1) == 2\nassert f(1) is true\n"
    )
    assert "_l, _r = f(1), 2\n" in out
    assert f"assert _l == _r, {SIDES}\n" in out
    assert f'assert _l is _r, {SIDES} + "\\nop   : is"\n' in out
    src = "calls = []\n\ndef g() -> bool\n  calls <- 1\n  len(calls) == 1\n\nassert g() == false\n"
    ns = {}
    try:
        exec(transpile(src), ns)
    except AssertionError as err:
        assert str(err) == "left : True\nright: False"
    assert len(ns["calls"]) == 1
    # a chained comparison, a message and a boolean expression keep the plain form
    out = transpile(
        "assert 1 < f(1) < 3\nassert f(1) == 2, 'msg'\nassert f(1) == 2 or f(2) == 3\n"
    )
    assert "assert 1 < f(1) < 3\n" in out
    assert "assert f(1) == 2, 'msg'\n" in out
    assert "assert f(1) == 2 or f(2) == 3\n" in out


def test_classes():
    src = (
        "from collections import OrderedDict\n\n"
        "class LRUCache\n"
        "  def __init__(capacity: int)\n"
        "    self.d = OrderedDict()\n"
        "    self.cap = capacity\n\n"
        "  def get(key: int) -> int\n"
        "    if key not in self.d\n"
        "      return -1\n"
        "    self.d.move_to_end(key)\n"
        "    self.d[key]\n\n"
        "  def put(key: int, value: int) -> none\n"
        "    self.d[key] = value\n"
        "    self.d.move_to_end(key)\n"
        "    if len(self.d) > self.cap\n"
        "      self.d.popitem(last=false)\n\n"
        "def twice(x: int) -> int\n"
        "  def inner(y)\n"
        "    y * 2\n"
        "  inner(x)\n\n"
        "class Solution extends LRUCache\n"
        "  def peek() -> int\n"
        "    self.cap\n\n"
        "c = LRUCache(2)\n"
        "c.put(1, 1)\n"
        "c.put(2, 2)\n"
        "c.put(3, 3)\n"
    )
    out = transpile(src)
    assert "class LRUCache:\n    def __init__(self, capacity: int):\n" in out
    assert "    def get(self, key: int) -> int:\n" in out
    assert "    def put(self, key: int, value: int) -> None:\n" in out
    assert (
        "\ndef twice(x: int) -> int:\n    def inner(y):\n        return y * 2\n" in out
    )
    assert "class Solution(LRUCache):\n    def peek(self) -> int:\n" in out
    # __init__ and a def typed -> none return nothing; the others their last line
    assert (
        "return self.cap = capacity" not in out
        and "        return self.d[key]\n" in out
    )
    assert "return self.d.popitem" not in out
    ns = {}
    exec(out, ns)
    assert ns["c"].get(1) == -1 and ns["c"].get(3) == 3 and ns["twice"](4) == 8
    assert ns["Solution"](5).peek() == 5
    # a decorator goes above a class too
    out = transpile("@dataclass\nclass P\n  x = 0\n")
    assert out.endswith("@dataclass\nclass P:\n    x = 0\n")
    # the 0.7 form is refused with a pointer to the class line
    with pytest.raises(MuError, match="extends goes on the class line"):
        transpile("extends Base\ndef f() -> int\n  1\n")
    with pytest.raises(MuError, match="a class holds no class"):
        transpile("class A\n  class B\n    pass\n")


def test_fmt_sets_the_methods_of_a_class_apart():
    src = "class A\n  def f()\n    1\n  def g()\n    2\n  x = 3\n\n\n\nclass B\n  pass\ndef h()\n  4\n"
    want = "class A\n  def f()\n    1\n\n  def g()\n    2\n  x = 3\n\nclass B\n  pass\n\ndef h()\n  4\n"
    assert fmt(src) == want
    assert fmt(want) == want


def test_c_style_conditional():
    def one(body):
        return transpile(f"def f(x: int, y: int) -> int\n  {body}\n")

    assert "return x if x > 0 else -x" in one("x > 0 ? x : -x")
    assert "return 1 if x else 2 if y else 3" in one("x ? 1 : y ? 2 : 3")
    assert "return (1 if y else 2) if x else 3" in one("x ? (y ? 1 : 2) : 3")
    assert "return (1 if y else 2) if x else 3" in one("x ? y ? 1 : 2 : 3")
    assert "{1: 2 if x else 3}" in one("{1: x ? 2 : 3}")
    assert "sum([x if x > 0 else -x for x in [x, y]])" in one(
        "sum for x in [x, y]: x > 0 ? x : -x"
    )
    assert "[1][0 if x else 1]" in one("[1][x ? 0 : 1]")
    src = "def f(x: int) -> int\n  count for x in [x] if x ? 1 : 0: 2\n"
    with pytest.raises(MuError, match="no `\\? :` in a fold's if"):
        transpile(src)
    assert run("def f(x: int) -> str\n  x > 0 ? 'R' : x < 0 ? 'L' : '.'\n", -2) == "L"


def test_the_formatter_spaces_a_conditional_and_keeps_a_type_tight():
    assert fmt("y = x>0?'R':x<0?'L':'.'\n") == "y = x > 0 ? 'R' : x < 0 ? 'L' : '.'\n"
    assert fmt("y = x ? -1 : (1)\n") == "y = x ? -1 : (1)\n"
    assert fmt("def f(a: int?, b: [int]?) -> str?\n  a\n") == (
        "def f(a: int?, b: [int]?) -> str?\n  a\n"
    )
    assert fmt("d = {1: x ? 2 : 3}\n") == "d = {1: x ? 2 : 3}\n"
    assert fmt("if x ? a : b: k += 1\n") == "if x ? a : b\n  k += 1\n"
    assert (
        fmt("y = sum for x in xs: x>0?x:-x\n")
        == "y = sum for x in xs: x > 0 ? x : -x\n"
    )


def test_with_try_and_raise_are_blocks_without_colons():
    src = (
        "def f(path: str) -> str\n"
        "  try\n"
        "    with open(path) as fh\n"
        "      text = fh.read()\n"
        "  except OSError as e\n"
        "    raise ValueError(path) from e\n"
        "  else\n"
        "    text.upper()\n"
        "  finally\n"
        "    log <- path\n"
    )
    out = transpile(src)
    assert "    with open(path) as fh:\n" in out
    assert "    except OSError as e:\n        raise ValueError(path) from e\n" in out
    assert "    else:\n        return text.upper()\n    finally:\n" in out
    ns = {"log": []}
    exec(out, ns)
    assert ns["f"](__file__).startswith('"""THE LANGUAGE')
    with pytest.raises(ValueError):
        ns["f"]("/no/such/file")
    assert ns["log"] == [__file__, "/no/such/file"]
    assert fmt(src) == src
    with pytest.raises(MuError, match="needs an except or a finally"):
        transpile("def f()\n  try\n    1\n  g()\n")


def test_a_def_takes_star_args_keyword_only_and_star_star_kwargs():
    src = (
        "def f(a, *rest: int, k=1, **kw) -> int\n"
        "  a + sum(rest) * k + len(kw)\n"
        "\n"
        "def g(*, n)\n"
        "  f(*[1, 2], **{'k': n, 'z': 0})\n"
    )
    out = transpile(src)
    assert "def f(a, *rest: int, k=1, **kw) -> int:" in out
    assert "def g(*, n):" in out
    ns = {}
    exec(out, ns)
    assert ns["g"](n=10) == 1 + 2 * 10 + 1
    assert fmt(src) == src


def test_python_operators_and_literals_for_numeric_code():
    src = (
        "def f(x, y)\n"
        "  global seen\n"
        "  seen = 0xff + 0b1 + 0o7\n"
        "  first, *rest = x @ y\n"
        "  if (n := len(rest)) > 0\n"
        "    return n\n"
        "  rest\n"
    )
    out = transpile(src)
    assert "first, *rest = x @ y" in out and "if (n := len(rest)) > 0:" in out

    class M:
        def __matmul__(self, other):
            return [1, 2, 3]

    ns = {}
    exec(out, ns)
    assert ns["f"](M(), None) == 2 and ns["seen"] == 263
    assert fmt(src) == src


def test_subscripts_take_ellipsis_and_several_slices():
    out = transpile("def f(x)\n  x[..., :2] = 0\n  x[:, none, -1]\n")
    assert "x[..., :2] = 0" in out and "return x[:, None, -1]" in out
    assert fmt("y = x[...,:2]\n") == "y = x[..., :2]\n"


def test_typed_assignments_and_dataclass_fields():
    src = (
        "from dataclasses import dataclass\n"
        "\n"
        "@dataclass\n"
        "class Config\n"
        "  n_layer: int = 12\n"
        "  name: str? = none\n"
        "  sizes: [int]\n"
        "\n"
        "class Net extends Config, object\n"
        '  def depth() -> "Net"\n'
        "    self\n"
    )
    out = transpile(src)
    assert "    n_layer: int = 12\n    name: Optional[str] = None\n" in out
    assert "class Net(Config, object):" in out
    ns = {}
    exec(out.replace("    sizes: list[int]\n", ""), ns)
    assert ns["Net"](name="x").depth().n_layer == 12


def test_adjacent_strings_join_and_a_dict_unpacks():
    out = transpile("def f(d)\n  {**d, 'k': 'a' 'b'}\n")
    assert "return Rec({**d, 'k': 'a' 'b'})" in out


def test_a_staticmethod_takes_no_self_and_a_classmethod_takes_cls():
    src = (
        "class C\n"
        "  @staticmethod\n"
        "  def make(n)\n"
        "    n * 2\n"
        "\n"
        "  @classmethod\n"
        "  def build(n)\n"
        "    cls.make(n)\n"
    )
    out = transpile(src)
    assert "def make(n):" in out and "def build(cls, n):" in out
    ns = {}
    exec(out, ns)
    assert ns["C"].build(4) == 8
    assert fmt(src) == src


def test_one_line_defs_in_a_row_stay_together():
    src = (
        "class V\n"
        "  def __neg__(): self * -1\n"
        "  def __radd__(other): self + other\n"
        "\n"
        "  def backward() -> none\n"
        "    pass\n"
    )
    assert fmt(src) == src


def test_fmt_keeps_the_line_breaks_of_a_long_or_commented_bracket():
    src = (
        "config_args = {\n"
        "  'gpt2': dict(n_layer=12, n_head=12, n_embd=768),  # 124M params\n"
        "  'gpt2-medium': dict(n_layer=24, n_head=16, n_embd=1024),\n"
        "}[model_type]\n"
    )
    assert fmt(src) == src
    assert "'gpt2-medium'" in transpile(src)
    messy = src.replace("  'gpt2-m", "        'gpt2-m").replace("}[", "  }[")
    assert fmt(messy) == src
    # short and without a comment, it is joined as before
    assert fmt("x = [1,\n  2]\n") == "x = [1, 2]\n"


def test_a_module_docstring_stays_first_and_a_string_method_drops_brackets():
    src = '"""Doc."""\n\nd = {:list}\nx = ", ".join map(str, [1, 2])\n'
    out = transpile(src)
    assert out.startswith('"""Doc."""\n')
    ns = {}
    exec(out, ns)
    assert ns["__doc__"] == "Doc." and ns["x"] == "1, 2"


def test_a_generator_returns_nothing_after_its_last_line():
    out = transpile("def f()\n  yield 1\n  g()\n")
    assert "return g()" not in out and "    g()" in out


def test_star_star_keeps_its_place_among_named_arguments():
    out = transpile("x = f(1, a=2, **kw, b=3)\n")
    assert "f(1, a=2, **kw, b=3)" in out


def test_a_lambda_may_be_a_branch_of_a_conditional():
    out = transpile("def f(c)\n  g = c ? x -> x + 1 : () -> none\n  g\n")
    assert "g = (lambda x: x + 1) if c else lambda: None" in out
    ns = {}
    exec(out, ns)
    assert ns["f"](True)(1) == 2 and ns["f"](False)() is None


def test_a_type_may_be_a_python_generic():
    out = transpile("from typing import Optional\nx: Optional[str] = none\ny: dict[str, [int]] = {}\n")
    assert "x: Optional[str] = None" in out
    assert "y: dict[str, list[int]] = Rec()" in out


def test_async_def_awaits():
    """`async def` is a coroutine; `await` waits inside it, and the last
    line is returned."""
    src = (
        "import asyncio\n\nasync def get(k: str) -> str\n  await asyncio.sleep(0)\n  k * 2\n\n"
        "async def main() -> [str]\n  await asyncio.gather(*[get(k) for k in 'ab'])\n\n"
        "assert asyncio.run(main()) == ['aa', 'bb']\n"
    )
    out = transpile(src)
    assert "async def get(k: str) -> str:" in out
    assert "return await asyncio.gather(" in out
    exec(out, {})
    out = transpile("class Solution\n  @staticmethod\n  async def f() -> int\n    1\n")
    assert "    @staticmethod\n    async def f() -> int:" in out


def test_mu_file_runs_the_file(tmp_path):
    """`mu FILE ARG...` runs the file with ARGs in sys.argv, and a traceback
    shows the mu line, not the Python one; `mu --py FILE` prints the Python."""
    import sys

    f = tmp_path / "t.mu"
    f.write_text(
        "import sys\n\ndef f(n: int) -> int\n  n * 2\n\n"
        "print(f(21), sys.argv[1:])\nx = [1]\nprint x[3]\n"
    )
    mu = [sys.executable, "-m", "mulang"]
    run = subprocess.run([*mu, str(f), "a"], capture_output=True, text=True)
    assert run.returncode == 1
    assert run.stdout == "42 ['a']\n"
    assert f'File "{f}", line 8, in <module>\n    print x[3]\n' in run.stderr
    assert "mulang" not in run.stderr
    py = subprocess.run([*mu, "--py", str(f)], capture_output=True, text=True)
    assert "print(x[3])" in py.stdout
    stdin = subprocess.run([*mu, "-"], input="xs = [1, 2]\nprint len xs\n", capture_output=True, text=True)
    assert stdin.stdout == "2\n"


def test_a_mu_module_is_imported_by_its_defs(tmp_path):
    """`from lib.nums import evens` finds lib/nums.mu next to the file and
    pastes the defs the file calls, with what they call and import; the
    module's own tests under its defs are not part of it."""
    import sys

    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "nums.mu").write_text(
        "from math import gcd\n\nLIMIT = 10\n\n"
        "def even(n: int) -> bool\n  n % 2 == 0\n\n"
        "def evens(xs: [int]) -> [int]\n  [x for x in xs if even(x)]\n\n"
        "def lcm(a: int, b: int) -> int\n  a * b // gcd(a, b)\n\n"
        "def big(n: int) -> bool\n  n >= LIMIT\n\n"
        "assert evens([1, 2]) == [2]\nprint 'module tests ran'\n"
    )
    main = tmp_path / "main.mu"
    main.write_text("from lib.nums import evens, big\n\nprint evens([1, 2, 3, 4])\nprint big(11)\n")
    out = transpile(main.read_text(), path=main)
    assert "from lib.nums" not in out and "def even(n):" in out and "LIMIT = 10" in out
    assert "def lcm" not in out and "gcd" not in out and "module tests" not in out
    run = subprocess.run(
        [sys.executable, "-m", "mulang", str(main)], capture_output=True, text=True, cwd="/"
    )
    assert run.stdout == "[2, 4]\nTrue\n", run.stderr


def test_a_mu_import_names_what_it_takes(tmp_path):
    (tmp_path / "m.mu").write_text("def f() -> int\n  1\n")
    (tmp_path / "a.mu").write_text("from b import g\n\ndef f() -> int\n  g()\n")
    (tmp_path / "b.mu").write_text("from a import f\n\ndef g() -> int\n  f()\n")
    here = tmp_path / "x.mu"
    for src, said in [
        ("from m import nope\n", "m.mu defines no nope"),
        ("import m\n", "write `from m import`"),
        ("from m import f as g\n", "no `*` or `as`"),
        ("from a import f\n", "an import cycle: a.mu -> b.mu -> a.mu"),
    ]:
        with pytest.raises(MuError, match=re.escape(said)):
            transpile(src, path=here)
    # a Python module is imported as before
    assert "from collections import deque" in transpile("from collections import deque\n", path=here)


def test_a_union_type():
    out = transpile("def f(x: int | str) -> int | none\n  none\n")
    assert "def f(x: int | str) -> int | None:" in out


def test_a_relative_import_stays_python_and_binds_its_names():
    """`from .grid import table` is a package's own import: it is written
    as Python, and `table` is never pasted from the library over it."""
    out = transpile("from .grid import table\nfrom ..a.b import c\n\ndef f() -> int\n  table(1)\n")
    assert "from .grid import table\n" in out and "from ..a.b import c\n" in out
    out = transpile("from math import factorial\n\ndef f() -> int\n  factorial(3)\n")
    assert "def factorial" not in out


def test_a_from_line_without_import_is_a_mu_error():
    for src in ("x = 1\nfrom operator gt, lt\n", "from operator import\n"):
        with pytest.raises(MuError, match="expected from module import names"):
            transpile(src)
    with pytest.raises(MuError, match="^line 2:"):
        transpile("x = 1\nfrom operator gt, lt\n")


def test_python_generics_take_ellipsis_and_type_lists():
    """`Tuple[Cell, ...]` and `Callable[[Any], bool]` pass through as Python
    wrote them; an `A -> B` type in a file that imports Callable adds no
    second import."""
    src = (
        "from typing import Any, Callable, Tuple\n\n"
        "X: Tuple[int, ...] = (1,)\n\n"
        "def f(ok: Callable[[Any], bool], g: int -> bool) -> Tuple[int, ...]\n  X\n"
    )
    out = transpile(src)
    assert "X: Tuple[int, ...] = (1,)" in out
    assert "def f(ok: Callable[[Any], bool], g: Callable[[int], bool]) -> Tuple[int, ...]:" in out
    assert out.count("from typing import") == 1


def test_a_starred_item_in_a_bracketed_tuple():
    out = transpile("def f(e: [int]) -> (int, int)\n  (*e, 1) + (1, *e)\n")
    assert "return (*e, 1) + (1, *e)" in out


def test_one_scalar_literal_fills_every_name():
    out = transpile("a, b, c = 0\nlo, hi = -inf\nok, done = false\n")
    assert "a, b, c = 0, 0, 0\n" in out
    assert "lo, hi = -inf, -inf\n" in out
    assert "ok, done = False, False\n" in out
    out = transpile("a, b = 'xy'\nc, d = pair\n")
    assert "a, b = 'xy'\n" in out and "c, d = pair\n" in out


def test_dots_repeat_an_item_one_copy_per_dot():
    src = "x = 1\ny = 2\np, q, r, s, t = 0..., 1..\na, b, c, d = 2x.., 2y..\nres, path, used = []...\n"
    out = transpile(src)
    assert "p, q, r, s, t = 0, 0, 0, 1, 1\n" in out
    assert "a, b, c, d = (2 * x), (2 * x), (2 * y), (2 * y)\n" in out
    ns = {}
    exec(out, ns)
    assert ns["res"] == [] and ns["res"] is not ns["path"]
    assert "a, b, c, d, e = 0, 0, 0, 0, 1\n" in transpile("a, b, c, d, e = 0...., 1\n")


def test_a_ret_line_repeats():
    out = transpile("def f() -> [int]\n  ret res, seen = []..\n  res <- 1\n")
    assert "res, seen = [], []\n" in out and "return res\n" in out


def test_a_repeat_must_fill_every_name_once():
    for src, msg in [
        ("a, b = 0..., 1\n", "2 names, 4 values"),
        ("def f()\n  return 0..\n", "only on the right of `=`"),
        ("a = b = 0..\n", "chained"),
        ("*a, b = 0..\n", "starred"),
    ]:
        with pytest.raises(MuError, match=msg):
            transpile(src)
    assert "range(0, n + 1)" in transpile("xs = 0..n\n")


def test_a_number_before_a_name_multiplies():
    out = transpile(
        "n = 5\nk = 2\na = 1 + 8n\nb = k * (2n + k - 1) // 2\nc = 2(n + 1)\n"
        "d = 1/2n\ne1 = 2n**2\nf = 2xs[1] if (xs := [1, 3]) else 0\ng = 1if n else 0\n"
    )
    ns = {}
    exec(out, ns)
    assert (ns["a"], ns["b"], ns["c"], ns["d"], ns["e1"], ns["g"]) == (41, 11, 12, 0.1, 50, 1)
    assert "a = 1 + (8 * n)\n" in out


def test_a_number_before_a_name_refuses_a_space_and_number_letters():
    for src, msg in [
        ("n = 1\nx = 2 n\n", "write 2n, with no space, or 2 \\* n"),
        ("e = 1\nx = 2e\n", "exponent"),
        ("x = 0\ny = 0x\n", "hex"),
    ]:
        with pytest.raises(MuError, match=msg):
            transpile(src)


def test_fmt_keeps_a_number_on_its_name_and_dots_on_their_item():
    src = "res, path = []..,  [0]\nx = 2x**2 + 3(a+b)\nz = 1 if c else ...\n"
    assert fmt(src) == "res, path = [].., [0]\nx = 2x ** 2 + 3(a + b)\nz = 1 if c else ...\n"


ISLANDS = (
    "class Solution\n"
    "  def countIslands(grid: [[int]], k: int) -> int\n"
    "    sum for p in cells(grid, gt=0): {p ->\n"
    "      total, grid[p] = grid[p], -1\n"
    "      total + sum for q in nbrs(grid, p, gt=0): mu(q)\n"
    "    }(p) % k == 0\n"
)


def test_a_block_lambda_in_a_one_line_fold_is_one_def_before_the_statement():
    out = transpile(ISLANDS)
    fold = "return sum([_mu1(p) % k == 0 for p in cells(grid, gt=0)])"
    assert out.count("def _mu1(p):") == 1 and out.index("def _mu1") < out.index(fold)
    ns: dict = {}
    exec(out, ns)
    grid = [[0, 2, 1, 0, 0], [0, 5, 0, 0, 5], [0, 0, 1, 0, 0], [0, 1, 4, 7, 0], [0, 2, 0, 0, 8]]
    assert ns["Solution"]().countIslands(grid, 5) == 2
    assert fmt(ISLANDS) == ISLANDS


def test_a_one_line_block_lambda_recurses_as_mu():
    src = "def fib(n: int) -> int\n  {k -> k < 2 ? k : mu(k - 1) + mu(k - 2)}(n)\n"
    out = transpile(src)
    assert "lambda" not in out and "def _mu1(k):" in out
    ns: dict = {}
    exec(out, ns)
    assert ns["fib"](10) == 55
    assert fmt(src) == src


def test_a_block_lambda_that_assigns_an_outer_name_gets_nonlocal():
    src = (
        "def total(xs: [int]) -> int\n"
        "  ret acc = 0\n"
        "  {i ->\n"
        "    if i < len xs\n"
        "      acc += xs[i]\n"
        "      mu(i + 1)\n"
        "  }(0)\n"
    )
    out = transpile(src)
    assert "    def _mu1(i):\n        nonlocal acc\n" in out
    ns: dict = {}
    exec(out, ns)
    assert ns["total"]([1, 2, 3]) == 6


def test_a_block_lambda_in_a_fold_block_is_built_once_per_item():
    src = (
        "def reach(G: {int: [int]}) -> int\n"
        "  max for i in G\n"
        "    seen = set()\n"
        "    {j ->\n"
        "      seen.add(j)\n"
        "      1 + sum for k in G[j] if k not in seen: mu(k)\n"
        "    }(i)\n"
    )
    out = transpile(src)
    assert "    for i in G:\n        seen = set()\n        def _mu1(j):\n" in out
    ns: dict = {}
    exec(out, ns)
    assert ns["reach"]({0: [1], 1: [2], 2: []}) == 3
    assert fmt(src) == src


@pytest.mark.parametrize(
    "src, message",
    [
        (
            "def f(xs: [int]) -> int\n  sum for x in xs: {y -> x + y}(1)\n",
            "line 2: the mu block reads x from the loop around it; take x as a parameter",
        ),
        ("def f(xs: [int]) -> [int]\n  [{y -> x + y}(1) for x in xs]\n", "reads x from the loop"),
        ("def f(x: int) -> int\n  mu(x)\n", "line 2: mu names the block lambda around it"),
        (
            "def f(n: int) -> int\n  {k ->\n    | k < 2 -> k\n    x = 1\n  }(n)\n",
            "line 4: a case list is the whole body of the block",
        ),
    ],
)
def test_block_lambda_errors(src, message):
    with pytest.raises(MuError, match=re.escape(message)):
        transpile(src)


def test_memo_before_a_block_lambda_caches_its_def_on_the_deep_stack():
    src = (
        "def longest(G: {int: [int]}) -> int\n"
        "  memo {\n"
        "    n ->\n"
        "      max from 0 for v in G[n]: 1 + mu(v)\n"
        "  }(0)\n"
    )
    out = transpile(src)
    assert "    def run():\n        @cache\n        def _mu1(n):\n" in out
    assert "    return deep(run)\n" in out
    ns: dict = {}
    exec(out, ns)
    chain = {i: [i + 1] for i in range(20000)} | {20000: []}
    assert ns["longest"](chain) == 20000
    assert fmt(src) == src


def test_a_block_lambda_may_take_a_case_list_as_its_body():
    src = (
        "def longest(G: {int: [int]}) -> int\n"
        "  memo {n ->\n"
        "    | not G[n] -> 0\n"
        "    | else     -> max for v in G[n]: 1 + mu(v)\n"
        "  }(0)\n"
    )
    out = transpile(src)
    assert "def _mu1(n):\n            if not G[n]:\n                return 0\n" in out
    assert "            return max([1 + _mu1(v) for v in G[n]])\n" in out
    ns: dict = {}
    exec(out, ns)
    assert ns["longest"]({0: [1, 2], 1: [2], 2: []}) == 2
    assert fmt(src) == src
    plain = (
        "def fib(n: int) -> int\n"
        "  {k ->\n"
        "    | k < 2 -> k\n"
        "    | else  -> mu(k - 1) + mu(k - 2)\n"
        "  }(n)\n"
    )
    ns = {}
    exec(transpile(plain), ns)
    assert ns["fib"](20) == 6765
    assert fmt(plain) == plain


def test_memo_before_a_one_line_block_lambda():
    src = "def fib(n: int) -> int\n  memo {k -> k < 2 ? k : mu(k - 1) + mu(k - 2)}(n)\n"
    out = transpile(src)
    assert "@cache\n        def _mu1(k):" in out
    ns: dict = {}
    exec(out, ns)
    assert ns["fib"](90) == 2880067194370816120
    assert fmt(src) == src


def test_memo_before_anything_but_a_name_or_a_block_lambda_is_refused():
    src = "def f(n: int) -> int\n  memo (k -> k)(n)\n"
    message = "line 2: memo goes before a name or a block `{n ->`"
    with pytest.raises(MuError, match=re.escape(message)):
        transpile(src)


def test_a_first_block_is_a_def_passed_as_the_key():
    src = (
        "def shipWithinDays(weights: [int], days: int) -> int\n"
        "  first cap in max weights..sum weights\n"
        "    used, total = 1, 0\n"
        "    for w in weights\n"
        "      if total + w > cap\n"
        "        used, total = used + 1, 0\n"
        "      total += w\n"
        "    used <= days\n"
    )
    out = transpile(src)
    assert "def _first1(cap):" in out and "key=_first1)" in out
    ns: dict = {}
    exec(out, ns)
    assert ns["shipWithinDays"]([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 5) == 15
    assert fmt(src) == src


CLOSED = (
    "class Solution\n"
    "  def closedIsland(grid: [[int]]) -> int\n"
    "    res = count for c in cells(grid, eq=0)\n"
    "      {\n"
    "        (c) ->\n"
    "          grid[c] = 1\n"
    "          closed = not is_edge(grid, *c)\n"
    "          for n in nbrs(grid, c, eq=0)\n"
    "            closed = mu(n) and closed\n"
    "          closed\n"
    "      }(c)\n"
    "    res\n"
)


def test_a_brace_alone_opens_a_block_and_an_assigned_fold_block_is_lifted():
    """The head may sit under the `{`; the fold block is the value of `=`;
    the lambda reads nothing the block binds, so it is built before the loop."""
    out = transpile(CLOSED)
    assert out.index("def _mu1(c):") < out.index("acc = 0") < out.index("res = acc")
    ns: dict = {}
    exec(out, ns)
    grid = [[1, 1, 1, 1, 1, 1, 1, 0], [1, 0, 0, 0, 0, 1, 1, 0], [1, 0, 1, 0, 1, 1, 1, 0], [1, 0, 0, 0, 0, 1, 0, 1], [1, 1, 1, 1, 1, 1, 1, 0]]
    assert ns["Solution"]().closedIsland(grid) == 2
    assert fmt(CLOSED) == CLOSED


def test_a_block_lambda_reading_a_name_the_fold_block_binds_stays_in_the_loop():
    src = (
        "def reach(G: {int: [int]}) -> int\n"
        "  ret best = max for i in G\n"
        "    seen = set()\n"
        "    {j ->\n"
        "      seen.add(j)\n"
        "      1 + sum for k in G[j] if k not in seen: mu(k)\n"
        "    }(i)\n"
    )
    out = transpile(src)
    assert "        seen = set()\n        def _mu1(j):\n" in out and "best = acc" in out
    ns: dict = {}
    exec(out, ns)
    assert ns["reach"]({0: [1], 1: [2], 2: []}) == 3


def test_by_indexes_a_list_a_dict_or_a_string():
    """`by=` on max, min and sort is Python's key=. A lambda or a known
    function is the key as it is; a parameter or a local goes through
    `_by`, which at run time indexes anything that is not a function."""
    src = (
        "def f(a: int, b: int, values: str) -> int\n"
        "  max(a, b, by=values)\n"
        "def g(xs: [int], d: {int: int}) -> int\n"
        "  min(xs, by=d)\n"
        "def h(xs: [int], w: [int]) -> [int]\n"
        "  sort(xs, by=w)\n"
        "def k(xs: [int]) -> [int]\n"
        "  sort(xs, by=x -> -x)\n"
        "def m(ws: [str]) -> [str]\n"
        "  sort(ws, by=len)\n"
        "def p(ds: [int]) -> [int]\n"
        "  sort(ds, by=even)\n"
    )
    out = transpile(src)
    assert "max(a, b, key=_by(values))" in out
    assert "min(xs, key=_by(d))" in out
    assert "sorted(xs, key=_by(w))" in out
    assert "sorted(xs, key=lambda x: -x)" in out
    assert "sorted(ws, key=len)" in out
    assert "sorted(ds, key=even)" in out and "def even(n):" in out
    assert out.count("def _by(key):") == 1
    ns = {}
    exec(out, ns)
    assert ns["f"](0, 1, "BA") == 0 and ns["f"](0, 1, "AB") == 1
    assert ns["g"]([0, 1, 2], {0: 5, 1: 2, 2: 7}) == 1
    assert ns["h"]([0, 1, 2], [5, 2, 7]) == [1, 0, 2]
    assert ns["k"]([1, 3, 2]) == [3, 2, 1]
    assert ns["m"](["ccc", "a", "bb"]) == ["a", "bb", "ccc"]
    assert ns["p"]([3, 2, 1]) == [3, 1, 2]  # even is False then True


def test_by_on_a_local_function_still_calls_it():
    """A local def passed as `by=` goes through `_by` too and is called."""
    src = (
        "def f(xs: [int]) -> [int]\n"
        "  def neg(x)\n"
        "    -x\n"
        "  sort(xs, by=neg)\n"
    )
    out = transpile(src)
    assert "sorted(xs, key=_by(neg))" in out
    ns = {}
    exec(out, ns)
    assert ns["f"]([1, 3, 2]) == [3, 2, 1]
