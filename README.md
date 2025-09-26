# gitoverit

`gitoverit` is a CLI helper that walks directories, finds non-submodule Git repositories, and summarizes their state in a Rich-powered table or JSON output.

## Usage

```
gitoverit [OPTIONS] <DIRS...>
```

- `--fetch` &mdash; fetch from configured remotes before collecting status.
- `--format {table,json}` &mdash; choose between a Rich table (default) or JSON payload.
- `--dirty-only` &mdash; hide repositories that are completely clean.
- `--sort {mtime,author,none}` &mdash; sort by newest modification time (default), committer ident, or disable sorting.
- `--reverse` &mdash; flip the selected sort order.

When no arguments are supplied the help text is shown (instead of running against the current directory). Use `-h` or `--help` for the usage synopsis.
