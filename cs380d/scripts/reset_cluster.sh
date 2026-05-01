#!/bin/bash
# reset_cluster.sh - stop etcd, wipe data dirs, restart a fresh 3-node cluster

set -e

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CLUSTER="infra1=http://127.0.0.1:12380,infra2=http://127.0.0.1:22380,infra3=http://127.0.0.1:32380"

echo "Stopping etcd nodes..."
pkill -9 -f 'bin/etcd --name infra' 2>/dev/null || true

# Wait for all peer ports to be released before trying to rebind
for port in 12380 22380 32380; do
    for _ in $(seq 1 10); do
        ss -tlnp | grep -q ":$port " || break
        sleep 0.5
    done
done

echo "Wiping data directories..."
rm -rf "$REPO_ROOT/infra1.etcd" "$REPO_ROOT/infra2.etcd" "$REPO_ROOT/infra3.etcd"

echo "Starting etcd nodes..."
for i in 1 2 3; do
    case $i in
        1) client_port=2379;  peer_port=12380; metrics_port=9101 ;;
        2) client_port=22379; peer_port=22380; metrics_port=9102 ;;
        3) client_port=32379; peer_port=32380; metrics_port=9103 ;;
    esac

    "$REPO_ROOT/bin/etcd" \
        --name "infra$i" \
        --data-dir "$REPO_ROOT/infra$i.etcd" \
        --listen-client-urls "http://127.0.0.1:$client_port" \
        --advertise-client-urls "http://127.0.0.1:$client_port" \
        --listen-peer-urls "http://127.0.0.1:$peer_port" \
        --initial-advertise-peer-urls "http://127.0.0.1:$peer_port" \
        --listen-metrics-urls "http://127.0.0.1:$metrics_port" \
        --initial-cluster-token etcd-cluster-1 \
        --initial-cluster "$CLUSTER" \
        --initial-cluster-state new \
        --election-timeout="${ELECTION_TIMEOUT_MS:-1000}" \
        --heartbeat-interval="${HEARTBEAT_INTERVAL_MS:-100}" \
        --heartbeat-mark=0x1337 \
        --max-request-bytes=10485760 \
        --logger=zap --log-outputs=stderr \
        >> "/tmp/etcd${i}.log" 2>&1 &
done

echo "Waiting for cluster to form..."
sleep 2
for attempt in $(seq 1 20); do
    if "$REPO_ROOT/bin/etcdctl" \
        --endpoints=http://localhost:2379,http://localhost:22379,http://localhost:32379 \
        endpoint health &>/dev/null; then
        echo "Cluster healthy."
        exit 0
    fi
    sleep 1
done

echo "ERROR: cluster did not become healthy after reset" >&2
exit 1
