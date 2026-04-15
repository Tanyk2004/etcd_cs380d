#!/bin/bash
# scripts/apply_network.sh

LATENCY=$1
JITTER=$2
LOSS=$3
INTERFACE=${4:-lo}  # Default to loopback for local testing

# Clear existing rules
tc qdisc del dev $INTERFACE root 2>/dev/null || true

if [ "$LATENCY" != "0ms" ] || [ "$LOSS" != "0" ]; then
    # Add netem qdisc for delay and loss
    tc qdisc add dev $INTERFACE root handle 1: netem \
        delay $LATENCY $JITTER distribution normal \
        loss $LOSS%
    
    echo "Applied: delay=$LATENCY jitter=$JITTER loss=$LOSS% on $INTERFACE"
else
    echo "No network impairment applied"
fi