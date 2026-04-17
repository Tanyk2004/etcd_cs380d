#!/bin/bash
# scripts/apply_network.sh <latency> <jitter> <loss%> [interface] [bw_mbit]
#
# Applies tc qdiscs to simulate network conditions.
#
# When bw_mbit > 0:
#   root: tbf  (token-bucket rate limiter) — caps total bandwidth
#   child: netem (delay + loss)
#   → Background noise + etcd traffic share the cap.  Fill it with UDP flood
#     via network-noise and etcd packets will queue, triggering elections.
#
# When bw_mbit == 0 (or unset):
#   root: netem only (just adds delay/loss, no bandwidth cap)

LATENCY=${1:-0ms}
JITTER=${2:-0ms}
LOSS=${3:-0}
INTERFACE=${4:-lo}
BW_MBIT=${5:-0}

# Clear existing rules
tc qdisc del dev "$INTERFACE" root 2>/dev/null || true

no_latency=false
no_loss=false
[[ "$LATENCY" == "0ms" || "$LATENCY" == "0" ]] && no_latency=true
[[ "$LOSS" == "0" || "$LOSS" == "0.0" ]]       && no_loss=true

# Build the netem argument string
netem_args="delay ${LATENCY} ${JITTER} distribution normal"
$no_loss || netem_args="$netem_args loss ${LOSS}%"

if [[ "$BW_MBIT" != "0" && "$BW_MBIT" != "0.0" && -n "$BW_MBIT" ]]; then
    # ── Bandwidth cap + delay/loss ────────────────────────────────────────────
    # tbf burst must be >= rate × min_latency (use 20ms as a safe floor)
    # burst_bytes = BW_Mbit/s × 1e6/8 × 0.020
    burst_kb=$(awk -v bw="$BW_MBIT" 'BEGIN{printf "%dk", bw * 1e6/8 * 0.020 / 1024}')

    # tbf latency: how long a packet can wait in the tbf queue (200ms budget)
    tc qdisc add dev "$INTERFACE" root handle 1: tbf \
        rate "${BW_MBIT}mbit" burst "$burst_kb" latency 200ms

    # Attach netem as a child for delay/loss
    if $no_latency && $no_loss; then
        echo "Applied: bw=${BW_MBIT}mbit (no delay/loss) on $INTERFACE"
    else
        tc qdisc add dev "$INTERFACE" parent 1:1 handle 10: netem $netem_args
        echo "Applied: bw=${BW_MBIT}mbit delay=$LATENCY jitter=$JITTER loss=$LOSS% on $INTERFACE"
    fi
else
    # ── Delay/loss only, no bandwidth cap ────────────────────────────────────
    if $no_latency && $no_loss; then
        echo "No network impairment applied"
    else
        tc qdisc add dev "$INTERFACE" root netem $netem_args
        echo "Applied: delay=$LATENCY jitter=$JITTER loss=$LOSS% on $INTERFACE"
    fi
fi
