"""The tests register pydsa as the library, the way the leet profile does,
so the programs that call cells, nbrs or heap transpile and run."""

import pydsa
from pydsa import adj, arith, combo, counting, digits, generators, grid, heaps, search

import mulang

# the helpers a mu file may call by name, the leet profile's library
LIBRARY = {
    generators: ["as_list"],
    grid: [
        "cells", "nbrs", "table", "like", "shape", "put", "row", "col", "set_row",
        "set_col", "in_bounds", "is_edge", "edges", "grid_bfs",
    ],
    combo: ["pairs", "triples"],
    digits: ["to_digits", "to_int", "even", "odd"],
    arith: ["ceil_div"],
    search: ["first_true", "last_true", "first_false", "last_false", "min_chunks"],
    counting: ["Multiset"],
    adj: ["levels", "adjacency", "indegrees", "components", "graph", "dijkstra"],
    heaps: ["heap"],
}  # fmt: skip

for module, names in LIBRARY.items():
    mulang.library(module, *names)
mulang.environment(
    {
        name: getattr(pydsa, name)
        for name in dir(pydsa)
        if not name.startswith("_")
        and str(getattr(getattr(pydsa, name), "__module__", "")).startswith("pydsa")
    }
)
