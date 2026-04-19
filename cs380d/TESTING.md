# etcd Election Timeout Testing Guide

This guide covers building the stack, running individual scenarios, and observing
Raft election timeouts — including the eBPF-based heartbeat priority system and
NIC-contention scenarios that trigger elections without writing directly to etcd.

---

## Prerequisites

```bash
# 1. Build etcd (includes SO_MARK heartbeat tagging)
./scripts/build.sh          # or: go build -o bin/etcd ./server/etcdmain

# 2. Build the traffic simulator
cd etcd-workload-tester && cargo build --release && cd ..

# 3. Build the noise-client / noise-node / tc-exporter (NIC contention scenarios)
GOWORK=off go build -C cs380d/tools/network-noise -o cs380d/bin/noise-node   ./cmd/node
GOWORK=off go build -C cs380d/tools/network-noise -o cs380d/bin/noise-client ./cmd/client
GOWORK=off go build -C cs380d/tools/network-noise -o cs380d/bin/tc-exporter  ./cmd/tc-exporter

# 4. Build the eBPF controller (requires cilium/ebpf — separate module)
cd ebpf-controller && GOWORK=off go build -o ebpf-controller . && cd ..

# 5. Compile the TC BPF program (if bpf/tc_prio.bpf.o is missing or stale)
#    Requires: clang, llvm-strip, libbpf-dev
#    sudo apt install clang llvm libbpf-dev
cd bpf && make && cd ..

# 6. Install Python deps (for result analysis and plotting)
pip install pandas matplotlib

# 7. Verify tc/netem is available
tc qdisc help 2>&1 | grep netem || echo "WARN: netem not available"
```

---

## 1. Start a Local 3-Node etcd Cluster

`reset_cluster.sh` handles this automatically when running the test suite.
For manual testing, start the three nodes with `--heartbeat-mark=0x1337` so
the eBPF controller can identify and prioritize Raft stream connections:

```bash
CLUSTER="infra1=http://127.0.0.1:12380,infra2=http://127.0.0.1:22380,infra3=http://127.0.0.1:32380"

for i in 1 2 3; do
  case $i in
    1) cport=2379;  pport=12380; mport=9101 ;;
    2) cport=22379; pport=22380; mport=9102 ;;
    3) cport=32379; pport=32380; mport=9103 ;;
  esac
  ./bin/etcd \
    --name "infra$i" \
    --data-dir "./infra$i.etcd" \
    --listen-client-urls  "http://127.0.0.1:$cport" \
    --advertise-client-urls "http://127.0.0.1:$cport" \
    --listen-peer-urls    "http://127.0.0.1:$pport" \
    --initial-advertise-peer-urls "http://127.0.0.1:$pport" \
    --listen-metrics-urls "http://127.0.0.1:$mport" \
    --initial-cluster-token etcd-cluster-1 \
    --initial-cluster "$CLUSTER" \
    --initial-cluster-state new \
    --election-timeout=1000 \
    --heartbeat-interval=100 \
    --heartbeat-mark=0x1337 \
    --logger=zap --log-outputs=stderr >> /tmp/etcd${i}.log 2>&1 &
done
```

Verify health:
```bash
./bin/etcdctl \
  --endpoints=localhost:2379,localhost:22379,localhost:32379 \
  endpoint health
```

To wipe and restart fresh:
```bash
pkill -9 -f 'bin/etcd --name infra' || true
rm -rf infra{1,2,3}.etcd
```

---

## 2. eBPF Heartbeat Priority Boosting

The stack has two layers of eBPF integration:

**Layer 1 — etcd server (SO_MARK)**
`--heartbeat-mark=0x1337` causes each node to call `SO_MARK(0x1337)` on every
socket opened for Raft peer streams (heartbeats + AppendEntries). This tags the
packets at the kernel level without any payload inspection.

**Layer 2 — ebpf-controller + TC BPF program**
`bpf/tc_prio.bpf.c` is a TC egress hook that reads `skb->mark`. When the
controller sets `boost_config[0] = 1`, the kernel program raises `skb->priority`
to 7 (highest band in a PRIO qdisc) for every marked packet.

The controller polls etcd metrics every 200 ms. When peer RTT exceeds `--rtt-ms`
or `heartbeat_send_failures` is climbing, it enables the boost; it turns it off
after `--hysteresis` clean samples.

Start the controller before running scenarios (requires `sudo` for `CAP_NET_ADMIN`
and `CAP_BPF`):

```bash
sudo ebpf-controller/ebpf-controller \
  --iface lo \
  --obj  bpf/tc_prio.bpf.o \
  --metrics http://127.0.0.1:9101/metrics \
  --rtt-ms 20 \
  --hysteresis 5
```

The test suite starts this automatically when `ebpf-controller/ebpf-controller`
and `bpf/tc_prio.bpf.o` are present.

---

## 3. Available Scenarios

All scenarios are defined in **`cs380d/scenarios/definitions.json`**. Each entry
specifies election/heartbeat timeouts, per-phase QPS and duration, and network
impairment parameters.

| Scenario | election / heartbeat | Network | Load type |
|---|---|---|---|
| `baseline_steady_state` | 1000 ms / 100 ms | 1ms | Light mixed — 0 elections expected |
| `sustained_write_flood` | 250 ms / 50 ms | 1ms | Continuous max writes |
| `burst_recovery` | 250 ms / 50 ms | 5ms/2ms | Calm → burst → calm cycles |
| `gradual_rampup` | 250 ms / 50 ms | 2ms | 5-step load ramp, finds election threshold |
| `competing_traffic` | 250 ms / 50 ms | 5ms/5ms/0.1% loss | High QPS mixed on shared infra |
| `short_timeout_spike` | 150 ms / 30 ms | 70ms | Minimum timeout, any stall fires election |
| `parallel_write_bomb` | 200 ms / 30 ms | 2ms | 5× simulators, 64 KB values, disk I/O bottleneck |
| `mega_value_flood` | 200 ms / 30 ms | 1ms | 8× simulators, 512 KB values, WAL starvation |
| `wan_simulation` | 700 ms / 130 ms | 50ms/15ms/0.5% | Cross-region RTT |
| `cross_datacenter` | 1000 ms / 150 ms | 80ms/10ms/0.5% | US-East → US-West |
| `transatlantic` | 1300 ms / 250 ms | 120ms/20ms/1% | NYC → London |
| `network_saturated` | 1700 ms / 300 ms | 150ms/50ms | Extreme loopback latency |
| `chaos_network` | 2000 ms / 250 ms | 100ms/50ms/30% loss | Chaos: loss + jitter |
| `leader_kill` | 500 ms / 100 ms | 1ms | Direct leader kill, measures recovery cost |
| `nic_contention` | 150 ms / 30 ms | 2ms, **100 Mbit cap** | noise-client → noise-nodes, UDP fills tbf queue |
| `noisy_neighbor_elections` | 150 ms / 30 ms | 20ms, **500 Mbit cap** | noise-client → noise-nodes, 8 KB payloads |

The two `noise-client` scenarios are the primary proof that **network pressure
causes elections independently of Raft write volume**: the workload tester sends
requests to intermediate noise-nodes, which replicate via UDP (saturating the
bandwidth cap) and only write metadata to etcd every 5000–9000 ops. AppendEntries
are rare (~1–3/s), so the election timer runs down when the tbf queue is full.

---

## 4. Running Scenarios

All commands assume `CWD = cs380d/` (the `cs380d/` subdirectory).

### Run the full test suite

```bash
cd cs380d
sudo bash scripts/run_test_suite.sh
```

Results land in `./results/<timestamp>/`. Each scenario subdirectory contains:
- `timeseries.csv` — per-second Prometheus metric snapshots
- `tester_phase<N>.log` — raw workload tester output
- `ops_phase<N>.csv` — per-second throughput parsed from tester log
- `metrics_start.txt` / `metrics_end.txt` — raw Prometheus scrapes
- `noise_node{1,2,3}.log` — noise-node logs (NIC contention scenarios only)
- `analysis.json` — pass/fail verdict with election count, p99 latency, success rate

### Run a single scenario by name

```bash
cd cs380d
sudo bash scripts/run_test_suite.sh --scenario nic_contention
```

Replace `nic_contention` with any name from the scenario table above.

### Run a single traffic-sim profile (no scenario file)

```bash
./etcd-workload-tester/target/release/traffic-sim \
  --endpoints=localhost:2379,localhost:22379,localhost:32379 \
  --profile=spike \
  --duration=60s
```

Available profiles (ordered by intensity):

| Profile | QPS | Workers | Write ratio | Value size |
|---|---|---|---|---|
| `idle` | 5 | 1 | 50% | 64 B |
| `low` | 100 | 5 | 50% | 256 B |
| `medium` | 500 | 20 | 60% | 1 KB |
| `high` | 2000 | 50 | 70% | 4 KB |
| `write-heavy` | 3000 | 100 | 100% | 4 KB |
| `read-heavy` | 3000 | 50 | 5% | 256 B |
| `large-value` | 200 | 10 | 100% | 64 KB |
| `huge-value` | 50 | 8 | 100% | 512 KB |
| `spike` | 10000 | 200 | 80% | 8 KB |

### Run a NIC contention scenario manually

```bash
# 1. Start the cluster with heartbeat marking
ELECTION_TIMEOUT_MS=150 HEARTBEAT_INTERVAL_MS=30 bash scripts/reset_cluster.sh

# 2. Apply the bandwidth cap
sudo bash scripts/apply_network.sh 2ms 1ms 0 lo 100

# 3. Start noise-nodes (3 application servers that replicate via UDP to etcd)
for i in 1 2 3; do
  case $i in
    1) http=19001; udp=18001; peers="127.0.0.1:18002,127.0.0.1:18003" ;;
    2) http=19002; udp=18002; peers="127.0.0.1:18001,127.0.0.1:18003" ;;
    3) http=19003; udp=18003; peers="127.0.0.1:18001,127.0.0.1:18002" ;;
  esac
  cs380d/bin/noise-node \
    -id "node$i" -listen ":$http" -repl ":$udp" \
    -peers "$peers" \
    -etcdctl ./bin/etcdctl \
    -etcd http://127.0.0.1:2379,http://127.0.0.1:22379,http://127.0.0.1:32379 \
    -batch 9000 -pkt 8192 &
done

# 4. Drive load via noise-client (NOT etcd directly)
cs380d/bin/noise-client \
  -nodes http://127.0.0.1:19001,http://127.0.0.1:19002,http://127.0.0.1:19003 \
  -qps 15000 -size 4096 -workers 50 -dur 180s

# 5. Reset network when done
sudo bash scripts/reset_network.sh
```

---

## 5. Observing Election Timeouts

### Live terminal metrics

```bash
watch -n 1 'curl -s localhost:2379/metrics | grep -E \
  "etcd_server_leader_changes|etcd_server_has_leader|etcd_server_proposals_pending|etcd_network_peer_round_trip"'
```

| Metric | What it means |
|---|---|
| `etcd_server_leader_changes_seen_total` | **Increments = election fired** |
| `etcd_server_has_leader` | Drops to 0 during election, recovers when new leader elected |
| `etcd_server_proposals_pending` | >100 = Raft pipeline backing up |
| `etcd_server_proposals_failed_total` | Leader dropping proposals under overload |
| `etcd_network_peer_round_trip_time_seconds` | **Primary network signal.** p99 > heartbeat_interval = heartbeats at risk; p99 > election_timeout = elections will fire |
| `etcd_server_heartbeat_send_failures_total` | Disk-induced failures only (WAL stall). Will be **0** under NIC saturation — use peer RTT instead |

### NIC saturation metrics (tc qdisc)

The `tc-exporter` on `:9105` exposes queue stats. With the Grafana stack running,
the "NIC Queue Saturation" and "NIC Throttle & Drop Rate" panels show these live:

```bash
# Manual check while a scenario is running (requires root)
tc -s qdisc show dev lo
```

| Field | Meaning |
|---|---|
| `overlimits` | Packets that hit the rate cap (queued, not yet dropped) |
| `dropped` | Queue overflow — these cause TCP retransmit → election when RTO > election_timeout |
| `backlog Xb` | Instantaneous queue depth. Cap = rate × latency = 100 Mbit × 200ms = **2.5 MB** |

Election trigger sequence: `overlimits rises → backlog → 2.5 MB → drops appear → leader_changes increments`

### Grafana dashboard

Start the monitoring stack (from repo root):

```bash
cd tools/raft-monitor && docker compose up -d
```

Open **http://localhost:3000** (admin / admin). The "etcd Raft Observability"
dashboard auto-provisions with panels for:
- Leader status and election rate
- Raft peer RTT p99 (with threshold lines at 30 ms and 150 ms)
- NIC queue backlog and drop rate (from tc-exporter)
- Proposals pending / failed
- WAL fsync and backend commit latency
- Noise app throughput vs etcd proposal rate

The tc-exporter starts automatically when running the test suite. To start it
standalone so Grafana has data between scenario runs:

```bash
cs380d/bin/tc-exporter -iface lo -port :9105 &
```

### Continuous metrics CSV

```bash
mkdir -p /tmp/etcd-results
cs380d/scripts/collect_metrics.sh /tmp/etcd-results &
METRICS_PID=$!

# ... run your scenario ...

kill $METRICS_PID
# Results are in /tmp/etcd-results/timeseries.csv
```

CSV columns: `timestamp, leader_changes, proposals_pending, proposals_failed,
peer_rtt_p99, hb_send_failures, wal_fsync_avg_ms, backend_commit_avg_ms,
has_leader, proposals_committed_total, slow_apply_total, peer_sent_bytes_total,
noise_ops_s, tc_drops_s, tc_overlimits_s, tc_backlog_bytes, tc_backlog_pkts`

---

## 6. Network Impairment

`apply_network.sh` wraps `tc netem` + optional `tc tbf` and requires `sudo`.

```bash
# Delay only
sudo cs380d/scripts/apply_network.sh 50ms 10ms 0.1 lo

# Bandwidth cap + delay (NIC contention scenarios)
# tbf rate cap is set first; netem delay/loss runs inside it
sudo cs380d/scripts/apply_network.sh 2ms 1ms 0 lo 100    # 100 Mbit cap

# Remove all rules
sudo cs380d/scripts/reset_network.sh
# or manually:
sudo tc qdisc del dev lo root 2>/dev/null || true
```

**How the bandwidth cap triggers elections:**
The tbf queue holds up to `rate × latency = 100 Mbit × 200 ms = 2.5 MB`.
UDP replication (15000 req/s × 8 KB = ~1 Gbit/s offered) fills this in ~50 ms.
Once full, incoming packets queue for up to 200 ms before being dropped.
With `election_timeout=150 ms < tbf_latency=200 ms`, a heartbeat TCP segment
that enters a full queue arrives at the follower after the election timer has
already fired.

> `tc netem` on loopback affects outgoing packets only and applies equally to
> all three nodes since they share the same interface. For asymmetric impairment
> per node, run nodes in separate network namespaces.

---

## 7. Interpreting Results

**No elections during spike** — heartbeat pipeline keeping up; Raft term stable.

**Elections during `write-heavy` or `spike`** — AppendEntries are so frequent
they reset the election timer, but `proposals_pending` climbs until the leader's
send buffer saturates. The ebpf-controller should prevent this by boosting
marked heartbeat packets above bulk data traffic.

**Elections during `large-value` / `mega_value_flood`** — WAL fsync blocking the
Raft goroutine (check `wal_fsync_avg_ms` rising). Not network-induced.

**Elections during `nic_contention`** — the intended outcome. `tc_drops_s > 0`
and `peer_rtt_p99 > election_timeout` confirm the mechanism:
NIC saturation → tbf queue overflow → heartbeat TCP dropped → follower election.

**`has_leader` stays 0 for >2 s** — election is not completing. Probable causes:
RequestVote RPCs are also being dropped (network too saturated), or
`election_timeout < 4 × RTT` violates the Raft liveness condition.

**Elections in WAN scenarios immediately after cluster start** — the default
election timeout is too close to the RTT. Each scenario already has a tuned
`election_timeout_ms` satisfying `election_timeout ≥ 4 × RTT`.

---

## 8. Useful One-Liners

```bash
# Count total leader changes since cluster start
curl -s localhost:2379/metrics | grep 'etcd_server_leader_changes_seen_total' | grep -v '#'

# Live peer RTT across all nodes
for port in 9101 9102 9103; do
  echo "node:$port RTT samples:"
  curl -s localhost:$port/metrics | grep 'peer_round_trip_time_seconds_sum\|_count' | grep -v '#'
done

# Watch tc queue depth in real time (requires root)
watch -n 0.5 'tc -s qdisc show dev lo | grep -E "backlog|dropped|overlimits"'

# Tail ebpf-controller boost decisions
tail -f /tmp/ebpf-controller.log

# Query tc metrics from Prometheus
curl -s 'localhost:9090/api/v1/query?query=tc_qdisc_backlog_bytes{type="tbf"}' \
  | python3 -c "import json,sys; [print(r['metric'], r['value'][1]) for r in json.load(sys.stdin)['data']['result']]"

# Run analysis on an existing result directory
python3 cs380d/scripts/analyze_results.py \
  ./results/20260408_120000/nic_contention \
  "$(jq -c '.scenarios[] | select(.name=="nic_contention")' cs380d/scenarios/definitions.json)"

# Hot-reload Prometheus config after editing prometheus.yml
curl -s -X POST localhost:9090/-/reload
```
