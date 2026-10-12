# 2231. Largest Number After Digit Swaps by Parity
class Solution
  def largestInteger(num: int) -> int
    ds = to_digits num
    by = {r: sorted(d for d in ds if d % 2 == r) for r in 0..1}
    to_int([by[d % 2] . for d in ds])
