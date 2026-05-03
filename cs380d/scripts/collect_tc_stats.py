#!/usr/bin/env python3
"""
collect_tc_stats.py <output_dir> [interface] [interval_s]

Samples `tc -s qdisc show dev <interface>` at <interval_s> (default 0.1 s)
and appends rows to <output_dir>/tc_stats.csv.  Runs until SIGINT/SIGTERM.

CSV columns:
  timestamp_ms   - unix time in milliseconds (sub-second resolution)
  type           - qdisc type (tbf, netem, fq, ...)
  handle         - qdisc handle string (e.g. "1:", "10:")
  sent_bytes     - cumulative bytes forwarded through this qdisc
  sent_pkts      - cumulative packets forwarded
  dropped        - cumulative packets dropped (queue overflow)
  overlimits     - cumulative packets rate-limited (queued, not dropped)
  backlog_bytes  - instantaneous bytes sitting in the queue right now
  backlog_pkts   - instantaneous packets in queue right now
"""

import sys
import os
import csv
import time
import subprocess
import re
import signal
from pathlib import Path

FIELDS = [
    "timestamp_ms", "type", "handle",
    "sent_bytes", "sent_pkts", "dropped", "overlimits",
    "backlog_bytes", "backlog_pkts",
]


def parse_tc(text: str) -> list[dict]:
    """Parse `tc -s qdisc show` output; return one dict per qdisc."""
    records = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = re.match(r"qdisc\s+(\S+)\s+(\S+):\s+", lines[i].strip())
        if m:
            rec = {
                "type": m.group(1), "handle": m.group(2),
                "sent_bytes": 0, "sent_pkts": 0,
                "dropped": 0, "overlimits": 0,
                "backlog_bytes": 0, "backlog_pkts": 0,
            }
            for j in range(i + 1, min(i + 5, len(lines))):
                s = lines[j].strip()
                sm = re.match(
                    r"Sent\s+(\d+)\s+bytes\s+(\d+)\s+pkt\s+"
                    r"\(dropped\s+(\d+),\s+overlimits\s+(\d+)",
                    s,
                )
                if sm:
                    rec["sent_bytes"]  = int(sm.group(1))
                    rec["sent_pkts"]   = int(sm.group(2))
                    rec["dropped"]     = int(sm.group(3))
                    rec["overlimits"]  = int(sm.group(4))
                bm = re.match(r"backlog\s+(\d+)b\s+(\d+)p", s)
                if bm:
                    rec["backlog_bytes"] = int(bm.group(1))
                    rec["backlog_pkts"]  = int(bm.group(2))
            records.append(rec)
        i += 1
    return records


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    out_dir  = Path(sys.argv[1])
    iface    = sys.argv[2] if len(sys.argv) > 2 else "lo"
    interval = float(sys.argv[3]) if len(sys.argv) > 3 else 0.1

    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "tc_stats.csv"

    running = True
    def _stop(sig, frame):
        nonlocal running
        running = False
    signal.signal(signal.SIGINT,  _stop)
    signal.signal(signal.SIGTERM, _stop)

    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        f.flush()

        next_tick = time.monotonic()
        while running:
            ts_ms = int(time.time() * 1000)
            try:
                out = subprocess.run(
                    ["tc", "-s", "qdisc", "show", "dev", iface],
                    capture_output=True, text=True, timeout=0.05,
                )
                for rec in parse_tc(out.stdout):
                    rec["timestamp_ms"] = ts_ms
                    writer.writerow(rec)
                f.flush()
            except Exception:
                pass

            next_tick += interval
            sleep_for = next_tick - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)

    print(f"[collect_tc_stats] wrote {csv_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
