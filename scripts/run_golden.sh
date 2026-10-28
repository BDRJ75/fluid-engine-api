#!/usr/bin/env bash
# Offline golden regression tests for the SPH engine (no DB needed).
# Usage: ./scripts/run_golden.sh            # compare
#        ./scripts/run_golden.sh --update-golden   (or UPDATE_GOLDEN=1) # regenerate
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -m pytest -q tests/test_golden_sph.py "$@"
