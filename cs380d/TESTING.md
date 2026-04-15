# etcd Election Timeout Testing Guide

This guide walks you through starting a local etcd cluster, running the workload scenarios, and observing raft election timeouts.

---

## Prerequisites

```bash
# Build etcd
make build

# Build the traffic simulator
cd etcd-workload-tester
cargo build --release
cd ..

# Install Python deps (for analysis)
pip install pandas

# Verify tc/netem is available (for network impairment scenarios)
tc qdisc help 2>&1 | grep netem || echo "WARN: netem not available"
```

---

## 1. Start a Local 3-Node etcd Cluster

Open three terminals (or run in background):

```bash
# Node 1
./bin/etcd \
  --name node1 \
  --data-dir /tmp/etcd-node1 \
  --listen-client-urls http://127.0.0.1:2379 \
  --advertise-client-urls http://127.0.0.1:2379 \
  --listen-peer-urls http://127.0.0.1:2380 \
  --initial-advertise-peer-urls http://127.0.0.1:2380 \
  --initial-cluster node1=http://127.0.0.1:2380,node2=http://127.0.0.1:2480,node3=http://127.0.0.1:2580 \
  --initial-cluster-state new \
  --log-level warn

# Node 2
./bin/etcd \
  --name node2 \
  --data-dir /tmp/etcd-node2 \
  --listen-client-urls http://127.0.0.1:22379 \
  --advertise-client-urls http://127.0.0.1:22379 \
  --listen-peer-urls http://127.0.0.1:2480 \
  --initial-advertise-peer-urls http://127.0.0.1:2480 \
  --initial-cluster node1=http://127.0.0.1:2380,node2=http://127.0.0.1:2480,node3=http://127.0.0.1:2580 \
  --initial-cluster-state existing \
  --log-level warn

# Node 3
./bin/etcd \
  --name node3 \
  --data-dir /tmp/etcd-node3 \
  --listen-client-urls http://127.0.0.1:32379 \
  --advertise-client-urls http://127.0.0.1:32379 \
  --listen-peer-urls http://127.0.0.1:2580 \
  --initial-advertise-peer-urls http://127.0.0.1:2580 \
  --initial-cluster node1=http://127.0.0.1:2380,node2=http://127.0.0.1:2480,node3=http://127.0.0.1:2580 \
  --initial-cluster-state existing \
  --log-level warn
```

To wipe and restart the cluster fresh:
```bash
rm -rf /tmp/etcd-node{1,2,3}
```

Verify the cluster is healthy:
```bash
./bin/etcdctl --endpoints=localhost:2379,localhost:22379,localhost:32379 endpoint health
```

---

## 2. Traffic Simulator Overview

The simulator binary lives at `etcd-workload-tester/target/release/traffic-sim`.

**Two modes:**

| Mode | When to use |
|------|-------------|
| `--profile <name>` | Hold a single load level for a fixed duration |
| `--scenario <file>` | Multi-phase run that steps through profiles automatically |

**Available profiles (ordered by intensity):**

| Profile      | QPS    | Workers | Write Ratio | Value Size |
|--------------|--------|---------|-------------|------------|
| idle         | 5      | 1       | 0.50        | 64 B       |
| low          | 100    | 5       | 0.50        | 256 B      |
| medium       | 500    | 20      | 0.60        | 1 KB       |
| high         | 2000   | 50      | 0.70        | 4 KB       |
| write-heavy  | 3000   | 100     | 1.00        | 4 KB       |
| read-heavy   | 3000   | 50      | 0.05        | 256 B      |
| txn          | 500    | 30      | 0.80        | 512 B      |
| leases       | 300    | 20      | 0.70        | 256 B      |
| large-value  | 200    | 10      | 1.00        | 64 KB      |
| spike        | 10000  | 200     | 0.80        | 8 KB       |

---

## 3. Running Scenarios

All commands assume `CWD = repo root`.

### 3a. Quick single-profile run

```bash
./etcd-workload-tester/target/release/traffic-sim \
  --endpoints=localhost:2379,localhost:22379,localhost:32379 \
  --profile=high \
  --duration=60s
```

### 3b. Step-up / step-down ramp

Gradually ramps from idle → low → medium → high → spike → back down. Good for finding the load threshold where election timeouts start.

```bash
./etcd-workload-tester/target/release/traffic-sim \
  --endpoints=localhost:2379,localhost:22379,localhost:32379 \
  --scenario=etcd-workload-tester/scenarios/step-up-down.json
```

### 3c. Periodic spikes

Alternates between low load and 5-second spike bursts. Useful for seeing whether a spike causes an election and how long recovery takes.

```bash
./etcd-workload-tester/target/release/traffic-sim \
  --endpoints=localhost:2379,localhost:22379,localhost:32379 \
  --scenario=etcd-workload-tester/scenarios/periodic-spikes.json
```

### 3d. Election stress

Cycles through write-heavy → large-value → txn → leases. Designed specifically to exercise the workloads most likely to delay heartbeats and trigger election timeouts.

```bash
./etcd-workload-tester/target/release/traffic-sim \
  --endpoints=localhost:2379,localhost:22379,localhost:32379 \
  --scenario=etcd-workload-tester/scenarios/election-stress.json
```

---

## 4. Observing Election Timeouts

### 4a. Live metrics (terminal)

While the simulator runs, poll the key raft metrics in a second terminal:

```bash
watch -n 1 'curl -s localhost:2379/metrics | grep -E \
  "etcd_server_leader_changes|etcd_server_proposals_pending|etcd_server_proposals_failed|etcd_server_heartbeat_send_failures|etcd_network_peer_round_trip"'
```

What to look for:

| Metric | Election-timeout signal |
|--------|------------------------|
| `etcd_server_leader_changes_seen_total` | **Increments** = an election happened |
| `etcd_server_proposals_pending` | Rising above ~100 = raft pipeline backing up |
| `etcd_server_proposals_failed_total` | Rising = the leader is dropping proposals |
| `etcd_server_heartbeat_send_failures_total` | Any non-zero = heartbeats delayed/dropped |
| `etcd_network_peer_round_trip_time_seconds` | p99 > ~150 ms on loopback = raft is saturated |

### 4b. Leader-change event stream (FIFO)

The simulator writes a JSON event to `/tmp/raft-leader-events` every time the leader changes. Read it in a separate terminal to see election events with nanosecond timestamps:

```bash
# Create the FIFO if it doesn't exist yet
mkfifo /tmp/raft-leader-events 2>/dev/null || true
# Tail events
cat /tmp/raft-leader-events
```

Example output:
```json
{"ts_ns":1710370123456789012,"event":"leader_change","old_leader":8743054607900982853,"new_leader":3372751804698800710,"old_term":3,"term":4,"changes_total":1}
```

Pass `--leader-pipe` to change the FIFO path:
```bash
traffic-sim ... --leader-pipe=/tmp/my-events --leader-poll-ms=100
```

### 4c. Continuous metrics collection

Capture a time-series CSV during a run using the provided script:

```bash
mkdir -p /tmp/etcd-results
cs380d/scripts/collect_metrics.sh /tmp/etcd-results &
METRICS_PID=$!

# ... run your scenario ...

kill $METRICS_PID
# Results are in /tmp/etcd-results/timeseries.csv
```

Columns: `timestamp, leader_changes, proposals_pending, proposals_failed, peer_rtt_p99, hb_send_failures`

### 4d. HTTP control API (live profile switching)

While a simulator is running you can switch profiles without restarting:

```bash
# Switch to spike mid-test
curl -X POST http://localhost:9191/profile/spike

# Back to low
curl -X POST http://localhost:9191/profile/low

# Check current profile and stats
curl http://localhost:9191/profile
curl http://localhost:9191/stats
```

---

## 5. Running the Full Test Suite

The suite in `cs380d/scenarios/definitions.json` covers six scenarios with expected pass/fail thresholds:

| Scenario | Description |
|----------|-------------|
| `baseline_steady_state` | Normal ops, zero elections expected |
| `sustained_write_flood` | Max write pressure, validates election tolerance |
| `burst_recovery` | Realistic traffic bursts with 5ms WAN sim |
| `wan_simulation` | 50ms cross-region latency + 0.1% packet loss |
| `gradual_rampup` | Finds the load threshold where elections begin |
| `competing_traffic` | 100 Mbit bandwidth cap to simulate shared network |

```bash
cd cs380d
bash scripts/run_test_suite.sh
```

Results land in `./results/<timestamp>/`. Each scenario directory contains:
- `timeseries.csv` — per-second metric snapshots
- `metrics_start.txt` / `metrics_end.txt` — raw Prometheus scrapes
- `analysis.json` — pass/fail verdict with election count, p99 latency, success rate

Run analysis standalone on any existing result directory:
```bash
python3 cs380d/scripts/analyze_results.py \
  ./results/20260408_120000/sustained_write_flood \
  "$(jq -c '.scenarios[] | select(.name=="sustained_write_flood")' cs380d/scenarios/definitions.json)"
```

---

## 6. Network Impairment (for WAN / competing-traffic scenarios)

The `apply_network.sh` script wraps `tc netem` and requires `sudo`:

```bash
# Add 50ms delay, 10ms jitter, 0.1% loss on loopback
sudo cs380d/scripts/apply_network.sh 50ms 10ms 0.1

# Remove all rules
sudo tc qdisc del dev lo root 2>/dev/null || true
```

> **Note:** `tc netem` on loopback affects outgoing packets only and applies equally to all three nodes since they share the same interface. For asymmetric impairment per node, run nodes on separate network namespaces.

---

## 7. Interpreting Results

**No elections during spike** — heartbeat pipeline is keeping up; raft term is stable.

**Elections spike with `proposals_pending` > ~500** — the leader's send queue is saturated; it can't deliver heartbeats within the election timeout (`--election-timeout`, default 1000 ms). The followers time out and start a new election.

**Elections appear during `large-value` phase but not `write-heavy`** — large value serialization/IO is blocking the raft goroutine, not raw QPS.

**Elections appear immediately in `wan_simulation`** — the default `--election-timeout` (1000 ms) is too short for 50 ms RTT under load; try `--election-timeout=5000`.

---

## 8. Useful One-Liners

```bash
# Count total leader changes since cluster start
curl -s localhost:2379/metrics | grep 'etcd_server_leader_changes_seen_total' | grep -v '#'

# Watch raft term across all nodes
for port in 2379 22379 32379; do
  echo -n "node:$port term="; curl -s localhost:$port/metrics | \
    grep 'etcd_server_current_version' | head -1
done

# Dump full metrics snapshot
curl -s localhost:2379/metrics > /tmp/etcd-metrics-$(date +%s).txt

# Run simulator in background, kill after 2 minutes
./etcd-workload-tester/target/release/traffic-sim \
  --endpoints=localhost:2379 \
  --scenario=etcd-workload-tester/scenarios/election-stress.json &
SIM_PID=$!
sleep 120 && kill $SIM_PID
```
