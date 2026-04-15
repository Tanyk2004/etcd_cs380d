#!/bin/bash
# analyze_scenario.sh - wrapper that calls analyze_results.py for a single scenario dir

set -e

SCENARIO_DIR=$1
SCENARIO_JSON=$2
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

PYTHON="${VENV_PYTHON:-/home/tanay/cs380d/venv/bin/python3}"
"$PYTHON" "$SCRIPT_DIR/analyze_results.py" "$SCENARIO_DIR" "$SCENARIO_JSON"
