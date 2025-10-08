# gitoverit: introducing concurrency/parallelization/multiprocessing: draft plan

Brainstorming:

# repo finding
keep it serial (it's mostly I/O bound anyway, but we can revisit that later)
modularizing: move the repo-finding-related functionality to repo_finder.py 

# per-repo processing (after finder has found all the repos)
...or I guess we could kick repos into the concurrency pool as soon we find them -- that's not a bad idea (could actually be a big speed boost). Maybe phase 2.
we'll launch a RepoProcessor task per found repo
  - run fetch on the repo if asked to
  - run the status-info gathering on the repo
  - return results / exception via concurrent future

# HookProtocol
Get rid of it. It's too broad and the callback-can-participate-via-return-values hasn't been useful and probably never will be.
We'll create narrower-purpose callback protocols where appropriate.

```python
class RepoFinderListener(PseudoCodeProtocol😉):
    def started(roots: list[str]) -> None: ...
    def looking_at(self, current_path: str) -> None: ...
    """ pass paths-that-are-repos by path:str or a lightweight dataclass -- not sure yet """
    def repo_found(self, repo_path: str) -> None: ...
    def finished() -> None: ...
```

In the concurrent phase, we'll just monitor progress by counting completed futures.
```python
futures = executor.map(process_repo_task_launcher_fn, repo_paths)
completion_iter = enumerate(concurrent.futures.as_completed(futures), 1)
for n_completed, future in completion_iter:
    try:
        # update progress bar
        processed_repo: ProcessedRepo = future.result()
        # do whatever with processed_repo
    except Exception as e:
        # track exception for later reporting / logging
        # (maybe the progress bar has a slot for that)?
        # we're not using tqdm -- but I remember it has pbar.write() which lets you write a message without messing up the bar
        # it also has pbar.set_postfix() which might be useful for showing "n errors" or something
        # might see if rich progress bar has some similar facilities
```

# Other stuff
I want to start with ProcessPoolExecutor to avoid some threading gotchas.
maybe do n_procs: min 1 if 1 cores, min 2 if 2 cores, else n_procs - 1 


# Agent notes (go here):


