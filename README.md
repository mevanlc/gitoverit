# gitoverit

`gitoverit` runs Git-oriented status views over repositories and branches.

The package stays named `gitoverit`, and installs three direct commands:

- `gitoverit` - recursively find repositories and print a repo status table
- `gito` - same repo status command as `gitoverit`
- `gitob` - show local branches inside one repository, including linked worktree occupancy

## Install

```bash
uv tool install .
```

For development:

```bash
uv tool install -e --force .
```

## Repo Status

```bash
gitoverit [OPTIONS] [DIRS]...
gito [OPTIONS] [DIRS]...
```

If no directory is given, the current directory is used.

Common options:

```text
-f, --fetch                Run git fetch --all before inspection.
    --pull-safe            Fetch first, then fast-forward only safe clean repos.
-o, --format {table,json}  Output format. Default: table.
-d, --dirty-only           Hide repos without uncommitted changes.
-s, --sort FIELD           Sort by dir, path, name, status, branch, remote, url, mtime, ident, author, or none.
-r, --reverse              Reverse sort order.
-j, --jobs N               Worker count. Omit for auto; 0 for sequential.
-a, --table-algo {cell,char}
-c, --columns SPEC         Add/remove/reset table columns.
-w, --where EXPR           Filter rows by expression.
-p, --print EXPR           Print one expression result per repo.
-0, --print0               Use NUL delimiters with --print.
    --errors               Print collected errors after output.
    --no-progress          Suppress interactive progress.
    --help-where           Show expression reference.
```

Examples:

```bash
gitoverit ~/projects
gito ~/projects -d -s author
gito ~/projects -f -o json
gito ~/projects -w 'branch != "main" and ahead > 0'
gito ~/projects -w dirty -p path -0 | xargs -0 -n1 echo
```

## Branch Status

```bash
gitob [OPTIONS] [REPO_DIR]
```

If no repository directory is given, the current directory is used.

Common options:

```text
-f, --fetch                Run git fetch --all --prune first.
-j, --json                 Output JSON instead of a table.
-g, --glob PATTERN         Include branches matching a glob.
-r, --regex PATTERN        Include branches matching a regex.
-s, --sort FIELD           Sort by branch, date, author, ahead, behind, status, upstream, current, head, subject, worktree, gone, or none.
    --reverse              Reverse sort order.
-p, --print EXPR           Print one expression result per branch.
-0, --print0               Use NUL delimiters with --print.
-a, --table-algo {cell,char}
-c, --columns SPEC         Add/remove/reset table columns.
-w, --where EXPR           Filter rows by expression.
    --help-where           Show expression reference.
```

Examples:

```bash
gitob .
gitob ~/projects/gitoverit --json
gitob -g 'feature/*'
gitob -w 'gone or ahead > 0'
gitob -p branch -0 | xargs -0 -n1 git branch -D
```

## Development

Tasks are uv-managed:

```bash
uv run tasks/test
uv run tasks/check
```
