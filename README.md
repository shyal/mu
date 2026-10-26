# mu

Documentation: [www.shyal.com/mu](https://www.shyal.com/mu/)

mu is a small language that transpiles to Python. A file is indented
blocks without colons, calls of one argument without brackets, `..`
ranges, `x -> ...` lambdas, `<-` push, folds (`sum for x in xs: f(x)`),
`memo` for a cached function with cases, and `class` for a Python class.
The Python it produces is plain and runs anywhere; the transpiler pastes
in the source of every helper the file calls.

```
pip install mu-language
mu file.mu        # run the file
mu --py file.mu   # the Python
mu --fmt file.mu  # the file, formatted
mu-repl           # a REPL
```

## Solves

Each solve has three tabs: mu, mu + pydsa and python. The first two are
complete mu files. The mu + pydsa version calls
[pydsa](https://www.shyal.com/pydsa/) helpers by name, with no import. The
profile in [Profiles](#profiles) registers them. The python tab gives the
same solve in plain Python, for comparison. In both mu versions, the
Python that `mu --py file.mu` prints
imports only the standard library, because mu pastes in the source of each
helper that the file calls.

```mu
# 53. Maximum Subarray (Kadane's algorithm)
class Solution
  def maxSubArray(nums: [int]) -> int
    max_diff(scan(+, [0] + nums))[-1]
```

<details><summary>mu</summary>

```mu
# 53. Maximum Subarray (Kadane's algorithm)
class Solution
  def maxSubArray(nums: [int]) -> int
    ret best = cur = -inf
    for x in nums
      best = max(best, cur') = max(x, cur + x)
```

</details>

<details><summary>python</summary>

```python
# 53. Maximum Subarray (Kadane's algorithm)
class Solution:
    def maxSubArray(self, nums: list[int]) -> int:
        best = cur = -float("inf")
        for x in nums:
            cur = max(x, cur + x)
            best = max(best, cur)
        return best
```

</details>

```mu
# 121. Best Time to Buy and Sell Stock (running minimum)
class Solution
  def maxProfit(prices: [int]) -> int
    max([0, *max_diff(prices)])
```

<details><summary>mu</summary>

```mu
# 121. Best Time to Buy and Sell Stock (running minimum)
class Solution
  def maxProfit(prices: [int]) -> int
    ret best, low = 0, inf
    for p in prices
      best = max(best, p - low') = min(low, p)
```

</details>

<details><summary>python</summary>

```python
# 121. Best Time to Buy and Sell Stock (running minimum)
class Solution:
    def maxProfit(self, prices: list[int]) -> int:
        low, best = float("inf"), 0
        for p in prices:
            low = min(low, p)
            best = max(best, p - low)
        return best
```

</details>

```mu
# 130. Surrounded Regions (connected components)
class Solution
  def solve(board: [[str]])
    for c in components(cells(board, eq='O'), p -> nbrs(board, p, eq='O'))
      if not any(is_edge(board, i, j) for i, j in c)
        put(board, c, 'X')
```

<details><summary>mu</summary>

```mu
# 130. Surrounded Regions (connected components)
class Solution
  def solve(board: [[str]])
    m, n = len(board), len(board[0])
    st = [(i, j) for i in 0..<m for j in 0..<n if i in (0, m - 1) or j in (0, n - 1)]
    while st
      i, j = st .
      if 0 <= i < m and 0 <= j < n and board[i][j] == 'O'
        board[i][j] = 'S'
        st += [(i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)]
    for row in board
      row[:] = [c == 'S' ? 'O' : 'X' for c in row]
```

</details>

<details><summary>python</summary>

```python
# 130. Surrounded Regions (connected components)
class Solution:
    def solve(self, board: list[list[str]]) -> None:
        m, n = len(board), len(board[0])
        st = [
            (i, j)
            for i in range(m)
            for j in range(n)
            if i in (0, m - 1) or j in (0, n - 1)
        ]
        while st:
            i, j = st.pop()
            if 0 <= i < m and 0 <= j < n and board[i][j] == "O":
                board[i][j] = "S"
                st += [(i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)]
        for row in board:
            row[:] = ["O" if c == "S" else "X" for c in row]
```

</details>

```mu
# 200. Number of Islands (connected components)
class Solution
  def numIslands(grid: [[str]]) -> int
    len components(cells(grid, eq='1'), p -> nbrs(grid, p, eq='1'))
```

<details><summary>mu</summary>

```mu
# 200. Number of Islands (connected components)
from itertools import product

class Solution
  def numIslands(grid: [[str]]) -> int
    m, n = len(grid), len(grid[0])
    sum for p in product(0..<m, 0..<n) if grid[p] == '1'
      st = [p]
      while st
        i, j = st .
        if 0 <= i < m and 0 <= j < n and grid[i][j] == '1'
          grid[i][j] = '0'
          st += [(i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)]
      1
```

</details>

<details><summary>python</summary>

```python
# 200. Number of Islands (connected components)
class Solution:
    def numIslands(self, grid: list[list[str]]) -> int:
        m, n, islands = len(grid), len(grid[0]), 0
        for r in range(m):
            for c in range(n):
                if grid[r][c] == "1":
                    islands += 1
                    stack = [(r, c)]
                    while stack:
                        i, j = stack.pop()
                        if 0 <= i < m and 0 <= j < n and grid[i][j] == "1":
                            grid[i][j] = "0"
                            stack += [(i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)]
        return islands
```

</details>

```mu
# 410. Split Array Largest Sum (binary search on the answer)
class Solution
  def splitArray(nums: [int], k: int) -> int
    first_true(max(nums), sum(nums), cap -> min_chunks(nums, cap) <= k)
```

<details><summary>mu</summary>

```mu
# 410. Split Array Largest Sum (binary search on the answer)
class Solution
  def splitArray(nums: [int], k: int) -> int
    def chunks(cap)
      n, total = 1, 0
      for x in nums
        n, total = total + x > cap ? (n + 1, x) : (n, total + x)
      n
    first cap in max(nums)..sum(nums) if chunks(cap) <= k
```

</details>

<details><summary>python</summary>

```python
# 410. Split Array Largest Sum (binary search on the answer)
from bisect import bisect_left


class Solution:
    def splitArray(self, nums: list[int], k: int) -> int:
        def chunks(cap):
            n, total = 1, 0
            for x in nums:
                n, total = (n + 1, x) if total + x > cap else (n, total + x)
            return n

        lo, hi = max(nums), sum(nums)
        return lo + bisect_left(
            range(lo, hi + 1), True, key=lambda cap: chunks(cap) <= k
        )
```

</details>

```mu
# 516. Longest Palindromic Subsequence (dynamic programming)
class Solution
  def longestPalindromeSubseq(s: str) -> int
    lcs(s, s[::-1])
```

<details><summary>mu</summary>

```mu
# 516. Longest Palindromic Subsequence (dynamic programming)
class Solution
  def longestPalindromeSubseq(s: str) -> int
    memo f(i, j) =
      | i > j        -> 0
      | i == j       -> 1
      | s[i] == s[j] -> 2 + f(i + 1, j - 1)
      | else         -> max(f(i + 1, j), f(i, j - 1))
    f(0, len(s) - 1)
```

</details>

<details><summary>python</summary>

```python
# 516. Longest Palindromic Subsequence (dynamic programming)
class Solution:
    def longestPalindromeSubseq(self, s: str) -> int:
        n = len(s)
        dp = [0] * n
        for i in range(n - 1, -1, -1):
            dp[i], prev = 1, 0
            for j in range(i + 1, n):
                prev, dp[j] = dp[j], prev + 2 if s[i] == s[j] else max(dp[j], dp[j - 1])
        return dp[-1]
```

</details>

```mu
# 542. 01 Matrix (multi-source breadth-first search)
class Solution
  def updateMatrix(mat: [[int]]) -> [[int]]
    grid_bfs(mat, list(cells(mat, eq=0)))
```

<details><summary>mu</summary>

```mu
# 542. 01 Matrix (multi-source breadth-first search)
class Solution
  def updateMatrix(mat: [[int]]) -> [[int]]
    m, n = len(mat), len(mat[0])
    q = [(i, j) for i in 0..<m for j in 0..<n if mat[i][j] == 0]
    dist = [[v ? -1 : 0 for v in row] for row in mat]
    for i, j in q
      for a, b in [(i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)]
        if 0 <= a < m and 0 <= b < n and dist[a][b] == -1
          dist[a][b] = dist[i][j] + 1
          q <- (a, b)
    dist
```

</details>

<details><summary>python</summary>

```python
# 542. 01 Matrix (multi-source breadth-first search)
class Solution:
    def updateMatrix(self, mat: list[list[int]]) -> list[list[int]]:
        m, n = len(mat), len(mat[0])
        q = [(i, j) for i in range(m) for j in range(n) if mat[i][j] == 0]
        dist = [[-1 if v else 0 for v in row] for row in mat]
        for i, j in q:
            for a, b in [(i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)]:
                if 0 <= a < m and 0 <= b < n and dist[a][b] == -1:
                    dist[a][b] = dist[i][j] + 1
                    q.append((a, b))
        return dist
```

</details>

```mu
# 547. Number of Provinces (connected components)
class Solution
  def findCircleNum(isConnected: [[int]]) -> int
    len components(0..<len(isConnected), u -> [v for >v, x in isConnected[u] if x])
```

<details><summary>mu</summary>

```mu
# 547. Number of Provinces (connected components)
class Solution
  def findCircleNum(isConnected: [[int]]) -> int
    seen = set()
    sum for s in 0..<len(isConnected) if s not in seen
      st = [s]
      while st
        u = st .
        if u not in seen
          seen.add(u)
          st += [v for >v, x in isConnected[u] if x]
      1
```

</details>

<details><summary>python</summary>

```python
# 547. Number of Provinces (connected components)
class Solution:
    def findCircleNum(self, isConnected: list[list[int]]) -> int:
        seen, provinces = set(), 0
        for s in range(len(isConnected)):
            if s not in seen:
                provinces += 1
                st = [s]
                while st:
                    u = st.pop()
                    if u not in seen:
                        seen.add(u)
                        st += [v for v, x in enumerate(isConnected[u]) if x]
        return provinces
```

</details>

```mu
# 560. Subarray Sum Equals K (prefix sums)
class Solution
  def subarraySum(nums: [int], k: int) -> int
    count_pairs(scan(+, [0] + nums), lookup=p -> p - k)
```

<details><summary>mu</summary>

```mu
# 560. Subarray Sum Equals K (prefix sums)
class Solution
  def subarraySum(nums: [int], k: int) -> int
    seen, total = counter([0]), 0
    sum for x in nums
      total += x
      found = seen[total - k]
      seen[total] += 1
      found
```

</details>

<details><summary>python</summary>

```python
# 560. Subarray Sum Equals K (prefix sums)
from collections import Counter


class Solution:
    def subarraySum(self, nums: list[int], k: int) -> int:
        seen, total, count = Counter([0]), 0, 0
        for x in nums:
            total += x
            count += seen[total - k]
            seen[total] += 1
        return count
```

</details>

```mu
# 583. Delete Operation for Two Strings (dynamic programming)
class Solution
  def minDistance(word1: str, word2: str) -> int
    len(word1) + len(word2) - 2 * lcs(word1, word2)
```

<details><summary>mu</summary>

```mu
# 583. Delete Operation for Two Strings (dynamic programming)
class Solution
  def minDistance(word1: str, word2: str) -> int
    memo f(i, j) =
      | i == len(word1)      -> len(word2) - j
      | j == len(word2)      -> len(word1) - i
      | word1[i] == word2[j] -> f(i + 1, j + 1)
      | else                 -> 1 + min(f(i + 1, j), f(i, j + 1))
    f(0, 0)
```

</details>

<details><summary>python</summary>

```python
# 583. Delete Operation for Two Strings (dynamic programming)
class Solution:
    def minDistance(self, word1: str, word2: str) -> int:
        dp = list(range(len(word2) + 1))
        for i, a in enumerate(word1, 1):
            prev, dp[0] = dp[0], i
            for j, b in enumerate(word2, 1):
                prev, dp[j] = dp[j], prev if a == b else 1 + min(dp[j], dp[j - 1])
        return dp[-1]
```

</details>

```mu
# 695. Max Area of Island (connected components)
class Solution
  def maxAreaOfIsland(grid: [[int]]) -> int
    max from 0 for c in components(cells(grid, eq=1), p -> nbrs(grid, p, eq=1)): len(c)
```

<details><summary>mu</summary>

```mu
# 695. Max Area of Island (connected components)
from itertools import product

class Solution
  def maxAreaOfIsland(grid: [[int]]) -> int
    m, n = len(grid), len(grid[0])
    max from 0 for p in product(0..<m, 0..<n) if grid[p] == 1
      st, area = [p], 0
      while st
        i, j = st .
        if 0 <= i < m and 0 <= j < n and grid[i][j] == 1
          grid[i][j] = 0
          area += 1
          st += [(i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)]
      area
```

</details>

<details><summary>python</summary>

```python
# 695. Max Area of Island (connected components)
class Solution:
    def maxAreaOfIsland(self, grid: list[list[int]]) -> int:
        m, n, best = len(grid), len(grid[0]), 0
        for i in range(m):
            for j in range(n):
                st, area = [(i, j)], 0
                while st:
                    a, b = st.pop()
                    if 0 <= a < m and 0 <= b < n and grid[a][b] == 1:
                        grid[a][b] = 0
                        area += 1
                        st += [(a + 1, b), (a - 1, b), (a, b + 1), (a, b - 1)]
                best = max(best, area)
        return best
```

</details>

```mu
# 743. Network Delay Time (Dijkstra's algorithm)
class Solution
  def networkDelayTime(times: [[int]], n: int, k: int) -> int
    max(dijkstra(graph(1..n, times, directed=True), k).values()) or -1 if inf
```

<details><summary>mu</summary>

```mu
# 743. Network Delay Time (Dijkstra's algorithm)
from heapq import heappop, heappush

class Solution
  def networkDelayTime(times: [[int]], n: int, k: int) -> int
    adj = {:list: u <- (v, w) for u, v, w in times}
    dist, pq = {}, [(0, k)]
    while pq
      d, u = heappop(pq)
      if u not in dist
        dist[u] = d
        for v, w in adj[u]
          heappush(pq, (d + w, v))
    len(dist) == n ? max(dist.values()) : -1
```

</details>

<details><summary>python</summary>

```python
# 743. Network Delay Time (Dijkstra's algorithm)
from collections import defaultdict
from heapq import heappop, heappush


class Solution:
    def networkDelayTime(self, times: list[list[int]], n: int, k: int) -> int:
        adj = defaultdict(list)
        for u, v, w in times:
            adj[u].append((v, w))
        dist, pq = {}, [(0, k)]
        while pq:
            d, u = heappop(pq)
            if u not in dist:
                dist[u] = d
                for v, w in adj[u]:
                    heappush(pq, (d + w, v))
        return max(dist.values()) if len(dist) == n else -1
```

</details>

```mu
# 778. Swim in Rising Water (binary search on the answer)
class Solution
  def swimInWater(grid: [[int]]) -> int
    reach = t -> grid_bfs(grid, [(0, 0)], ok=v -> v <= t)[-1][-1] >= 0
    first_true(grid[0][0], len(grid) ** 2, reach)
```

<details><summary>mu</summary>

```mu
# 778. Swim in Rising Water (binary search on the answer)
class Solution
  def swimInWater(grid: [[int]]) -> int
    n = len(grid)

    def reach(t)
      seen, st = {(0, 0)}, [(0, 0)]
      while st
        i, j = st .
        for a, b in [(i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)]
          if 0 <= a < n and 0 <= b < n and (a, b) not in seen and grid[a][b] <= t
            seen.add((a, b))
            st <- (a, b)
      (n - 1, n - 1) in seen

    first t in grid[0][0]..(n * n) if reach(t)
```

</details>

<details><summary>python</summary>

```python
# 778. Swim in Rising Water (binary search on the answer)
from bisect import bisect_left


class Solution:
    def swimInWater(self, grid: list[list[int]]) -> int:
        n = len(grid)

        def reach(t):
            seen, st = {(0, 0)}, [(0, 0)]
            while st:
                i, j = st.pop()
                for a, b in [(i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)]:
                    if (
                        0 <= a < n
                        and 0 <= b < n
                        and (a, b) not in seen
                        and grid[a][b] <= t
                    ):
                        seen.add((a, b))
                        st.append((a, b))
            return (n - 1, n - 1) in seen

        lo = grid[0][0]
        return lo + bisect_left(range(lo, n * n + 1), True, key=reach)
```

</details>

```mu
# 875. Koko Eating Bananas (binary search on the answer)
class Solution
  def minEatingSpeed(piles: [int], h: int) -> int
    first_true(1, max(piles), k -> sum(ceil_div(p, k) for p in piles) <= h)
```

<details><summary>mu</summary>

```mu
# 875. Koko Eating Bananas (binary search on the answer)
class Solution
  def minEatingSpeed(piles: [int], h: int) -> int
    first k in 1..max(piles) if sum(ceil(p / k) for p in piles) <= h
```

</details>

<details><summary>python</summary>

```python
# 875. Koko Eating Bananas (binary search on the answer)
from bisect import bisect_left


class Solution:
    def minEatingSpeed(self, piles: list[int], h: int) -> int:
        def ok(k):
            return sum((p + k - 1) // k for p in piles) <= h

        return 1 + bisect_left(range(1, max(piles) + 1), True, key=ok)
```

</details>

```mu
# 974. Subarray Sums Divisible by K (prefix sums)
class Solution
  def subarraysDivByK(nums: [int], k: int) -> int
    count_pairs(scan(+, [0] + nums), key=p -> p % k)
```

<details><summary>mu</summary>

```mu
# 974. Subarray Sums Divisible by K (prefix sums)
class Solution
  def subarraysDivByK(nums: [int], k: int) -> int
    seen, total = counter([0]), 0
    sum for x in nums
      total = (total + x) % k
      found = seen[total]
      seen[total] += 1
      found
```

</details>

<details><summary>python</summary>

```python
# 974. Subarray Sums Divisible by K (prefix sums)
from collections import Counter


class Solution:
    def subarraysDivByK(self, nums: list[int], k: int) -> int:
        seen, total, count = Counter([0]), 0, 0
        for x in nums:
            total = (total + x) % k
            count += seen[total]
            seen[total] += 1
        return count
```

</details>

```mu
# 1011. Capacity To Ship Packages Within D Days (binary search on the answer)
class Solution
  def shipWithinDays(weights: [int], days: int) -> int
    first_true(max(weights), sum(weights), cap -> min_chunks(weights, cap) <= days)
```

<details><summary>mu</summary>

```mu
# 1011. Capacity To Ship Packages Within D Days (binary search on the answer)
class Solution
  def shipWithinDays(weights: [int], days: int) -> int
    def trips(cap)
      n, load = 1, 0
      for w in weights
        n, load = load + w > cap ? (n + 1, w) : (n, load + w)
      n
    first cap in max(weights)..sum(weights) if trips(cap) <= days
```

</details>

<details><summary>python</summary>

```python
# 1011. Capacity To Ship Packages Within D Days (binary search on the answer)
from bisect import bisect_left


class Solution:
    def shipWithinDays(self, weights: list[int], days: int) -> int:
        def trips(cap):
            n, load = 1, 0
            for w in weights:
                n, load = (n + 1, w) if load + w > cap else (n, load + w)
            return n

        lo, hi = max(weights), sum(weights)
        return lo + bisect_left(range(lo, hi + 1), True, key=lambda c: trips(c) <= days)
```

</details>

```mu
# 1020. Number of Enclaves (connected components)
class Solution
  def numEnclaves(grid: [[int]]) -> int
    islands = components(cells(grid, eq=1), p -> nbrs(grid, p, eq=1))
    sum for c in islands if not any(is_edge(grid, i, j) for i, j in c): len(c)
```

<details><summary>mu</summary>

```mu
# 1020. Number of Enclaves (connected components)
class Solution
  def numEnclaves(grid: [[int]]) -> int
    m, n = len(grid), len(grid[0])
    st = [(i, j) for i in 0..<m for j in 0..<n if i in (0, m - 1) or j in (0, n - 1)]
    while st
      i, j = st .
      if 0 <= i < m and 0 <= j < n and grid[i][j] == 1
        grid[i][j] = 0
        st += [(i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)]
    sum for row in grid: sum(row)
```

</details>

<details><summary>python</summary>

```python
# 1020. Number of Enclaves (connected components)
class Solution:
    def numEnclaves(self, grid: list[list[int]]) -> int:
        m, n = len(grid), len(grid[0])
        st = [
            (i, j)
            for i in range(m)
            for j in range(n)
            if i in (0, m - 1) or j in (0, n - 1)
        ]
        while st:
            i, j = st.pop()
            if 0 <= i < m and 0 <= j < n and grid[i][j] == 1:
                grid[i][j] = 0
                st += [(i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)]
        return sum(map(sum, grid))
```

</details>

```mu
# 1074. Number of Submatrices That Sum to Target (prefix sums)
class Solution
  def numSubmatrixSumTarget(matrix: [[int]], target: int) -> int
    P = [[0] * len(matrix[0]), *scan((a, b) -> [x + y for x, y in zip(a, b)], matrix)]
    strip = (a, b) -> scan(+, [0] + [y - x for x, y in zip(a, b)])
    sum for i, j in pairs(len(P)): count_pairs(strip(P[i], P[j]), lookup=s -> s - target)
```

<details><summary>mu</summary>

```mu
# 1074. Number of Submatrices That Sum to Target (prefix sums)
class Solution
  def numSubmatrixSumTarget(matrix: [[int]], target: int) -> int
    P = [[0] * len(matrix[0]), *scan((a, b) -> [x + y for x, y in zip(a, b)], matrix)]
    ret count = 0
    for >i, a in P
      for b in P[i + 1:]
        seen, total = counter([0]), 0
        for x, y in zip(a, b)
          total += y - x
          count += seen[total - target]
          seen[total] += 1
```

</details>

<details><summary>python</summary>

```python
# 1074. Number of Submatrices That Sum to Target (prefix sums)
from collections import Counter


class Solution:
    def numSubmatrixSumTarget(self, matrix: list[list[int]], target: int) -> int:
        P = [[0] * len(matrix[0])]
        for row in matrix:
            P.append([x + y for x, y in zip(P[-1], row)])
        count = 0
        for i, a in enumerate(P):
            for b in P[i + 1 :]:
                seen, total = Counter([0]), 0
                for x, y in zip(a, b):
                    total += y - x
                    count += seen[total - target]
                    seen[total] += 1
        return count
```

</details>

```mu
# 1091. Shortest Path in Binary Matrix (breadth-first search)
class Solution
  def shortestPathBinaryMatrix(grid: [[int]]) -> int
    eight = [(a, b) for a in -1..1 for b in -1..1]
    d = grid_bfs(grid, [(0, 0)], ok=v -> not v, dirs=eight)
    grid[0][0] or d[-1][-1] < 0 ? -1 : d[-1][-1] + 1
```

<details><summary>mu</summary>

```mu
# 1091. Shortest Path in Binary Matrix (breadth-first search)
class Solution
  def shortestPathBinaryMatrix(grid: [[int]]) -> int
    n = len(grid)
    if grid[0][0]
      return -1
    dist, q = {(0, 0): 1}, [(0, 0)]
    for i, j in q
      for a in i - 1..i + 1
        for b in j - 1..j + 1
          if 0 <= a < n and 0 <= b < n and not grid[a][b] and (a, b) not in dist
            dist[(a, b)] = dist[(i, j)] + 1
            q <- (a, b)
    dist.get((n - 1, n - 1), -1)
```

</details>

<details><summary>python</summary>

```python
# 1091. Shortest Path in Binary Matrix (breadth-first search)
class Solution:
    def shortestPathBinaryMatrix(self, grid: list[list[int]]) -> int:
        n = len(grid)
        if grid[0][0]:
            return -1
        dist, q = {(0, 0): 1}, [(0, 0)]
        for i, j in q:
            for a in range(i - 1, i + 2):
                for b in range(j - 1, j + 2):
                    if (
                        0 <= a < n
                        and 0 <= b < n
                        and not grid[a][b]
                        and (a, b) not in dist
                    ):
                        dist[(a, b)] = dist[(i, j)] + 1
                        q.append((a, b))
        return dist.get((n - 1, n - 1), -1)
```

</details>

```mu
# 1143. Longest Common Subsequence (dynamic programming)
class Solution
  def longestCommonSubsequence(text1: str, text2: str) -> int
    lcs(text1, text2)
```

<details><summary>mu</summary>

```mu
# 1143. Longest Common Subsequence (dynamic programming)
class Solution
  def longestCommonSubsequence(text1: str, text2: str) -> int
    memo f(i, j) =
      | i == len(text1) or j == len(text2) -> 0
      | text1[i] == text2[j]               -> 1 + f(i + 1, j + 1)
      | else                               -> max(f(i + 1, j), f(i, j + 1))
    f(0, 0)
```

</details>

<details><summary>python</summary>

```python
# 1143. Longest Common Subsequence (dynamic programming)
class Solution:
    def longestCommonSubsequence(self, text1: str, text2: str) -> int:
        dp = [0] * (len(text2) + 1)
        for a in text1:
            prev = 0
            for j, b in enumerate(text2, 1):
                prev, dp[j] = dp[j], prev + 1 if a == b else max(dp[j], dp[j - 1])
        return dp[-1]
```

</details>

```mu
# 1162. As Far from Land as Possible (multi-source breadth-first search)
class Solution
  def maxDistance(grid: [[int]]) -> int
    d = grid_bfs(grid, list(cells(grid, eq=1)))
    max from -1 for i, j in cells(grid, eq=0): d[i][j]
```

<details><summary>mu</summary>

```mu
# 1162. As Far from Land as Possible (multi-source breadth-first search)
class Solution
  def maxDistance(grid: [[int]]) -> int
    n = len(grid)
    q = [(i, j) for i in 0..<n for j in 0..<n if grid[i][j]]
    dist = {p: 0 for p in q}
    for i, j in q
      for a, b in [(i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)]
        if 0 <= a < n and 0 <= b < n and (a, b) not in dist
          dist[(a, b)] = dist[(i, j)] + 1
          q <- (a, b)
    max from -1 for p, d in dist.items() if not grid[p]: d
```

</details>

<details><summary>python</summary>

```python
# 1162. As Far from Land as Possible (multi-source breadth-first search)
class Solution:
    def maxDistance(self, grid: list[list[int]]) -> int:
        n = len(grid)
        q = [(i, j) for i in range(n) for j in range(n) if grid[i][j]]
        dist = {p: 0 for p in q}
        for i, j in q:
            for a, b in [(i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)]:
                if 0 <= a < n and 0 <= b < n and (a, b) not in dist:
                    dist[(a, b)] = dist[(i, j)] + 1
                    q.append((a, b))
        return max((d for (i, j), d in dist.items() if not grid[i][j]), default=-1)
```

</details>

```mu
# 1248. Count Number of Nice Subarrays (prefix sums)
class Solution
  def numberOfSubarrays(nums: [int], k: int) -> int
    count_pairs(scan(+, [0] + [x % 2 for x in nums]), lookup=p -> p - k)
```

<details><summary>mu</summary>

```mu
# 1248. Count Number of Nice Subarrays (prefix sums)
class Solution
  def numberOfSubarrays(nums: [int], k: int) -> int
    seen, odd = counter([0]), 0
    sum for x in nums
      odd += x % 2
      found = seen[odd - k]
      seen[odd] += 1
      found
```

</details>

<details><summary>python</summary>

```python
# 1248. Count Number of Nice Subarrays (prefix sums)
from collections import Counter


class Solution:
    def numberOfSubarrays(self, nums: list[int], k: int) -> int:
        seen, odd, count = Counter([0]), 0, 0
        for x in nums:
            odd += x % 2
            count += seen[odd - k]
            seen[odd] += 1
        return count
```

</details>

```mu
# 1254. Number of Closed Islands (connected components)
class Solution
  def closedIsland(grid: [[int]]) -> int
    islands = components(cells(grid, eq=0), p -> nbrs(grid, p, eq=0))
    count for c in islands if not any(is_edge(grid, i, j) for i, j in c)
```

<details><summary>mu</summary>

```mu
# 1254. Number of Closed Islands (connected components)
from itertools import product

class Solution
  def closedIsland(grid: [[int]]) -> int
    m, n = len(grid), len(grid[0])

    def sink(i, j)
      st = [(i, j)]
      while st
        a, b = st .
        if 0 <= a < m and 0 <= b < n and grid[a][b] == 0
          grid[a][b] = 1
          st += [(a + 1, b), (a - 1, b), (a, b + 1), (a, b - 1)]

    for i, j in product(0..<m, 0..<n)
      if i in (0, m - 1) or j in (0, n - 1)
        sink(i, j)
    sum for i, j in product(0..<m, 0..<n) if grid[i][j] == 0
      sink(i, j)
      1
```

</details>

<details><summary>python</summary>

```python
# 1254. Number of Closed Islands (connected components)
class Solution:
    def closedIsland(self, grid: list[list[int]]) -> int:
        m, n = len(grid), len(grid[0])

        def sink(i, j):
            st = [(i, j)]
            while st:
                a, b = st.pop()
                if 0 <= a < m and 0 <= b < n and grid[a][b] == 0:
                    grid[a][b] = 1
                    st += [(a + 1, b), (a - 1, b), (a, b + 1), (a, b - 1)]

        for i in range(m):
            for j in range(n):
                if i in (0, m - 1) or j in (0, n - 1):
                    sink(i, j)
        count = 0
        for i in range(m):
            for j in range(n):
                if grid[i][j] == 0:
                    sink(i, j)
                    count += 1
        return count
```

</details>

```mu
# 1334. Find the City With the Smallest Number of Neighbors at a Threshold Distance
# (Dijkstra's algorithm)
class Solution
  def findTheCity(n: int, edges: [[int]], distanceThreshold: int) -> int
    g, t = graph(0..<n, edges), distanceThreshold
    min(reversed(0..<n), key=c -> sum(d <= t for d in dijkstra(g, c).values()))
```

<details><summary>mu</summary>

```mu
# 1334. Find the City With the Smallest Number of Neighbors at a Threshold Distance
# (Floyd-Warshall algorithm)
class Solution
  def findTheCity(n: int, edges: [[int]], distanceThreshold: int) -> int
    D = [[i == j ? 0 : inf for j in 0..<n] for i in 0..<n]
    for u, v, w in edges
      D[u][v] = D[v][u] = w
    for k in 0..<n
      for i in 0..<n
        for j in 0..<n
          D[i][j] = min(D[i][j], D[i][k] + D[k][j])
    min(reversed(0..<n), key=c -> sum(d <= distanceThreshold for d in D[c]))
```

</details>

<details><summary>python</summary>

```python
# 1334. Find the City With the Smallest Number of Neighbors at a Threshold Distance
# (Floyd-Warshall algorithm)
from math import inf


class Solution:
    def findTheCity(
        self, n: int, edges: list[list[int]], distanceThreshold: int
    ) -> int:
        D = [[0 if i == j else inf for j in range(n)] for i in range(n)]
        for u, v, w in edges:
            D[u][v] = D[v][u] = w
        for k in range(n):
            for i in range(n):
                for j in range(n):
                    D[i][j] = min(D[i][j], D[i][k] + D[k][j])
        return min(
            reversed(range(n)), key=lambda c: sum(d <= distanceThreshold for d in D[c])
        )
```

</details>

```mu
# 1971. Find if Path Exists in Graph (connected components)
class Solution
  def validPath(n: int, edges: [[int]], source: int, destination: int) -> bool
    g = adjacency(edges, directed=False)
    any({source, destination} <= set(c) for c in components(0..<n, u -> g[u]))
```

<details><summary>mu</summary>

```mu
# 1971. Find if Path Exists in Graph (connected components)
class Solution
  def validPath(n: int, edges: [[int]], source: int, destination: int) -> bool
    g = {:list: u <- v for u, v in edges + [e[::-1] for e in edges]}
    seen, st = {source}, [source]
    while st
      for v in g[st .]
        if v not in seen
          seen.add(v)
          st <- v
    destination in seen
```

</details>

<details><summary>python</summary>

```python
# 1971. Find if Path Exists in Graph (connected components)
from collections import defaultdict


class Solution:
    def validPath(
        self, n: int, edges: list[list[int]], source: int, destination: int
    ) -> bool:
        g = defaultdict(list)
        for u, v in edges:
            g[u].append(v)
            g[v].append(u)
        seen, st = {source}, [source]
        while st:
            for v in g[st.pop()]:
                if v not in seen:
                    seen.add(v)
                    st.append(v)
        return destination in seen
```

</details>

## Spec

The language is specified one version at a time: [v0.1](spec/v0.1.md), [v0.2](spec/v0.2.md), [v0.3](spec/v0.3.md), [v0.4](spec/v0.4.md), [v0.5](spec/v0.5.md), [v0.6](spec/v0.6.md), [v0.7](spec/v0.7.md), [v0.8](spec/v0.8.md), [v0.9](spec/v0.9.md), [v0.10](spec/v0.10.md), [v0.11](spec/v0.11.md), [v0.12](spec/v0.12.md).

## Profiles

The language owns a few helpers (`Grid`, `deep`). The rest come from a
profile: a Python file that registers a project's library with
`mulang.library(module, *names)` and the names its run time preloads with
`mulang.environment(objects)`. This profile registers
[pydsa](https://www.shyal.com/pydsa/) (`pip install pizza-lib`), which the
solves above use in their versions with pydsa:

```python
# pydsa_profile.py
import mulang
from pydsa import adj, arith, combo, counting, generators, grid, heaps, search, seq

for module in adj, arith, combo, counting, generators, grid, heaps, search, seq:
    mulang.library(module)
```

A project names its profile in `pyproject.toml`:

```toml
[tool.mu]
profile = "pydsa_profile.py"
```

`python -m mulang`, the REPL, `py2mu` and the ide load it from the
working directory, or from the workspace root the ide is asked about.
The first profile is the LeetCode one in the `leet` repo,
`mu/leetmu.py`, whose library is also pydsa.

## Tools

- `mulang.ide`: hover, completion, go-to-definition and signature help
  over JSON lines, through the transpiled Python and Jedi.
- `mulang.py2mu`: a Python solution written as mu, in as few lines as the
  language allows.
- `vscode/`: the VS Code extension (highlighting, formatter, the ide, a
  debugger that steps through the mu file). Install the `.vsix` from the
  latest `vscode-v*` release, or link the folder into
  `~/.vscode/extensions` and reload the window. See
  [vscode/README.md](vscode/README.md).

## Releasing

PyPI: bump `version` in `pyproject.toml`, then `uv build` and
`uv publish dist/*` with `UV_PUBLISH_TOKEN` set (`.envrc`).

Editor extension: bump `version` in `vscode/package.json` and add a line to
`vscode/CHANGELOG.md`, then tag and push:

```
git tag vscode-v0.1.0 && git push origin vscode-v0.1.0
```

The `vscode` workflow runs the adapter tests, packages the `.vsix`, checks
the tag against `package.json`, and attaches the `.vsix` to a GitHub
release. A pull request that touches `vscode/` runs the same steps without
releasing. The extension is on neither registry: the VS Code Marketplace
needs a Microsoft account and Open VSX an Eclipse account, and neither
would open one.

## Tests

```
pip install -e '.[test]'
pytest
```

The tests also need `pydsa`, the leet profile's library, on the path. Two of
them run only under the leet harness, whose `sitecustomize` preloads the names
LeetCode preloads; elsewhere they skip.
