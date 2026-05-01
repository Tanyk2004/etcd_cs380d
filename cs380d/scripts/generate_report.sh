#!/bin/bash
# generate_report.sh - aggregate analysis.json files from all scenarios into a final summary

RESULTS_DIR=$1

if [ -z "$RESULTS_DIR" ]; then
    echo "Usage: $0 <results-dir>" >&2
    exit 1
fi

echo ""
echo "============================================================"
echo "FINAL REPORT: $RESULTS_DIR"
echo "============================================================"
printf "%-30s %-6s %-10s %-12s %-12s\n" "SCENARIO" "STATUS" "ELECTIONS" "P99_LAT_MS" "SUCCESS_RATE"
echo "------------------------------------------------------------"

pass=0
fail=0

for analysis in "$RESULTS_DIR"/*/analysis.json; do
    [ -f "$analysis" ] || continue

    name=$(jq -r '.scenario'        "$analysis")
    elections=$(jq -r '.elections'  "$analysis")
    p99=$(jq -r '.p99_latency_ms'   "$analysis")
    rate=$(jq -r '.success_rate'    "$analysis")
    all_pass=$(jq -r '(.elections_pass and .latency_pass and .success_pass)' "$analysis")

    if [ "$all_pass" = "true" ]; then
        status="PASS"
        pass=$((pass + 1))
    else
        status="FAIL"
        fail=$((fail + 1))
    fi

    printf "%-30s %-6s %-10s %-12s %-12s\n" "$name" "$status" "$elections" "$p99" "$rate"
done

echo "------------------------------------------------------------"
echo "Total: $((pass + fail)) | Passed: $pass | Failed: $fail"
echo ""

# Write machine-readable summary
summary_file="$RESULTS_DIR/summary.json"
jq -s '{
    total: length,
    passed: (map(select(.elections_pass and .latency_pass and .success_pass)) | length),
    failed: (map(select(.elections_pass and .latency_pass and .success_pass) | not) | length),
    scenarios: .
}' "$RESULTS_DIR"/*/analysis.json > "$summary_file" 2>/dev/null || true

echo "Summary written to $summary_file"

# Generate PDF + PNG plots
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${VENV_PYTHON:-/home/tanay/cs380d/venv/bin/python3}"
if "$PYTHON" "$SCRIPT_DIR/plot_results.py" "$RESULTS_DIR" 2>&1; then
    echo "PDF report: $RESULTS_DIR/report.pdf"
else
    echo "WARNING: plot_results.py failed — run manually:"
    echo "  python3 $SCRIPT_DIR/plot_results.py $RESULTS_DIR"
fi
