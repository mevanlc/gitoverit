# Contributing to gitoverit

Thanks for your interest in contributing. Bug reports, feature ideas, and pull requests are all welcome.

## Reporting issues

Before opening an issue, please search [existing issues](https://github.com/mevanlc/gitoverit/issues) to avoid duplicates. When filing a new one, use the bug report or feature request template and include the information it asks for.

For bug reports, the most useful details are:
- Your OS and Python version (`python --version`)
- The version of gitoverit you're running (`gitoverit --help` shows installed paths)
- The exact command you ran and the output you got
- What you expected to happen instead

## Development setup

gitoverit uses [`uv`](https://docs.astral.sh/uv/) for dependency management.

```bash
git clone https://github.com/mevanlc/gitoverit.git
cd gitoverit
uv sync
```

Install the CLI in editable mode so local changes are picked up:

```bash
uv tool install -e --force .
```

## Running checks

The `tasks/` directory contains lightweight scripts for common workflows:

```bash
uv run tasks/check    # Pyright type checking
uv run tasks/test     # unittest suite
```

Both should pass before you open a pull request.

## Pull requests

1. Fork the repo and create a topic branch from `main`.
2. Make focused changes — one logical change per PR keeps review fast.
3. Add or update tests when you change behavior. Tests live in `tests/`.
4. Run `uv run tasks/check` and `uv run tasks/test` locally.
5. Update `README.md` if you change user-facing flags or output.
6. Open the PR using the template and link any related issues.

## Code style

- Follow the structure documented in `AGENTS.md` — particularly the split between `reporting.py`, `progress.py`, and `output/`.
- Keep `RepoReport` and worker arguments pickle-friendly (they cross process boundaries).
- Prefer small, composable functions over large ones.

## Questions

If you're unsure whether a change fits the project's scope, open an issue first to discuss before investing time in a PR.
