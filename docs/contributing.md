# Contributing

Read the [contributing guide](https://github.com/OrbFrontend/Orb/blob/main/CONTRIBUTING.md)
before opening a pull request.

Use [GitHub Discussions](https://github.com/OrbFrontend/Orb/discussions) for
questions, ideas, and help requests. Use issues for confirmed bugs and focused
feature requests.

## Extending a fork

The [contributing guide's extension map](https://github.com/OrbFrontend/Orb/blob/main/CONTRIBUTING.md#where-to-extend)
points to the code and checks for each kind of change. Read
[Backend layers and prompting](architecture/prompting.md) for dependency rules,
[Secondary workflows](architecture/secondary-workflow.md) for optional features,
and [Database init and upgrades](architecture/database.md#changing-the-schema)
before adding persisted fields.

## Build the documentation locally

The wiki uses [MkDocs Material](https://squidfunk.github.io/mkdocs-material/).
From the repository root:

```bash
pip install -r requirements-docs.txt
mkdocs serve
```

Open <http://127.0.0.1:8000>. MkDocs reloads pages as you edit them.

Run `mkdocs build --strict` before submitting; CI checks documentation changes
with the same command.

Changes to `docs/**` and `mkdocs.yml` are deployed to GitHub Pages after they are
merged into `main`.
