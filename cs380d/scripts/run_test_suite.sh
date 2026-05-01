#!/bin/bash
# scripts/run_test_suite.sh

set -e

RESULTS_DIR="./results/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$RESULTS_DIR"

# If run via sudo, fix ownership of results so the real user can read them
REAL_USER="${SUDO_USER:-$USER}"
NOISE_PID=""
TC_EXPORTER_PID=""
EBPF_CONTROLLER_PID=""
cleanup() {
    [[ -n "$NOISE_PID" ]]            && kill "$NOISE_PID"            2>/dev/null || true
    [[ -n "$TC_EXPORTER_PID" ]]      && kill "$TC_EXPORTER_PID"      2>/dev/null || true
    [[ -n "$EBPF_CONTROLLER_PID" ]]  && kill "$EBPF_CONTROLLER_PID"  2>/dev/null || true
    stop_noise_nodes 2>/dev/null || true
    chown -R "$REAL_USER:" "$RESULTS_DIR" 2>/dev/null || true
}
trap cleanup EXIT

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$SCRIPT_DIR/../bin"

TRAFFIC_SIM="$REPO_ROOT/etcd-workload-tester/target/release/traffic-sim"
NOISE_NODE="$BIN_DIR/noise-node"
NOISE_CLIENT="$BIN_DIR/noise-client"
TC_EXPORTER="$BIN_DIR/tc-exporter"
ETCDCTL="$REPO_ROOT/bin/etcdctl"
EBPF_CONTROLLER="$REPO_ROOT/ebpf-controller/ebpf-controller"
EBPF_BPF_OBJ="$REPO_ROOT/bpf/tc_prio.bpf.o"
ETCD_ENDPOINTS="http://127.0.0.1:2379,http://127.0.0.1:22379,http://127.0.0.1:32379"
SCENARIOS_FILE="./scenarios/definitions.json"

NOISE_NODE_PIDS=()  # PIDs of the 3 noise-node instances for the current scenario

# Build noise binaries if missing or stale
NOISE_SRC="$SCRIPT_DIR/../tools/network-noise"
_build_noise() {
    local src_ts
    src_ts=$(find "$NOISE_SRC" -name '*.go' -newer "$NOISE_NODE" 2>/dev/null | head -1)
    if [[ ! -x "$NOISE_NODE" || -n "$src_ts" ]]; then
        echo "Building noise-node..."
        GOWORK=off go build -C "$NOISE_SRC" -o "$BIN_DIR/noise-node"   ./cmd/node   2>&1 || \
            { echo "WARNING: noise-node build failed"; return 1; }
        GOWORK=off go build -C "$NOISE_SRC" -o "$BIN_DIR/noise-client" ./cmd/client 2>&1 || \
            { echo "WARNING: noise-client build failed"; return 1; }
        echo "Noise binaries built."
    fi
}
mkdir -p "$BIN_DIR"
_build_noise || true

# Start tc-exporter so Prometheus (and Grafana) can see qdisc saturation metrics.
# Safe to run even when no tbf cap is active — all values are just 0.
# Idempotent: skip if already serving on :9105.
TC_EXPORTER_PID=""
if curl -sf --max-time 1 localhost:9105/metrics >/dev/null 2>&1; then
    echo "tc-exporter already running on :9105"
elif GOWORK=off go build -C "$NOISE_SRC" -o "$BIN_DIR/tc-exporter" ./cmd/tc-exporter 2>/dev/null; then
    "$TC_EXPORTER" -iface lo -port :9105 > /tmp/tc-exporter.log 2>&1 &
    TC_EXPORTER_PID=$!
    echo "tc-exporter started (pid $TC_EXPORTER_PID) → :9105"
else
    echo "WARNING: tc-exporter build failed; tc metrics will not appear in Grafana"
fi

# Start the ebpf-controller: loads bpf/tc_prio.bpf.o, attaches it to TC egress
# on lo, and toggles heartbeat priority boosting when RTT or hb-failures rise.
# Requires CAP_NET_ADMIN + CAP_BPF (run_test_suite.sh is already invoked via sudo).
# --rtt-ms 20: boost kicks in when peer RTT exceeds 20ms, well before the
#              30ms heartbeat interval is threatened.
EBPF_CONTROLLER_PID=""
if [[ -x "$EBPF_CONTROLLER" && -f "$EBPF_BPF_OBJ" ]]; then
    "$EBPF_CONTROLLER" \
        --iface lo \
        --obj  "$EBPF_BPF_OBJ" \
        --metrics "http://127.0.0.1:9101/metrics" \
        --rtt-ms 20 \
        >> /tmp/ebpf-controller.log 2>&1 &
    EBPF_CONTROLLER_PID=$!
    echo "ebpf-controller started (pid $EBPF_CONTROLLER_PID, log /tmp/ebpf-controller.log)"
else
    echo "WARNING: ebpf-controller or bpf/tc_prio.bpf.o not found — heartbeat priority boost disabled"
    echo "  Build with: go build -o ebpf-controller/ebpf-controller ./ebpf-controller && make -C bpf"
fi

# ── Noise-node cluster management ────────────────────────────────────────────
# Starts 3 noise-node instances on localhost (one per "application node").
# Each pair is peers for UDP replication.
start_noise_nodes() {
    local batch=$1     # etcd write every N ops
    local pkt_size=${2:-1400}  # UDP replication packet size in bytes

    if [[ ! -x "$NOISE_NODE" ]]; then
        echo "ERROR: noise-node binary not found; skipping" >&2
        return 1
    fi

    echo "Starting 3 noise-node instances (batch=${batch} pkt=${pkt_size}B)..."
    # node1: HTTP :19001, UDP repl :18001, peers → 18002,18003
    "$NOISE_NODE" \
        -id node1 -listen :19001 -repl :18001 \
        -peers 127.0.0.1:18002,127.0.0.1:18003 \
        -etcdctl "$ETCDCTL" -etcd "$ETCD_ENDPOINTS" \
        -batch "$batch" -pkt "$pkt_size" >> "$SCENARIO_DIR/noise_node1.log" 2>&1 &
    NOISE_NODE_PIDS+=($!)

    "$NOISE_NODE" \
        -id node2 -listen :19002 -repl :18002 \
        -peers 127.0.0.1:18001,127.0.0.1:18003 \
        -etcdctl "$ETCDCTL" -etcd "$ETCD_ENDPOINTS" \
        -batch "$batch" -pkt "$pkt_size" >> "$SCENARIO_DIR/noise_node2.log" 2>&1 &
    NOISE_NODE_PIDS+=($!)

    "$NOISE_NODE" \
        -id node3 -listen :19003 -repl :18003 \
        -peers 127.0.0.1:18001,127.0.0.1:18002 \
        -etcdctl "$ETCDCTL" -etcd "$ETCD_ENDPOINTS" \
        -batch "$batch" -pkt "$pkt_size" >> "$SCENARIO_DIR/noise_node3.log" 2>&1 &
    NOISE_NODE_PIDS+=($!)

    # Wait for all 3 HTTP ports to be ready
    for port in 19001 19002 19003; do
        for _ in $(seq 1 20); do
            curl -sf "http://127.0.0.1:$port/health" &>/dev/null && break
            sleep 0.2
        done
    done
    echo "Noise nodes ready (pids: ${NOISE_NODE_PIDS[*]})"
}

stop_noise_nodes() {
    for pid in "${NOISE_NODE_PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    NOISE_NODE_PIDS=()
}

# Run N parallel noise-client instances for a given duration.
# All instances append to the same log file so parse_tester_stats.py sees
# aggregated ops/s (lines share ts= timestamps and are summed per-second).
run_parallel_noise_clients() {
    local qps=$1
    local size=$2
    local duration=$3
    local n=$4
    local log_file=$5
    local workers=${6:-50}
    local pids=()

    local nodes="http://127.0.0.1:19001,http://127.0.0.1:19002,http://127.0.0.1:19003"
    # Per-instance QPS: divide evenly
    local per_qps=$(( qps / n ))
    [[ $per_qps -lt 1 ]] && per_qps=1

    # Truncate log file once before all instances start so >> appends cleanly
    [[ -n "$log_file" ]] && : > "$log_file"

    for i in $(seq 1 "$n"); do
        if [[ -n "$log_file" ]]; then
            "$NOISE_CLIENT" \
                -nodes "$nodes" -qps "$per_qps" -size "$size" \
                -workers "$workers" -dur "$duration" \
                -profile "noise-c${i}" \
                >> "$log_file" 2>&1 &
        else
            "$NOISE_CLIENT" \
                -nodes "$nodes" -qps "$per_qps" -size "$size" \
                -workers "$workers" -dur "$duration" \
                > /dev/null 2>&1 &
        fi
        pids+=($!)
    done
    for pid in "${pids[@]}"; do
        wait "$pid" 2>/dev/null || true
    done
}

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
    latency=$(echo "$scenario"  | jq -r '.network.latency')
    jitter=$(echo "$scenario"   | jq -r '.network.jitter')
    loss=$(echo "$scenario"     | jq -r '.network.packet_loss')
    bw_limit=$(echo "$scenario" | jq -r '.network.bw_limit_mbit // 0')
    noise_bw=$(echo "$scenario" | jq -r '.network.noise_bw_mbit // 0')

    echo "Applying network conditions: latency=$latency jitter=$jitter loss=$loss bw_limit=${bw_limit}mbit noise=${noise_bw}mbit"
    ./scripts/apply_network.sh "$latency" "$jitter" "$loss" lo "$bw_limit"

    # Read per-scenario etcd tuning (with defaults)
    election_ms=$(echo "$scenario"   | jq -r '.election_timeout_ms // 1000')
    heartbeat_ms=$(echo "$scenario"  | jq -r '.heartbeat_interval_ms // 100')
    use_noise=$(echo "$scenario"      | jq -r '.use_noise_client // false')
    noise_batch=$(echo "$scenario"   | jq -r '.noise_batch_size // 2000')
    noise_pkt=$(echo "$scenario"     | jq -r '.noise_pkt_size // 1400')
    noise_workers=$(echo "$scenario" | jq -r '.noise_workers // 50')

    echo "Resetting cluster (election_timeout=${election_ms}ms heartbeat=${heartbeat_ms}ms)..."
    ELECTION_TIMEOUT_MS="$election_ms" HEARTBEAT_INTERVAL_MS="$heartbeat_ms" \
        ./scripts/reset_cluster.sh
    sleep 5

    # Record initial metrics
    curl -s localhost:2379/metrics > "$SCENARIO_DIR/metrics_start.txt"

    # ── Noise-node cluster: start before metrics so their etcd writes appear ──
    # For use_noise_client=true scenarios: noise-nodes ARE the application.
    # The workload tester (noise-client) talks to them, not to etcd directly.
    # For use_noise_client=false scenarios with noise_bw set: pure UDP flood.
    NOISE_PID=""
    if [[ "$use_noise" == "true" ]]; then
        stop_noise_nodes  # clean up any leftover from previous scenario
        start_noise_nodes "$noise_batch" "$noise_pkt"
    elif [[ "$noise_bw" != "0" && "$noise_bw" != "0.0" && -x "$BIN_DIR/network-noise" ]]; then
        echo "Starting raw UDP noise at ${noise_bw} Mbit/s..."
        "$BIN_DIR/network-noise" \
            -listen 127.0.0.1:19998 -target 127.0.0.1:19998 \
            -bw "$noise_bw" >> "$SCENARIO_DIR/noise.log" 2>&1 &
        NOISE_PID=$!
        sleep 0.5
    fi

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
                --heartbeat-mark=0x1337 \
                --max-request-bytes=10485760 \
                --logger=zap --log-outputs=stderr \
                >> "/tmp/etcd${killed_idx}.log" 2>&1 &
            echo "  $killed_name restarted (rejoining cluster)"
            sleep 2
            continue
        fi

        # ── Normal workload phase ─────────────────────────────────────────────
        committed_before=$(scrape_metric 'etcd_server_proposals_committed_total')
        failed_before=$(scrape_metric 'etcd_server_proposals_failed_total')

        tester_log="$SCENARIO_DIR/tester_phase${phase_num}.log"

        if [[ "$use_noise" == "true" ]]; then
            # ── noise-client path: requests go to noise-nodes, NOT etcd ──────
            payload_size=$(echo "$phase" | jq -r '.payload_size // .value_size // 4096')
            echo "Phase $phase_num [noise-client] x${parallel} qps=${qps} size=${payload_size}B duration=${duration}"
            run_parallel_noise_clients "$qps" "$payload_size" "$duration" "$parallel" "$tester_log" "$noise_workers"
        else
            # ── traffic-sim path: direct etcd writes ──────────────────────────
            profile=$(map_to_profile "$qps" "$read_ratio" "$value_size")
            echo "Phase $phase_num: profile=$profile x${parallel} (qps=$qps read_ratio=$read_ratio value_size=$value_size) duration=$duration"
            run_parallel_sims "$profile" "$duration" "$parallel" "$tester_log"
        fi

        committed_after=$(scrape_metric 'etcd_server_proposals_committed_total')
        failed_after=$(scrape_metric 'etcd_server_proposals_failed_total')

        total=$(( committed_after - committed_before ))
        failed=$(( failed_after - failed_before ))
        successful=$(( total - failed ))

        # Parse stats log into ops CSV for throughput plots
        PYTHON="${VENV_PYTHON:-/home/tanay/cs380d/venv/bin/python3}"
        "$PYTHON" "$(dirname "$0")/parse_tester_stats.py" \
            "$tester_log" "$SCENARIO_DIR/ops_phase${phase_num}.csv" 2>/dev/null || true

        phase_profile=$([ "$use_noise" == "true" ] && echo "noise-client" || echo "$profile")
        cat > "$SCENARIO_DIR/phase_$(date +%s).json" <<EOF
{
  "profile": "${phase_profile}",
  "parallel_sims": $parallel,
  "total_operations": $total,
  "successful_operations": $successful,
  "latencies_ms": []
}
EOF
    done < <(echo "$scenario" | jq -c '.phases[]')

    # Stop metrics collection, noise generator, and noise nodes
    kill $METRICS_PID 2>/dev/null || true
    [[ -n "$NOISE_PID" ]] && kill $NOISE_PID 2>/dev/null || true
    NOISE_PID=""
    if [[ "$use_noise" == "true" ]]; then
        stop_noise_nodes
    fi

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
