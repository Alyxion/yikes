# Development

Setup for working on yikes! itself.

## Setup

```bash
git clone https://github.com/Alyxion/yikes && cd yikes
poetry install                       # runtime + dev dependencies
git config core.hooksPath .githooks  # enable the repo's git hooks (one-time, per clone)
```

The pre-commit hook (`.githooks/pre-commit`) refuses to commit raw training
captures under `training_data/samples/` — they are verbatim terminal dumps that
can contain secrets. See [Activity & Training](activity-training.md) for the full
capture workflow.

## Tests

```bash
poetry run pytest -q
```

End-to-end tests that invoke real Claude Code, Codex, Docker, or tmux are opt-in
(marked `integration`) because they depend on local credentials and may spend API
credits. Run them explicitly with `poetry run pytest -m integration`.

## Docs

```bash
poetry run mkdocs serve            # live preview
poetry run mkdocs build --strict   # what CI builds
```

Diagrams are committed SVGs under `docs/diagrams/` (and `media/` for the README
hero), authored in Excalidraw; keep the `.excalidraw` source beside each export.

## Release

```bash
# bump the version in pyproject.toml (single source of truth), then:
poetry install                     # refresh the editable install's metadata
poetry build
poetry run twine check dist/*
poetry run twine upload dist/*     # credentials from ~/.pypirc or `poetry config pypi-token.pypi`
git tag -a vX.Y.Z -m "yikes X.Y.Z" && git push --tags
```
