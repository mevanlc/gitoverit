# Repo Orientation for Future Agents

## Project Shape
- **CLI entrypoint** lives in `src/gitoverit/cli.py`. It wires Typer argument parsing, selects a progress hook, and triggers report collection/rendering.
- **Core Git inspection** resides in `src/gitoverit/reporting.py`. Key functions:
  - `collect_reports()` – convenience wrapper that calls `collect_reports_parallel` with `max_workers=0` (sequential).
  - `collect_reports_parallel()` – parallel version using ProcessPoolExecutor with streaming discovery (default mode).
  - `discover_repositories()` – generator that walks directories yielding git repo paths. Skips submodule `.git` files and nested repos that are gitignored by their parent repo.
  - `analyze_repository()` – worker function that analyzes a single repo, returning a `RepoReport`.
  - `RepoReport` dataclass – canonical representation of a repo's status; renderers and tests consume this.
  - Utility functions like `parse_status_porcelain`, `simplify_url`, `latest_worktree_mtime`, etc.
- **Progress hooks** are defined in `src/gitoverit/progress.py`:
  - `HookProtocol` – simple progress notification protocol (no flow control).
  - `RichHook` – implements interactive progress bar with transition from indeterminate (discovery) to determinate (processing).
- **Output formatting** split under `src/gitoverit/output/`:
  - `table.py` renders Rich tables via `render_table(console, reports)`.
  - `json.py` exposes `render_json(reports)` returning a JSON string.

## Discovery Filtering
During directory walking, `discover_repositories()` applies these filters:
- **Submodules**: Detected by `.git` being a file (not directory) containing `"modules"`. Skipped entirely, no descent.
- **Gitignored nested repos**: When a repo is found inside another discovered repo, `git check-ignore -q` is run from the parent repo. If the nested path is ignored, it is skipped and descent is pruned.
- **Deduplication**: A `seen` set of resolved paths prevents yielding the same repo twice (handles symlinks).

## Parallelization Architecture
- **Default mode**: Parallel processing with auto-detected worker count using ProcessPoolExecutor.
- **Streaming discovery**: Repos are submitted to the worker pool immediately as discovered, maximizing CPU utilization.
- **Worker pool sizing**: Auto-detects optimal count (cpu_count - 1, capped at 8), or uses `--parallel N` override.
- **Sequential fallback**: Use `--parallel 0` to force main-thread-only processing.
- **Results collection**: Main thread collects completed futures via `wait(FIRST_COMPLETED)`.

## Workflows
- Tasks are managed through lightweight scripts in `tasks/`. Run them with `uv run tasks/<name>`.
  - `tasks/check` executes Pyright (type checking).
  - `tasks/test` runs the unittest suite.
- CLI: `gitoverit [OPTIONS] DIRS...`
  - `-f`/`--fetch` – run `git fetch --all` per repo before inspection.
  - `-d`/`--dirty-only` – only show repos with local or remote changes.
  - `-o`/`--format` – output format: `table` (default) or `json`.
  - `-s`/`--sort` – sort by `mtime` (default), `author`, or `none`.
  - `-r`/`--reverse` – reverse sort order.
  - `-p`/`--parallel` – worker count: None (auto), 0 (sequential), N (explicit).
  - `-a`/`--table-algo` – column width algorithm: `cell` (default) or `char`.
- Progress UI only activates when stdout is a TTY. Non-interactive runs skip hooks entirely.

## Conventions & Tips
- Treat repositories as read-only: inspection should avoid mutating `.git/` or worktrees.
- `RepoReport` is pickle-friendly for ProcessPoolExecutor – avoid attaching live objects.
- `analyze_repository()` is the worker function – must accept only pickleable args (Path, bool).
- When extending progress reporting, implement `HookProtocol`; no flow control (methods return None).
- For new output formats, hook into `collect_reports_parallel` and add a module under `output/`.
- Tests live in `tests/`, targeting helpers in `reporting.py`. Prefer temporary directories with real `Repo` instances for Git behavior.

Stay within this structure to keep features composable and testable.
