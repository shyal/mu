# mu for VS Code

Support for [mu](https://github.com/shyal/mu), a small language that
transpiles to Python: highlighting, formatting on save, hover, completion,
go-to-definition, signature help, rename (F2), and a debugger that steps through the
mu file while debugpy runs the Python it produced.

## Install

Download `mu-<version>.vsix` from the latest
[`vscode-v*` release](https://github.com/shyal/mu/releases). In VS Code:

```
code --install-extension mu-<version>.vsix
```

In VS Code, Cursor, VSCodium or Windsurf, the extensions view's menu also
has "Install from VSIX".

## Setup

The extension runs the `mulang` package in a Python of your choice.
Install it there, with Jedi for the editor features:

```
pip install 'mu-language[ide]'
```

Then point the extension at that interpreter if it is not `python3`:

```json
"mu.python": "${workspaceFolder}/.venv/bin/python"
```

Highlighting needs nothing installed. Formatting needs `mu-language`.
Hover, completion, go-to-definition and signature help need Jedi, which
the `ide` extra brings; rename needs only `mu-language`. Debugging needs the Python Debugger extension
(`ms-python.debugpy`, installed as a dependency) and a debug script in the
workspace; see below.

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `mu.python` | `python3` | The interpreter that runs `mulang`. |
| `mu.debugScript` | `mu/session.py` | A script under the workspace root that `debug [file.mu]` writes the Python and its line map with, and prints the Python file's path. |
| `mu.debugLogDir` | unset | When set, debugpy's logs go to this folder. |

`.mu` files format on save with two-space indentation; both are set in the
extension's configuration defaults and can be overridden per workspace.

## Debugging

Press F5 in a `.mu` file, or use the run button in the editor title. The
extension calls the debug script, which transpiles the file and writes a
line map, then starts debugpy on the Python with a proxy in between.
Breakpoints set on mu lines land on the first Python line each produced,
stack frames are reported at their mu line, and one step is one mu line.

A debug script is a small Python program that takes `debug file.mu`,
writes `file.py` and a JSON line map next to it, and prints the path of
the Python file. The one in the project this language came from is
[`mu/session.py`](https://github.com/shyal/leet/blob/master/mu/session.py).

## Development

```
cd vscode
ln -sfn "$PWD" ~/.vscode/extensions/shyal.mu-dev   # then reload the window
npm test                                            # the debug adapter
npx @vscode/vsce package                            # a .vsix
```

Releases are tagged `vscode-v<version>`; the `vscode` workflow in this
repository attaches the `.vsix` to a GitHub release.
