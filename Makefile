VERSION := $(shell sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
VSCODE := $(shell sed -n 's/^  "version": "\(.*\)",/\1/p' vscode/package.json)

# the leet venv has mu and pydsa as editable installs, so the tests see both
PY ?= $(HOME)/dev/leet/.venv/bin/python

.PHONY: release

# push master (the docs build), tag the extension when its version has no
# tag yet (the .vsix release), then publish mu-language to PyPI
release:
	$(PY) -m pytest -q tests
	git push origin master
	git rev-parse -q --verify refs/tags/vscode-v$(VSCODE) >/dev/null || \
		(git tag vscode-v$(VSCODE) && git push origin vscode-v$(VSCODE))
	rm -rf dist && uv build && uv publish
	@echo "mu-language $(VERSION) released"
