VERSION := $(shell sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
VSCODE := $(shell sed -n 's/^  "version": "\(.*\)",/\1/p' vscode/package.json)

PY := .venv/bin/python

.PHONY: venv release

# the dev venv: this repo and its sibling as editable installs, so a change
# in either is live, with the test, docs and drawing extras
venv:
	uv venv --python 3.13 .venv
	uv pip install --python $(PY) -e '.[test,ide,docs]' -e '../pydsa[draw]'

# push master (the docs build), tag the extension when its version has no
# tag yet (the .vsix release), then publish mu-language to PyPI
release:
	$(PY) -m pytest -q tests
	git push origin master
	git rev-parse -q --verify refs/tags/vscode-v$(VSCODE) >/dev/null || \
		(git tag vscode-v$(VSCODE) && git push origin vscode-v$(VSCODE))
	rm -rf dist && uv build && uv publish
	@echo "mu-language $(VERSION) released"
