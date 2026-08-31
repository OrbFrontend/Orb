# Contributing

Read the [contributing guide](https://github.com/OrbFrontend/Orb/blob/main/CONTRIBUTING.md)
before opening a pull request.

Use [GitHub Discussions](https://github.com/OrbFrontend/Orb/discussions) for
questions, ideas, and help requests. Use issues for confirmed bugs and focused
feature requests.

## Build the documentation locally

The wiki uses [MkDocs Material](https://squidfunk.github.io/mkdocs-material/).
From the repository root:

```bash
pip install -r requirements-docs.txt
mkdocs serve
```

Open <http://127.0.0.1:8000>. MkDocs reloads pages as you edit them.

Changes to `docs/**` and `mkdocs.yml` are deployed to GitHub Pages after they are
merged into `main`.
