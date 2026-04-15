#!/bin/bash
# scripts/apply_priority_qdisc.sh
# This sets up the qdisc structure your eBPF will use

INTERFACE=${1:-lo}
PEER_PORT=${2:-2380}

# Clear existing
tc qdisc del dev $INTERFACE root 2>/dev/null || true

# Create priority qdisc with 3 bands
tc qdisc add dev $INTERFACE root handle 1: prio bands 3 priomap 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1

# Band 0: High priority (will be set by eBPF for heartbeats)
# Band 1: Normal priority (default)
# Band 2: Low priority (bulk)

# Add netem to each band for testing different conditions
tc qdisc add dev $INTERFACE parent 1:1 handle 10: pfifo limit 1000
tc qdisc add dev $INTERFACE parent 1:2 handle 20: pfifo limit 1000  
tc qdisc add dev $INTERFACE parent 1:3 handle 30: pfifo limit 1000

# Filter: small packets to peer port go to high priority
# This is your "static prioritization" baseline
tc filter add dev $INTERFACE parent 1: protocol ip prio 1 u32 \
    match ip dport $PEER_PORT 0xffff \
    match u16 0x0000 0xff00 at 2 \
    flowid 1:1

echo "Priority qdisc configured on $INTERFACE"
tc qdisc show dev $INTERFACE