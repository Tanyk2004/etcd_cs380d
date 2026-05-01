#!/usr/bin/env python3
"""
Raft Observability Monitor for etcd.

Scrapes Prometheus metrics from etcd nodes and displays key Raft metrics
related to heartbeat health, leader elections, and network congestion.
Outputs both a live terminal view and a CSV log for later analysis.

Usage:
    python3 monitor.py --endpoints http://localhost:2379/metrics,http://localhost:22379/metrics
    python3 monitor.py --endpoints http://localhost:2379/metrics --interval 1 --csv results.csv
"""

import argparse
import csv
import io
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime


# Metrics we care about for the heartbeat-drop / leader-election investigation.
METRIC_KEYS = [
    # Server-level raft metrics
    "etcd_server_has_leader",
    "etcd_server_is_leader",
    "etcd_server_leader_changes_seen_total",
    "etcd_server_heartbeat_send_failures_total",
    "etcd_server_proposals_committed_total",
    "etcd_server_proposals_applied_total",
    "etcd_server_proposals_pending",
    "etcd_server_proposals_failed_total",
    "etcd_server_slow_read_indexes_total",
    "etcd_server_read_indexes_failed_total",
    # Network-level metrics
    "etcd_network_peer_sent_failures_total",
    "etcd_network_peer_received_failures_total",
    "etcd_network_active_peers",
    "etcd_network_disconnected_peers_total",
    # New: dropped messages by type (added by our patch)
    "etcd_network_dropped_messages_sent_total",
    "etcd_network_dropped_messages_received_total",
    # gRPC server metrics (request pressure)
    "grpc_server_started_total",
    "grpc_server_handled_total",
]

# Subset of metrics to display in the compact terminal view.
DISPLAY_METRICS = [
    "etcd_server_has_leader",
    "etcd_server_is_leader",
    "etcd_server_leader_changes_seen_total",
    "etcd_server_heartbeat_send_failures_total",
    "etcd_server_proposals_pending",
    "etcd_server_proposals_failed_total",
    "etcd_network_peer_sent_failures_total",
    "etcd_network_dropped_messages_sent_total",
    "etcd_network_dropped_messages_received_total",
]


def fetch_metrics(endpoint, timeout=5):
    """Fetch and parse Prometheus metrics from an endpoint."""
    try:
        req = urllib.request.Request(endpoint)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
        return parse_prometheus(body)
    except Exception as e:
        return {"__error__": str(e)}


def parse_prometheus(text):
    """Parse Prometheus text exposition format into {metric_name{labels}: value}."""
    metrics = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Split into metric_name{labels} value [timestamp]
        parts = line.split()
        if len(parts) < 2:
            continue
        key = parts[0]
        try:
            val = float(parts[1])
        except ValueError:
            continue
        metrics[key] = val
    return metrics


def extract_relevant(all_metrics, keys):
    """Extract metrics matching any of the given key prefixes."""
    result = {}
    for full_key, val in all_metrics.items():
        base = full_key.split("{")[0]
        if base in keys:
            result[full_key] = val
    return result


def format_value(v):
    """Format a metric value for display."""
    if v == int(v):
        return str(int(v))
    return f"{v:.4f}"


def print_header(endpoints):
    """Print the monitor header."""
    print("\033[2J\033[H", end="")  # Clear screen
    print("=" * 80)
    print("  etcd Raft Observability Monitor")
    print("  Tracking heartbeat drops, leader elections, network congestion")
    print("=" * 80)
    for i, ep in enumerate(endpoints):
        print(f"  Node {i}: {ep}")
    print("-" * 80)


def print_snapshot(endpoints, snapshots, prev_snapshots):
    """Print a formatted snapshot of metrics."""
    now = datetime.now().strftime("%H:%M:%S")
    print_header(endpoints)
    print(f"  Timestamp: {now}")
    print()

    for i, ep in enumerate(endpoints):
        snap = snapshots.get(ep, {})
        prev = prev_snapshots.get(ep, {})

        if "__error__" in snap:
            print(f"  [Node {i}] ERROR: {snap['__error__']}")
            print()
            continue

        print(f"  [Node {i}] {ep}")
        print(f"  {'Metric':<55} {'Value':>8} {'Delta':>8}")
        print(f"  {'-'*55} {'-'*8} {'-'*8}")

        relevant = extract_relevant(snap, DISPLAY_METRICS)
        for key in sorted(relevant.keys()):
            val = relevant[key]
            delta = ""
            if key in prev:
                d = val - prev[key]
                if d != 0:
                    delta = f"+{format_value(d)}" if d > 0 else format_value(d)

            # Shorten key for display
            display_key = key
            if len(display_key) > 55:
                display_key = display_key[:52] + "..."

            print(f"  {display_key:<55} {format_value(val):>8} {delta:>8}")
        print()

    # Summary section: highlight problems
    print("  --- ALERTS ---")
    alerts = []
    for i, ep in enumerate(endpoints):
        snap = snapshots.get(ep, {})
        prev = prev_snapshots.get(ep, {})
        if "__error__" in snap:
            continue

        # Check for leader changes
        curr_lc = snap.get("etcd_server_leader_changes_seen_total", 0)
        prev_lc = prev.get("etcd_server_leader_changes_seen_total", 0)
        if prev_lc > 0 and curr_lc > prev_lc:
            alerts.append(f"  !! Node {i}: LEADER ELECTION occurred "
                          f"(changes: {int(prev_lc)} -> {int(curr_lc)})")

        # Check for heartbeat failures
        curr_hb = snap.get("etcd_server_heartbeat_send_failures_total", 0)
        prev_hb = prev.get("etcd_server_heartbeat_send_failures_total", 0)
        if curr_hb > prev_hb:
            alerts.append(f"  !! Node {i}: {int(curr_hb - prev_hb)} heartbeat "
                          f"send failures since last check")

        # Check for dropped heartbeat messages
        for key, val in snap.items():
            if "dropped_messages" in key and "MsgHeartbeat" in key:
                prev_val = prev.get(key, 0)
                if val > prev_val:
                    alerts.append(
                        f"  !! Node {i}: {int(val - prev_val)} heartbeat "
                        f"messages DROPPED ({key})")

        # Check for no leader
        if snap.get("etcd_server_has_leader", 1) == 0:
            alerts.append(f"  !! Node {i}: NO LEADER")

    if alerts:
        for a in alerts:
            print(a)
    else:
        print("  (none)")
    print()
    sys.stdout.flush()


def write_csv_row(writer, timestamp, endpoints, snapshots):
    """Write one row of metrics to the CSV."""
    row = {"timestamp": timestamp}
    for i, ep in enumerate(endpoints):
        snap = snapshots.get(ep, {})
        if "__error__" in snap:
            continue
        relevant = extract_relevant(snap, METRIC_KEYS)
        for key, val in relevant.items():
            row[f"node{i}_{key}"] = val
    writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(
        description="Monitor etcd Raft metrics for heartbeat drops and leader elections")
    parser.add_argument(
        "--endpoints", required=True,
        help="Comma-separated list of etcd metrics endpoints "
             "(e.g. http://localhost:2379/metrics,http://localhost:22379/metrics)")
    parser.add_argument(
        "--interval", type=float, default=2.0,
        help="Scrape interval in seconds (default: 2)")
    parser.add_argument(
        "--csv", dest="csv_file", default=None,
        help="Path to write CSV log (optional)")
    parser.add_argument(
        "--duration", type=float, default=0,
        help="Run for this many seconds then exit (0=forever)")
    args = parser.parse_args()

    endpoints = [e.strip() for e in args.endpoints.split(",")]

    csv_fh = None
    csv_writer = None
    if args.csv_file:
        csv_fh = open(args.csv_file, "w", newline="")
        # We'll write headers after first scrape when we know all column names.
        csv_writer = None  # Initialized lazily.

    prev_snapshots = {}
    start = time.time()

    try:
        while True:
            snapshots = {}
            for ep in endpoints:
                snapshots[ep] = fetch_metrics(ep)

            print_snapshot(endpoints, snapshots, prev_snapshots)

            # CSV logging
            if csv_fh is not None:
                ts = datetime.now().isoformat()
                if csv_writer is None:
                    # Collect all possible column names
                    cols = {"timestamp"}
                    for i, ep in enumerate(endpoints):
                        snap = snapshots.get(ep, {})
                        relevant = extract_relevant(snap, METRIC_KEYS)
                        for key in relevant:
                            cols.add(f"node{i}_{key}")
                    csv_writer = csv.DictWriter(
                        csv_fh, fieldnames=sorted(cols), extrasaction="ignore")
                    csv_writer.writeheader()
                write_csv_row(csv_writer, ts, endpoints, snapshots)
                csv_fh.flush()

            prev_snapshots = snapshots

            if args.duration > 0 and (time.time() - start) >= args.duration:
                print("Duration reached, exiting.")
                break

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        if csv_fh:
            csv_fh.close()
            print(f"CSV log written to {args.csv_file}")


if __name__ == "__main__":
    main()
