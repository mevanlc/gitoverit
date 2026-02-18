# gitoverit

`gitoverit` is a CLI helper that walks directories, finds non-submodule Git repositories, and summarizes their state in a Rich-powered table or JSON output.

By default, gitoverit uses parallel processing to analyze multiple repositories concurrently, providing ~2.5x speedup over sequential analysis.

## Usage

```
gitoverit [OPTIONS] <DIRS...>
```

### Options

- `--fetch` — fetch from configured remotes before collecting status.
- `--format {table,json}` — choose between a Rich table (default) or JSON payload.
- `--dirty-only` — hide repositories that are completely clean.
- `--sort {mtime,author,none}` — sort by newest modification time (default), committer ident, or disable sorting.
- `--reverse` — flip the selected sort order.
- `--parallel N` / `-p N` — control parallel processing:
  - Not specified (default): auto-detect optimal worker count
  - `0`: sequential mode (main thread only)
  - `N > 0`: use exactly N worker processes
- `--errorfmt {ignore,short,long}` — control how repo analysis errors are displayed.
- `--table-algo {cell,char}` / `-a {cell,char}` — choose the table width algorithm (ignored for `--format json`).

### Examples

```bash
# Scan current directory with auto-detected parallelism
gitoverit .

# Scan multiple directories
gitoverit ~/projects ~/work

# Use sequential mode (no parallelism)
gitoverit ~/projects --parallel 0

# Use exactly 4 workers
gitoverit ~/projects --parallel 4

# Show only dirty repos, sorted by author
gitoverit ~/projects --dirty-only --sort author

# Fetch before scanning, output as JSON
gitoverit ~/projects --fetch --format json

# Use "minimize truncated chars" table layout
gitoverit ~/projects --table-algo char
```

## Install (uv tool)

```bash
uv tool install .
```

If you re-run `uv tool install .` after changing the code but keeping the same version number, uv may keep the existing tool environment. In that case use:

```bash
uv tool install --force --reinstall .
```

For development, you can install in editable mode so changes are reflected without reinstalling:

```bash
uv tool install -e --force .
```

## Performance

gitoverit uses ProcessPoolExecutor with streaming discovery for optimal performance:
- Repositories are submitted to worker processes immediately as discovered
- Workers analyze repos concurrently while directory traversal continues
- Typical speedup: ~2.5x faster than sequential on multi-core systems

## Progress Output

Progress output uses Rich when run in a TTY, showing:
- Discovery phase: indeterminate progress while finding repos
- Processing phase: determinate progress bar with completion status

Implement the `HookProtocol` in `src/gitoverit/progress.py` if you need a custom progress reporter.

## Help

When no arguments are supplied the help text is shown (instead of running against the current directory). Use `-h` or `--help` for the usage synopsis.
