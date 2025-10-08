#!/usr/bin/env python3
"""Quick benchmark to test parallel vs sequential performance"""

import time
from pathlib import Path
# Import directly to avoid typer dependency
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

# Import reporting module directly (avoiding __init__ which needs typer)
import importlib.util
spec = importlib.util.spec_from_file_location("reporting", "src/gitoverit/reporting.py")
reporting = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporting)

collect_reports = reporting.collect_reports
collect_reports_parallel = reporting.collect_reports_parallel

# Test on a directory with many repos
test_dir = Path.home() / "p" / "my"

print(f"Testing on directory: {test_dir}")
print(f"This directory should contain multiple git repositories\n")

# Sequential test
print("Testing sequential mode...")
start = time.perf_counter()
seq_reports = collect_reports([test_dir], fetch=False, dirty_only=False, hook=None)
seq_time = time.perf_counter() - start
print(f"Sequential: Found {len(seq_reports)} repos in {seq_time:.2f}s")

# Parallel test
print("\nTesting parallel mode...")
start = time.perf_counter()
par_reports = collect_reports_parallel([test_dir], fetch=False, dirty_only=False, hook=None)
par_time = time.perf_counter() - start
print(f"Parallel:   Found {len(par_reports)} repos in {par_time:.2f}s")

# Results
print(f"\n{'='*50}")
print(f"Results:")
print(f"  Sequential: {len(seq_reports)} repos in {seq_time:.2f}s")
print(f"  Parallel:   {len(par_reports)} repos in {par_time:.2f}s")
if par_time > 0:
    print(f"  Speedup:    {seq_time/par_time:.2f}x")

# Verify same results
seq_paths = {r.path for r in seq_reports}
par_paths = {r.path for r in par_reports}

if seq_paths == par_paths:
    print("\n✓ Both modes found the same repositories")
else:
    print("\n✗ Different repositories found!")
    print(f"  Only in sequential: {seq_paths - par_paths}")
    print(f"  Only in parallel: {par_paths - seq_paths}")