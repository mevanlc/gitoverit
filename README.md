# gitoverit

A CLI that walks directories, finds Git repositories, and prints a status summary as a table or JSON.
Also includes a handy Python-like expression language for filtering repos.

![](https://i.imgur.com/Ffvx8GI.png)

## Install

```bash
uv tool install .
```

To pick up local code changes when the version hasn't been bumped:

```bash
uv tool install --force --reinstall .
```

For development (editable install):

```bash
uv tool install -e --force .
```

## Usage

```
gitoverit [OPTIONS] [DIRS...]
```

If no directory is given, the current directory is used. Run with `-h` for the option list.

### Options

```
-f, --fetch                Run `git fetch --all` for each repo before inspection.
-o, --format {table,json}  Output format. Default: table.
-d, --dirty-only           Hide repos with no uncommitted changes.
-s, --sort {mtime,author,none}
                           Sort by latest worktree mtime (default), committer
                           identity, or disable sorting.
-r, --reverse              Reverse the active sort order.
-j, --jobs N               Worker count. Omit for auto-detect; 0 for sequential.
-a, --table-algo {cell,char}
                           Column-width algorithm for the table renderer.
-c, --columns SPEC         Add/remove/reset columns. See "Columns" below.
-w, --where EXPR           Filter rows by an expression. See `--help-where`.
-p, --print EXPR           Evaluate EXPR per repo and print the result, one
                           per line. Replaces table/JSON output.
-0, --print0               With --print, separate results with NUL bytes
                           instead of newlines.
    --errors               Print error tracebacks to stderr after output.
    --no-progress          Suppress the progress bar even on a TTY.
    --help-where           Show full reference for --where / --print.
```

### Columns

`--columns` takes a comma-separated spec. A bare name adds a column,
`-name` removes one, and a single `-` clears all columns first so the
remainder of the spec defines the full set.

Available columns: `dir`, `status`, `branch_remote`, `branch`, `remote`,
`url`, `mtime`, `ident`.

```bash
# Drop the URL column
gitoverit ~/projects -c -url

# Show only dir and status
gitoverit ~/projects -c -,dir,status
```

### Examples

```bash
# Scan the current directory
gitoverit .

# Scan multiple roots
gitoverit ~/projects ~/work

# Sequential mode
gitoverit ~/projects -j 0

# 4 workers
gitoverit ~/projects -j 4

# Dirty repos only, sorted by author
gitoverit ~/projects -d -s author

# Fetch first, then output JSON
gitoverit ~/projects -f -o json

# Repos on a non-main branch with unpushed commits
gitoverit ~/projects -w 'branch != "main" and ahead > 0'

# Print absolute paths of dirty repos, NUL-delimited (xargs-friendly)
gitoverit ~/projects -w dirty -p path -0 | xargs -0 -n1 echo
```

## Filtering and printing

`--where` and `--print` share an expression language (sandboxed via
`simpleeval`). Variables include `path`, `dir`, `status`, `branch`,
`remote`, `url`, `ident`, `mtime`, `dirty`, `ahead`, `behind`,
`modified`, `untracked`, and `deleted`. String variables expose `.rx()`
and `.rxi()` for regex matching.

Run `gitoverit --help-where` for the full reference and more examples.

## Parallelism

Repositories are analyzed in a `ThreadPoolExecutor`; threads are a good
fit because per-repo work is dominated by `git` subprocess I/O. Discovery
streams into the pool so workers start immediately.

The default worker count is `cpu_count - 1`, capped at 8. Override with
`-j N`, or use `-j 0` to run on the main thread (useful for debugging).

## Progress and TTY behavior

When stdout is a TTY, a Rich progress bar is shown: an indeterminate
"Discovery" phase followed by a determinate "Statusing" bar. Pass
`--no-progress` to suppress it. When stdout is not a TTY, no progress
output is emitted.

To implement a custom progress reporter, see `HookProtocol` in
`src/gitoverit/progress.py`.

## Development

See `AGENTS.md` for an orientation to the code layout and conventions,
and `CONTRIBUTING.md` for setup and the PR workflow.
