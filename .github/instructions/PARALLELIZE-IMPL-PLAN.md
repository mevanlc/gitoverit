# gitoverit Parallelization Implementation Plan

## Overview
Transform gitoverit from sequential to parallel repository processing using ProcessPoolExecutor, improving performance for large repository collections while keeping changes minimal and pragmatic.

## Phase 1: Simplify Progress Tracking

### 1.1 Streamline HookProtocol
**File**: `src/gitoverit/progress.py`
- [ ] Remove `HookReturn` enum and flow control via return values
- [ ] Simplify `HookProtocol` to just progress notifications (no control flow)
- [ ] Update `dispatch_hook` to not check return values
- [ ] Keep `RichHook` but adapt it to simpler interface

### 1.2 Keep Discovery in reporting.py
**Why**: The `discover_repositories` function is already well-placed and only ~20 lines. No need to move it.
- [ ] Keep `discover_repositories` in `reporting.py`
- [ ] Add simple progress callback parameter instead of full hook
- [ ] Remove the `dispatch_hook` calls, use direct callbacks

### 1.3 No Changes Needed to analyze_repository
**Note**: `analyze_repository` is already pickle-friendly!
- It accepts only `Path` and `bool` parameters (both pickleable)
- It creates the `Repo` object inside the function
- It returns a `RepoReport` dataclass (pickleable)
- Exceptions will be automatically captured by concurrent.futures

No wrapper function needed - we can pass `analyze_repository` directly to the process pool!

## Phase 2: Add Parallel Processing

### 2.1 Add Parallel Collection Function
**File**: `src/gitoverit/reporting.py` (add to existing file)
```python
import os
from concurrent.futures import ProcessPoolExecutor, as_completed

def get_worker_count(user_override: int | None = None) -> int:
    """Simple worker count logic"""
    if user_override is not None:
        return max(1, user_override)

    cpu_count = os.cpu_count() or 1
    if cpu_count <= 2:
        return cpu_count
    else:
        return min(cpu_count - 1, 8)

def collect_reports_parallel(
    dirs: Iterable[Path],
    *,
    fetch: bool,
    dirty_only: bool,
    hook: HookProtocol | None = None,
    max_workers: int | None = None,
) -> list[RepoReport]:
    """Parallel version of collect_reports"""

    # Phase 1: Discovery (still serial, it's I/O bound anyway)
    repo_paths: list[Path] = []
    for repo_path in discover_repositories(dirs):
        if hook:
            hook.discovering(repo_path)
        repo_paths.append(repo_path)

    if not repo_paths:
        return []

    # Phase 2: Parallel processing
    reports: list[RepoReport] = []
    error_count = 0

    if hook:
        hook.start_collect(len(repo_paths))

    with ProcessPoolExecutor(max_workers=get_worker_count(max_workers)) as executor:
        # Submit all work - just pass analyze_repository directly!
        futures = {
            executor.submit(analyze_repository, path, fetch): path
            for path in repo_paths
        }

        # Collect results as they complete
        for completed, future in enumerate(as_completed(futures), 1):
            path = futures[future]

            try:
                report = future.result()  # Will raise if worker raised
                if not (dirty_only and not report.dirty):
                    reports.append(report)
            except Exception as e:
                # Worker raised an exception - log it but continue
                error_count += 1
                # Could log: print(f"Failed to analyze {path}: {e}")

            if hook:
                hook.collecting(completed, path)
                # Update hook.error_count if we added that field

    if hook:
        hook.done()

    return reports
```

### 2.2 Adapt RichHook for Parallel Mode
**File**: `src/gitoverit/progress.py` (minimal changes)
```python
# Just update RichHook to track errors
class RichHook(HookProtocol):
    # ... existing code ...
    def __init__(self, console: Console) -> None:
        # ... existing init code ...
        self.error_count = 0  # Add error tracking

    # ... keep existing methods ...

    def collecting(self, index: int, path: Path) -> HookReturn:
        # Update to show error count if any
        if self.gather_task_id is not None:
            display_name = path.name or str(path)
            error_text = f" [{self.error_count} errors]" if self.error_count > 0 else ""
            description = (
                f"[cyan]Processing ({index}/{self.total_to_collect}): {display_name}{error_text}"
            )
            self.progress.update(
                self.gather_task_id,
                advance=1,
                description=description,
            )
        return HookReturn.CONTINUE
```

## Phase 3: Integration

### 3.1 Update CLI
**File**: `src/gitoverit/cli.py`
```python
@APP.command()
def cli(
    # ... existing parameters ...
    parallel: bool = typer.Option(
        False,
        "--parallel", "-p",
        help="Use parallel processing (experimental)"
    ),
    workers: Optional[int] = typer.Option(
        None,
        "--workers", "-w",
        help="Number of parallel workers (default: auto-detect, only with --parallel)"
    ),
) -> None:
    """Minimal CLI changes - add opt-in parallel flag"""

    hook = RichHook(console) if _stdout_is_tty() else None

    # Choose sequential or parallel based on flag
    if parallel:
        reports = collect_reports_parallel(
            dirs,
            fetch=fetch,
            dirty_only=dirty_only,
            hook=hook,
            max_workers=workers
        )
    else:
        reports = collect_reports(
            dirs,
            fetch=fetch,
            dirty_only=dirty_only,
            hook=hook
        )

    # Rest stays exactly the same
    _sort_reports(reports, sort=sort, reverse=reverse)

    if output_format is OutputFormat.JSON:
        typer.echo(render_json(reports))
    else:
        render_table(console, reports)
```

## Phase 4: Testing & Optimization

### 4.1 Test with Existing Test Suite
- [ ] Ensure all existing tests pass with sequential mode
- [ ] Add simple test for parallel mode:

**File**: `tests/test_gitoverit.py` (add to existing)
```python
def test_parallel_mode_basic():
    """Basic smoke test that parallel mode works"""
    # Test that collect_reports_parallel returns same results as sequential
    # Just verify it runs without crashing initially
```

### 4.2 Manual Testing Script
Create simple benchmark to verify improvement:
```python
# test_parallel.py (local testing script, not committed)
import time
from pathlib import Path
from gitoverit.reporting import collect_reports, collect_reports_parallel

# Test on a directory with many repos
test_dir = Path.home() / "projects"  # or wherever you have many repos

print("Testing sequential...")
start = time.perf_counter()
seq_reports = collect_reports([test_dir], fetch=False, dirty_only=False, hook=None)
seq_time = time.perf_counter() - start

print("Testing parallel...")
start = time.perf_counter()
par_reports = collect_reports_parallel([test_dir], fetch=False, dirty_only=False, hook=None)
par_time = time.perf_counter() - start

print(f"\nResults:")
print(f"Sequential: {len(seq_reports)} repos in {seq_time:.2f}s")
print(f"Parallel:   {len(par_reports)} repos in {par_time:.2f}s")
print(f"Speedup:    {seq_time/par_time:.2f}x")
```

## Phase 5: Future Improvements (Not Part of Initial Implementation)

- Stream processing (process repos as discovered)
- Better error reporting/recovery
- Result caching
- Make parallel the default after testing period

## Implementation Approach

### Start Simple
1. **Add `collect_reports_parallel()` alongside existing code**
2. **Use `--parallel` flag for opt-in testing**
3. **Keep all existing code unchanged initially**
4. **Gradually refactor after parallel version is stable**

### Key Implementation Details

**Serialization**:
- `analyze_repository` already accepts only `Path` and `bool` (both pickleable)
- Creates `Repo` objects inside worker process (already does this)
- Returns serializable `RepoReport` dataclass (already does this)
- **No wrapper needed** - can use existing function directly!

**Error Handling**:
- Let exceptions bubble up naturally in workers
- concurrent.futures captures them automatically
- Handle with try/except around `future.result()`
- Continue processing other repos even if some fail

**Progress**:
- Keep using existing `RichHook` with minor updates
- Use `as_completed()` to update progress in real-time

## Success Criteria

- [ ] 2-4x speedup on 20+ repositories
- [ ] No crashes or hangs
- [ ] Progress bar still works smoothly
- [ ] Ctrl-C cleanly terminates
- [ ] Existing tests still pass

## Estimated Work

- **Phase 1**: 1 hour - Simplify hook protocol
- **Phase 2**: 2-3 hours - Add parallel function
- **Phase 3**: 1 hour - CLI integration
- **Phase 4**: 1 hour - Testing

**Total: ~5-6 hours for working implementation**

## Notes

This plan prioritizes:
- **Minimal changes** to existing code
- **Opt-in** parallel mode for safe testing
- **Pragmatic** solutions over perfect architecture
- **Working code** over elaborate abstractions

The focus is getting a working parallel version quickly, then iterating based on real-world usage.