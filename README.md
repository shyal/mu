# mu

mu is a small language that transpiles to Python. A file is indented
blocks without colons, calls of one argument without brackets, `..`
ranges, `x -> ...` lambdas, `<-` push, folds (`sum for x in xs: f(x)`),
`memo` for a cached function with cases, and `class` for a Python class.
The Python it produces is plain and runs anywhere; the transpiler pastes
in the source of every helper the file calls.

```
pip install mu-language
mu file.mu        # the Python
mu --fmt file.mu  # the file, formatted
mu-repl           # a REPL
```

## Spec

The language is specified one version at a time: [v0.1](spec/v0.1.md), [v0.2](spec/v0.2.md), [v0.3](spec/v0.3.md), [v0.4](spec/v0.4.md), [v0.5](spec/v0.5.md), [v0.6](spec/v0.6.md), [v0.7](spec/v0.7.md), [v0.8](spec/v0.8.md), [v0.9](spec/v0.9.md), [v0.10](spec/v0.10.md).

## Profiles

The language owns a few helpers (`Grid`, `deep`). The rest come from a
profile: a Python file that registers a project's library with
`mulang.library(module, *names)` and the names its run time preloads with
`mulang.environment(objects)`. A project names its profile in
`pyproject.toml`:

```toml
[tool.mu]
profile = "mu/leetmu.py"
```

`python -m mulang`, the REPL, `py2mu` and the ide load it from the
working directory, or from the workspace root the ide is asked about.
The first profile is the LeetCode one in the `leet` repo, whose library
is the `pydsa` package (not yet published).

## Tools

- `mulang.ide`: hover, completion, go-to-definition and signature help
  over JSON lines, through the transpiled Python and Jedi.
- `mulang.py2mu`: a Python solution written as mu, in as few lines as the
  language allows.
- `vscode/`: the VS Code extension (highlighting, formatter, the ide, a
  debugger that steps through the mu file). Link it into
  `~/.vscode/extensions` and reload the window.

## Tests

```
pip install -e '.[test]'
pytest
```

The tests also need `pydsa`, the leet profile's library, on the path. Two of
them run only under the leet harness, whose `sitecustomize` preloads the names
LeetCode preloads; elsewhere they skip.
