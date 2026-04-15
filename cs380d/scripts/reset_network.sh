#!/bin/bash
# reset_network.sh - remove all tc rules from the loopback interface

INTERFACE=${1:-lo}
tc qdisc del dev "$INTERFACE" root 2>/dev/null || true
echo "Network rules cleared on $INTERFACE"
