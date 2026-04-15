#!/bin/bash
# collect_metrics.sh <output_dir>
# Scrapes all 3 etcd nodes once per second and writes timeseries.csv.
#
# Columns:
#   timestamp               - unix epoch seconds
#   leader_changes          - max across nodes (cumulative counter)
#   proposals_pending       - raft proposals queued but not yet committed
#   proposals_failed        - cumulative failed proposals
#   peer_rtt_p99            - fraction of peer RTT samples under 100ms (proxy for congestion)
#   hb_send_failures        - cumulative heartbeat send failures
#   wal_fsync_avg_ms        - per-interval average WAL fsync latency (ms)
#   backend_commit_avg_ms   - per-interval average bbolt commit latency (ms)
#   has_leader              - 1 if cluster has a leader, 0 during election
#   proposals_committed_total - cumulative committed proposals (derive rate in plots)
#   slow_apply_total        - cumulative slow applies (I/O pressure indicator)
#   peer_sent_bytes_total   - cumulative bytes sent to peers (network saturation)

OUTPUT_DIR=$1
INTERVAL=1  # seconds

echo "timestamp,leader_changes,proposals_pending,proposals_failed,peer_rtt_p99,hb_send_failures,wal_fsync_avg_ms,backend_commit_avg_ms,has_leader,proposals_committed_total,slow_apply_total,peer_sent_bytes_total" \
    > "$OUTPUT_DIR/timeseries.csv"

# Track previous histogram values for per-interval averages
prev_wal_sum=0;     prev_wal_count=0
prev_bck_sum=0;     prev_bck_count=0

TMPDIR_METRICS=$(mktemp -d)
trap 'rm -rf "$TMPDIR_METRICS"' EXIT

scrape_parallel() {
    # Fire all 3 curls simultaneously into temp files, wait for all to finish.
    curl -s --max-time 2 localhost:2379/metrics   > "$TMPDIR_METRICS/m1" 2>/dev/null &
    curl -s --max-time 2 localhost:22379/metrics  > "$TMPDIR_METRICS/m2" 2>/dev/null &
    curl -s --max-time 2 localhost:32379/metrics  > "$TMPDIR_METRICS/m3" 2>/dev/null &
    wait
}

# Cumulative election counter — survives process restarts that reset Prometheus
# counters back to 0.  We track the raw per-node max and add any increment
# (including post-reset increments) to a running total.
prev_raw_lc=0
cumulative_elections=0

while true; do
    timestamp=$(date +%s)

    scrape_parallel
    m1=$(cat "$TMPDIR_METRICS/m1")
    m2=$(cat "$TMPDIR_METRICS/m2")
    m3=$(cat "$TMPDIR_METRICS/m3")

    # ── leader_changes: cumulative counter robust to process restarts ─────────
    lc1=$(printf '%s' "$m1" | awk '/^etcd_server_leader_changes_seen_total[^_]/{print $2}')
    lc2=$(printf '%s' "$m2" | awk '/^etcd_server_leader_changes_seen_total[^_]/{print $2}')
    lc3=$(printf '%s' "$m3" | awk '/^etcd_server_leader_changes_seen_total[^_]/{print $2}')
    raw_lc=$(printf '%s\n' "${lc1:-0}" "${lc2:-0}" "${lc3:-0}" \
        | awk 'BEGIN{m=0}{v=$1+0; if(v>m)m=v}END{print m}')

    # Accumulate elections, handling counter resets (raw_lc < prev means restart)
    if [ "$raw_lc" -gt 0 ] 2>/dev/null; then
        if [ "$raw_lc" -ge "$prev_raw_lc" ] 2>/dev/null; then
            cumulative_elections=$(( cumulative_elections + raw_lc - prev_raw_lc ))
        else
            # Counter reset: add the new value (elections since restart)
            cumulative_elections=$(( cumulative_elections + raw_lc ))
        fi
        prev_raw_lc=$raw_lc
    fi
    leader_changes=$cumulative_elections

    # ── has_leader: 1 if ANY node reports a leader (0 = election in progress) ─
    hl1=$(printf '%s' "$m1" | awk '/^etcd_server_has_leader[^_]/{print $2}')
    hl2=$(printf '%s' "$m2" | awk '/^etcd_server_has_leader[^_]/{print $2}')
    hl3=$(printf '%s' "$m3" | awk '/^etcd_server_has_leader[^_]/{print $2}')
    has_leader=$(printf '%s\n' "${hl1:-0}" "${hl2:-0}" "${hl3:-0}" \
        | awk 'BEGIN{m=0}{v=$1+0; if(v>m)m=v}END{print m}')

    # ── remaining metrics from node 1 (leader carries most load) ─────────────
    m="$m1"

    proposals_pending=$(printf '%s' "$m" | awk '/^etcd_server_proposals_pending[^_]/{print $2}')
    proposals_failed=$(printf '%s'  "$m" | awk '/^etcd_server_proposals_failed_total[^_]/{print $2}')
    hb_failures=$(printf '%s'       "$m" | awk '/^etcd_server_heartbeat_send_failures_total[^_]/{print $2}')
    proposals_committed=$(printf '%s' "$m" | awk '/^etcd_server_proposals_committed_total[^_]/{print $2}')
    slow_apply=$(printf '%s'        "$m" | awk '/^etcd_server_slow_apply_total[^_]/{print $2}')

    # peer_sent_bytes: sum across all three nodes for full picture
    pb1=$(printf '%s' "$m1" | awk '/^etcd_network_peer_sent_bytes_total[^_]/{s+=$2}END{print s+0}')
    pb2=$(printf '%s' "$m2" | awk '/^etcd_network_peer_sent_bytes_total[^_]/{s+=$2}END{print s+0}')
    pb3=$(printf '%s' "$m3" | awk '/^etcd_network_peer_sent_bytes_total[^_]/{s+=$2}END{print s+0}')
    peer_sent_bytes=$(awk -v a="${pb1:-0}" -v b="${pb2:-0}" -v c="${pb3:-0}" \
        'BEGIN{printf "%d", a+b+c}')

    # peer RTT: fraction of samples under 100ms (higher = more congestion)
    peer_rtt=$(printf '%s' "$m" \
        | awk '/etcd_network_peer_round_trip_time_seconds_bucket\{.*le="0\.1"/{print $2}' \
        | head -1)

    # ── WAL fsync latency (per-interval average, ms) ──────────────────────────
    wal_sum=$(printf '%s'   "$m" | awk '/^etcd_disk_wal_fsync_duration_seconds_sum[^_]/{print $2}')
    wal_count=$(printf '%s' "$m" | awk '/^etcd_disk_wal_fsync_duration_seconds_count[^_]/{print $2}')
    wal_avg_ms=$(awk -v s="${wal_sum:-0}" -v ps="$prev_wal_sum" \
                     -v c="${wal_count:-0}" -v pc="$prev_wal_count" \
        'BEGIN{dc=c-pc; printf "%.3f",(dc>0?(s-ps)/dc*1000:0)}')

    # ── backend commit latency (per-interval average, ms) ────────────────────
    bck_sum=$(printf '%s'   "$m" | awk '/^etcd_disk_backend_commit_duration_seconds_sum[^_]/{print $2}')
    bck_count=$(printf '%s' "$m" | awk '/^etcd_disk_backend_commit_duration_seconds_count[^_]/{print $2}')
    bck_avg_ms=$(awk -v s="${bck_sum:-0}" -v ps="$prev_bck_sum" \
                     -v c="${bck_count:-0}" -v pc="$prev_bck_count" \
        'BEGIN{dc=c-pc; printf "%.3f",(dc>0?(s-ps)/dc*1000:0)}')

    echo "$timestamp,${leader_changes:-0},${proposals_pending:-0},${proposals_failed:-0},${peer_rtt:-0},${hb_failures:-0},$wal_avg_ms,$bck_avg_ms,${has_leader:-1},${proposals_committed:-0},${slow_apply:-0},${peer_sent_bytes:-0}" \
        >> "$OUTPUT_DIR/timeseries.csv"

    prev_wal_sum=${wal_sum:-$prev_wal_sum}
    prev_wal_count=${wal_count:-$prev_wal_count}
    prev_bck_sum=${bck_sum:-$prev_bck_sum}
    prev_bck_count=${bck_count:-$prev_bck_count}

    sleep $INTERVAL
done
