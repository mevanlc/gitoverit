# gitoverit Parallelization

## Overview
gitoverit uses ProcessPoolExecutor for parallel repository analysis, achieving ~2.5x speedup over sequential processing. Parallel mode is the default.

## Architecture

### Streaming Discovery + Parallel Processing
Repository discovery and analysis run concurrently:
1. **Discovery phase**: `discover_repositories()` walks directories, yielding repo paths
2. **Immediate submission**: Each discovered repo is submitted to the worker pool immediately
3. **Parallel analysis**: Workers process repos concurrently while discovery continues
4. **Results collection**: `as_completed()` yields results as workers finish

This streaming approach keeps workers busy throughout discovery, avoiding idle time.

### Worker Pool Sizing
```python
def get_worker_count(user_override: int | None = None) -> int:
    if user_override is not None:
        return max(1, user_override)

    cpu_count = os.cpu_count() or 1
    if cpu_count <= 2:
        return cpu_count
    else:
        return min(cpu_count - 1, 8)  # Cap at 8 to avoid overhead
```

### Progress Tracking
`HookProtocol` provides simple progress callbacks (no flow control):
- `discovering(path)` - Called as repos are found (indeterminate phase)
- `start_collect(total)` - Called when discovery completes (transition to determinate)
- `collecting(index, path)` - Called as each analysis completes
- `done()` - Called when all processing finishes

`RichHook` implements this with a progress bar that transitions from indeterminate (discovery) to determinate (processing).

## CLI Usage

```bash
# Default: parallel with auto-detected workers
gitoverit ~/projects

# Sequential mode (0 workers = main thread only)
gitoverit ~/projects --parallel 0

# Explicit worker count
gitoverit ~/projects --parallel 4
```

The `--parallel` / `-p` flag controls parallelization:
- `None` (default): Auto-detect optimal worker count
- `0`: Sequential mode (main thread only)
- `N > 0`: Use N workers

## Performance

Benchmark on 34 repositories:
- **Sequential** (`--parallel 0`): 8.23s
- **Parallel batch** (collect then submit): 6.64s (1.24x speedup)
- **Parallel streaming** (submit as discovered): 3.32s (2.48x speedup)

The streaming optimization provided a 2x improvement over batch parallel by eliminating worker idle time during discovery.

## Implementation Details

### Key Functions

**`collect_reports_parallel()`** - Main parallel orchestrator:
```python
def collect_reports_parallel(
    dirs: Iterable[Path],
    *,
    fetch: bool,
    dirty_only: bool,
    hook: HookProtocol | None = None,
    max_workers: int | None = None,
) -> list[RepoReport]:
    """Parallel version with streaming discovery"""

    with ProcessPoolExecutor(max_workers=get_worker_count(max_workers)) as executor:
        # Stream: submit repos to pool as discovered
        futures: dict[Future[RepoReport], Path] = {}
        for repo_path in discover_repositories(dirs):
            if hook:
                hook.discovering(repo_path)
            future = executor.submit(analyze_repository, repo_path, fetch)
            futures[future] = repo_path

        # Process results as they complete
        if hook:
            hook.start_collect(len(futures))

        for completed, future in enumerate(as_completed(futures), 1):
            path = futures[future]
            try:
                report = future.result()
                # ... handle report
            except Exception:
                # ... handle error

            if hook:
                hook.collecting(completed, path)
```

**`analyze_repository()`** - Worker function (already pickle-friendly):
- Takes only `Path` and `bool` (pickleable primitives)
- Creates `Repo` object inside worker process
- Returns `RepoReport` dataclass (pickleable)
- Exceptions automatically captured by concurrent.futures

### Design Decisions

1. **ProcessPoolExecutor over ThreadPoolExecutor** - Avoids GIL contention for CPU-bound git operations
2. **No wrapper functions** - `analyze_repository()` is already pickle-safe
3. **Streaming over batch** - Submit work immediately as discovered
4. **Simple progress protocol** - Removed flow control (`HookReturn`) complexity
5. **Parallel by default** - Better performance out of the box

## Future Improvements

- **gitstatusd integration** - Cache-based status for sub-100ms subsequent scans
- **Adaptive pool sizing** - Monitor system load and adjust worker count
- **Result caching** - Skip unchanged repos based on mtime/commit SHA
