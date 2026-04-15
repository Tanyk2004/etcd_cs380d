#!/bin/bash
# scripts/run_test_suite.sh

set -e

RESULTS_DIR="./results/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$RESULTS_DIR"

# If run via sudo, fix ownership of results so the real user can read them
REAL_USER="${SUDO_USER:-$USER}"
fix_ownership() { chown -R "$REAL_USER:" "$RESULTS_DIR" 2>/dev/null || true; }
trap fix_ownership EXIT

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TRAFFIC_SIM="$REPO_ROOT/etcd-workload-tester/target/release/traffic-sim"
ETCD_ENDPOINTS="http://127.0.0.1:2379,http://127.0.0.1:22379,http://127.0.0.1:32379"
SCENARIOS_FILE="./scenarios/definitions.json"

# Optional: run a single scenario by name
#   sudo bash scripts/run_test_suite.sh --scenario cross_datacenter
FILTER_SCENARIO=""
if [[ "${1:-}" == "--scenario" && -n "${2:-}" ]]; then
    FILTER_SCENARIO="$2"
fi

# Map scenario phase params to the nearest named traffic-sim profile.
map_to_profile() {
    local qps=$1
    local read_ratio=$2
    local value_size=${3:-256}

    # Large-value writes: WAL pressure is the point, not QPS.
    # Use huge-value (512KB) for very large payloads, large-value (64KB) otherwise.
    if (( value_size >= 262144 )); then
        echo "huge-value"; return
    fi
    if (( value_size >= 8192 )); then
        echo "large-value"; return
    fi

    if (( $(echo "$read_ratio >= 0.9" | bc -l) )); then
        echo "read-heavy"; return
    fi
    if (( $(echo "$qps <= 50" | bc -l) )); then
        echo "idle"; return
    fi
    if (( $(echo "$qps <= 300" | bc -l) )); then
        echo "low"; return
    fi
    if (( $(echo "$qps <= 1500" | bc -l) )); then
        echo "medium"; return
    fi
    if (( $(echo "$qps <= 5000" | bc -l) )); then
        if (( $(echo "$read_ratio == 0" | bc -l) )); then
            echo "write-heavy"
        else
            echo "high"
        fi
        return
    fi
    echo "spike"
}

# Scrape a single metric value from node 1. Always prints an integer (0 on miss).
scrape_metric() {
    local metric=$1
    local val
    val=$(curl -s localhost:2379/metrics \
        | grep "^${metric}" | grep -v '#' | awk '{print $2}' | head -1)
    val=$(echo "${val:-0}" | awk '{printf "%d", $1}')
    echo "${val:-0}"
}

# Run N parallel traffic-sim instances for a given profile+duration.
# Instance 1's output is captured to $log_file for later parsing.
# Waits for all instances to finish before returning.
run_parallel_sims() {
    local profile=$1
    local duration=$2
    local n=$3
    local log_file=$4
    local sim_pids=()

    for i in $(seq 1 "$n"); do
        local addr="localhost:$((9191 + i))"
        if [ "$i" -eq 1 ] && [ -n "$log_file" ]; then
            # Capture stdout+stderr from the first instance for stats parsing
            "$TRAFFIC_SIM" \
                --endpoints="$ETCD_ENDPOINTS" \
                --profile="$profile" \
                --duration="$duration" \
                --control-addr="$addr" \
                > "$log_file" 2>&1 &
        else
            "$TRAFFIC_SIM" \
                --endpoints="$ETCD_ENDPOINTS" \
                --profile="$profile" \
                --duration="$duration" \
                --control-addr="$addr" \
                > /dev/null 2>&1 &
        fi
        sim_pids+=($!)
    done

    for pid in "${sim_pids[@]}"; do
        wait "$pid" 2>/dev/null || true
    done
}

# Parse scenarios and run each one
while IFS= read -r scenario; do
    name=$(echo "$scenario" | jq -r '.name')
    echo "=========================================="
    echo "Running scenario: $name"
    echo "=========================================="

    SCENARIO_DIR="$RESULTS_DIR/$name"
    mkdir -p "$SCENARIO_DIR"

    # Apply network conditions
    latency=$(echo "$scenario" | jq -r '.network.latency')
    jitter=$(echo "$scenario"  | jq -r '.network.jitter')
    loss=$(echo "$scenario"    | jq -r '.network.packet_loss')

    echo "Applying network conditions: latency=$latency jitter=$jitter loss=$loss"
    ./scripts/apply_network.sh "$latency" "$jitter" "$loss"

    # Read per-scenario etcd tuning (with defaults)
    election_ms=$(echo "$scenario" | jq -r '.election_timeout_ms // 1000')
    heartbeat_ms=$(echo "$scenario" | jq -r '.heartbeat_interval_ms // 100')

    echo "Resetting cluster (election_timeout=${election_ms}ms heartbeat=${heartbeat_ms}ms)..."
    ELECTION_TIMEOUT_MS="$election_ms" HEARTBEAT_INTERVAL_MS="$heartbeat_ms" \
        ./scripts/reset_cluster.sh
    sleep 5

    # Record initial metrics
    curl -s localhost:2379/metrics > "$SCENARIO_DIR/metrics_start.txt"

    # Start metrics collection in background
    ./scripts/collect_metrics.sh "$SCENARIO_DIR" &
    METRICS_PID=$!

    # Run the workload phases
    phase_num=0
    while IFS= read -r phase; do
        phase_num=$((phase_num + 1))
        phase_type=$(echo "$phase"    | jq -r '.type // "workload"')
        duration=$(echo "$phase"      | jq -r '.duration // "30s"')
        qps=$(echo "$phase"           | jq -r '.qps // 0')
        read_ratio=$(echo "$phase"    | jq -r '.read_ratio // 0')
        parallel=$(echo "$phase"      | jq -r '.parallel_sims // 1')
        value_size=$(echo "$phase"    | jq -r '.value_size // 256')

        # ── Leader-kill phase: force an election directly ─────────────────────
        if [[ "$phase_type" == "leader_kill" ]]; then
            kill_delay=$(echo "$phase"  | jq -r '.kill_delay_s // 5')
            restart_after=$(echo "$phase" | jq -r '.restart_after_s // 10')

            echo "Phase $phase_num [leader_kill]: killing leader in ${kill_delay}s, restart after ${restart_after}s"
            sleep "$kill_delay"

            # Identify the leader by checking etcd_server_is_leader
            killed_name=""; killed_cport=""; killed_pport=""; killed_mport=""; killed_idx=""
            for ninfo in "infra1:2379:12380:9101:1" "infra2:22379:22380:9102:2" "infra3:32379:32380:9103:3"; do
                nn=$(echo "$ninfo" | cut -d: -f1)
                nc=$(echo "$ninfo" | cut -d: -f2)
                np=$(echo "$ninfo" | cut -d: -f3)
                nm=$(echo "$ninfo" | cut -d: -f4)
                ni=$(echo "$ninfo" | cut -d: -f5)
                is_ldr=$(curl -s --max-time 2 "localhost:$nc/metrics" 2>/dev/null \
                    | awk '/^etcd_server_is_leader[^_]/{print $2}')
                if [[ "$is_ldr" == "1" ]]; then
                    killed_name=$nn; killed_cport=$nc; killed_pport=$np
                    killed_mport=$nm; killed_idx=$ni
                    break
                fi
            done
            if [[ -z "$killed_name" ]]; then
                echo "  WARNING: could not identify leader — defaulting to infra1"
                killed_name=infra1; killed_cport=2379; killed_pport=12380
                killed_mport=9101; killed_idx=1
            fi

            echo "  Killing leader $killed_name (client port $killed_cport)..."
            pkill -9 -f "etcd --name $killed_name" 2>/dev/null || true

            # Wait for new leader on surviving nodes
            echo "  Waiting for election..."
            for _ in $(seq 1 30); do
                new_ldr=$(for p in 2379 22379 32379; do
                    [[ "$p" == "$killed_cport" ]] && continue
                    curl -s --max-time 2 "localhost:$p/metrics" 2>/dev/null
                done | awk '/^etcd_server_is_leader[^_]/{if($2+0==1)f=1}END{print f+0}')
                [[ "$new_ldr" == "1" ]] && { echo "  New leader elected."; break; }
                sleep 1
            done

            # Restart the killed node after the grace period
            sleep "$restart_after"
            echo "  Restarting $killed_name..."
            "$REPO_ROOT/bin/etcd" \
                --name "$killed_name" \
                --data-dir "$REPO_ROOT/${killed_name}.etcd" \
                --listen-client-urls "http://127.0.0.1:$killed_cport" \
                --advertise-client-urls "http://127.0.0.1:$killed_cport" \
                --listen-peer-urls "http://127.0.0.1:$killed_pport" \
                --initial-advertise-peer-urls "http://127.0.0.1:$killed_pport" \
                --listen-metrics-urls "http://127.0.0.1:$killed_mport" \
                --initial-cluster-token etcd-cluster-1 \
                --initial-cluster "$CLUSTER" \
                --initial-cluster-state existing \
                --election-timeout="${election_ms}" \
                --heartbeat-interval="${heartbeat_ms}" \
                --max-request-bytes=10485760 \
                --logger=zap --log-outputs=stderr \
                >> "/tmp/etcd${killed_idx}.log" 2>&1 &
            echo "  $killed_name restarted (rejoining cluster)"
            sleep 2
            continue
        fi

        # ── Normal workload phase ─────────────────────────────────────────────
        profile=$(map_to_profile "$qps" "$read_ratio" "$value_size")
        echo "Phase $phase_num: profile=$profile x${parallel} (qps=$qps read_ratio=$read_ratio value_size=$value_size) duration=$duration"

        committed_before=$(scrape_metric 'etcd_server_proposals_committed_total')
        failed_before=$(scrape_metric 'etcd_server_proposals_failed_total')

        tester_log="$SCENARIO_DIR/tester_phase${phase_num}.log"
        run_parallel_sims "$profile" "$duration" "$parallel" "$tester_log"

        committed_after=$(scrape_metric 'etcd_server_proposals_committed_total')
        failed_after=$(scrape_metric 'etcd_server_proposals_failed_total')

        total=$(( committed_after - committed_before ))
        failed=$(( failed_after - failed_before ))
        successful=$(( total - failed ))

        # Parse traffic-sim stats log into ops_stats CSV for throughput plots
        PYTHON="${VENV_PYTHON:-/home/tanay/cs380d/venv/bin/python3}"
        "$PYTHON" "$(dirname "$0")/parse_tester_stats.py" \
            "$tester_log" "$SCENARIO_DIR/ops_phase${phase_num}.csv" 2>/dev/null || true

        cat > "$SCENARIO_DIR/phase_$(date +%s).json" <<EOF
{
  "profile": "$profile",
  "parallel_sims": $parallel,
  "total_operations": $total,
  "successful_operations": $successful,
  "latencies_ms": []
}
EOF
    done < <(echo "$scenario" | jq -c '.phases[]')

    # Stop metrics collection
    kill $METRICS_PID 2>/dev/null || true

    # Record final metrics
    curl -s localhost:2379/metrics > "$SCENARIO_DIR/metrics_end.txt"

    # Analyze results
    ./scripts/analyze_scenario.sh "$SCENARIO_DIR" "$scenario"

    # Reset network
    ./scripts/reset_network.sh

    echo "Scenario $name complete. Results in $SCENARIO_DIR"
    sleep 10  # Cool down between scenarios
done < <(jq -c --arg f "$FILTER_SCENARIO" \
    'if $f == "" then .scenarios[] else .scenarios[] | select(.name == $f) end' \
    "$SCENARIOS_FILE")

# Generate final report
./scripts/generate_report.sh "$RESULTS_DIR"
