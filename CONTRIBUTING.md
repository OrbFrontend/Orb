# Contributing to Orb

Thanks for wanting to contribute. Here's what to do before opening a PR.

## 1. Get things running locally

Make sure the feature or fix works first.

Start the backend with `./run_unix.sh` (or `run_windows.bat` on Windows). Python 3.11+ required.

### Optional: Auto-formatting on commit

Run `npm ci` (requires Node.js) to install frontend tools from the committed lockfile and set up git hooks via Lefthook. The hooks format staged files before each commit:

- **Python** — Ruff (import sorting, formatting, and linting). The hook calls `python3 -m ruff`, so commit with the `.venv` the scripts create activated.
- **JavaScript** — Biome (formatting)

Keep `package.json` and `package-lock.json` together when changing dependencies.
Use `npm install --save-dev --save-exact <package>@<version>` for an intentional
update, review the lockfile, and verify it with `npm ci`. CI and the formatting
and lint scripts install from the same lockfile.

Ruff and Biome read their settings from `ruff.toml` and `biome.json`, so an editor's format-on-save produces the same output as the hooks and CI.

### Where to extend

[Where to Extend](docs/architecture/extending.md) maps each kind of change to the
code and contract that own it. [AGENTS.md](AGENTS.md) lists the dependency rules.

## 2. Run the checks

Everything lives in `scripts/`. The scripts create and sync `.venv` themselves. Run them before you push:

- **Tests** - `./scripts/tests.sh all`
- **Format** - `./scripts/format_backend.sh` and `./scripts/format_frontend.sh`
- **Lint** - `./scripts/lint.sh` (Ruff, Pyright, the backend and frontend layer checks, Biome, and the frontend unit tests)
- **Compatibility** - `./scripts/compatibility_test.sh`
- **Security** - `./scripts/security_check.sh` (pip-audit and Bandit)

If any of these fail, fix it before submitting. CI runs the tests, format, and lint checks.

For a focused backend run, use `./scripts/tests.sh tests/unit/test_example.py`;
`PYTEST_WORKERS=0` disables parallel workers when debugging. Add regression
coverage for changed behavior; [Where to Extend](docs/architecture/extending.md#tests)
describes the shared fixtures.

For documentation changes, install `requirements-docs.txt` into the development
venv and run `python -m mkdocs build --strict`.

## 3. Open a PR

- Keep it focused. One feature or fix per PR.
- Write a summary in the PR description that explains the what and the why.
- Link any related issues.

## 4. AI-assisted contributions

AI-generated code is welcome, but it needs extra scrutiny. If you're using an AI coding tool (Claude Code, Codex, Cursor, etc.), point it at `AGENTS.md` at the repo root first. It holds the layer rules and project conventions these tools need to produce correct code, and points to the architecture notes in `docs/architecture/` for prompt assembly, streaming, and the database.

## 5. Quick rules

- Small models first. If a feature doesn't work on something like Gemma 4 26B4A, it probably doesn't belong here.
- Only use agentic functionalities when absolutely needed - we will not have useless tools like `dice_roll`
- Keep the agent's scope tight - less freedom, fewer hallucinations.
- If something can be done with an algorithm, don't use an LLM for it.
- AI-generated code is accepted. It will be manually reviewed just like human written code. But must be subjected to more testing.
- What about support for other languages? => The repo is optimized for English only, especially the tts and detection algorithms. You'd probably wanna fork if your use case is non-English. Can't support grammar for every language under the sun.

## 6. Vision

- This is a writing/RP frontend and it will not pretend to be anything else. For general assistant tasks, use proper frontends like llama-ui or Open-WebUI.
- There will not be support for code execution, file browsing, web uploads, etc. basically anything that opens up an RCE attack surface that may compromise the user's machine and personal data.
- Orb's workflow is a chat frontend, we will not bloat it into a pseudo game engine with arbitrarily complex features that require learning (e.g. locations, maps, global stat tracking etc.).
