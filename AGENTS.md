# Repo Orientation for Future Agents

## Project Shape
- CLI entrypoint lives in `src/gitoverit/cli.py`. It wires Typer argument parsing, selects a progress hook, and triggers report collection/rendering.
- Core Git inspection logic resides in `src/gitoverit/reporting.py`. Public helpers:
  - `collect_reports()` – orchestrates repository discovery, analysis, and invokes optional progress hooks.
  - `RepoReport` dataclass – canonical representation of a repo’s status; renderers and tests consume this.
  - Utility functions like `parse_status_porcelain`, `simplify_url`, `latest_worktree_mtime`, etc., are here.
- Progress hooks are defined in `src/gitoverit/progress.py`. `RichHook` implements the interactive progress bar; use `dispatch_hook()` to safely invoke `HookProtocol` methods (return `HookReturn.STOP` to halt early).
- Output formatting is split under `src/gitoverit/output/`:
  - `table.py` renders Rich tables via `render_table(console, reports)`.
  - `json.py` exposes `render_json(reports)` returning a JSON string.

## Workflows
- Tasks are managed through lightweight scripts in `tasks/`. Run them with `uv run tasks/<name>`.
  - `tasks/check` executes Pyright (type checking).
  - `tasks/test` runs the unittest suite.
- CLI expectations: `gitoverit --sort <mode> [--reverse] DIRS...`. Default sort is modification time; JSON output includes the `latest_mtime` field.
- Progress UI only activates when stdout is a TTY. Non-interactive runs skip hooks entirely.

## Conventions & Tips
- Treat repositories as read-only: inspection should avoid mutating `.git/` or worktrees.
- `RepoReport` should stay serialization-friendly—avoid attaching live objects.
- When extending progress reporting, implement `ProgressHookProtocol`; returning `HookReturnAction.STOP` halts discovery/collection early.
- For new output formats, hook into `collect_reports` and add a module under `output/`; keep CLI selection logic minimal.
- Tests live in `tests/`, targeting helpers in `reporting.py`. Prefer temporary directories with real `Repo` instances for Git behavior.

Stay within this structure to keep features composable and testable.
