# Contributing

Before opening a PR, please read the contributing guide:
<https://github.com/OrbFrontend/Orb/blob/main/CONTRIBUTING.md>

Ideas, help requests, and questions go in [Discussions](https://github.com/OrbFrontend/Orb/discussions).

## Editing the Wiki

This wiki is built with [MkDocs Material](https://squidfunk.github.io/mkdocs-material/) from Markdown files in [`docs/`](https://github.com/OrbFrontend/Orb/tree/main/docs).

Local preview:

```bash
pip install -r requirements-docs.txt
mkdocs serve
```

Then open <http://127.0.0.1:8000>. Pages are auto-reloaded as you edit.

PRs that change `docs/**` or `mkdocs.yml` are deployed to GitHub Pages on merge to `main`.
