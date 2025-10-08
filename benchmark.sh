#!/bin/bash
# Benchmark script to compare sequential vs parallel gitoverit

echo "Benchmarking gitoverit on ~/p/my directory"
echo "=========================================="
echo ""

# Sequential timing
echo "Sequential mode:"
time gitoverit ~/p/my --format json > /dev/null 2>&1
echo ""

# Parallel timing
echo "Parallel mode:"
time gitoverit ~/p/my --parallel --format json > /dev/null 2>&1
echo ""

# Parallel with different worker counts
echo "Parallel mode with 2 workers:"
time gitoverit ~/p/my --parallel --workers 2 --format json > /dev/null 2>&1
echo ""

echo "Parallel mode with 4 workers:"
time gitoverit ~/p/my --parallel --workers 4 --format json > /dev/null 2>&1
echo ""

echo "Parallel mode with 8 workers:"
time gitoverit ~/p/my --parallel --workers 8 --format json > /dev/null 2>&1