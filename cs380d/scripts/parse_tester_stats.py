#!/usr/bin/env python3
"""
parse_tester_stats.py <tester_log> [<output_csv>]

Parses traffic-sim [stats] log lines into a structured CSV with one row per
stats sample (~1 second). Used by plot_results.py to show client-side
throughput and error rate alongside server-side raft metrics.

Output columns:
  sample          - sequential sample number (proxy for time offset)
  profile         - active workload profile name
  ops_per_s       - total operations per second
  puts_per_s      - put ops per second
  gets_per_s      - get ops per second
  errors_per_s    - errors per second
  lat_u1ms        - ops completing under 1ms
  lat_u10ms       - ops completing under 10ms
  lat_u100ms      - ops completing under 100ms
  lat_slow        - ops taking over 100ms (indicates severe latency)
  error_rate      - errors_per_s / ops_per_s (0..1)
"""

import re
import sys
import csv
from pathlib import Path

# Matches lines like:
#   2026-04-09T12:00:01.234Z  INFO ...: [stats] profile=spike           ops/s=8234  ...
STATS_RE = re.compile(
    r'\[stats\]\s+'
    r'profile=(\S+)\s+'
    r'ops/s=\s*(\d+)\s+'
    r'put=\s*(\d+)\s+'
    r'get=\s*(\d+)\s+'
    r'del=\s*\d+\s+'
    r'txn=\s*\d+\s+'
    r'lease=\s*\d+\s+'
    r'err=\s*(\d+)\s+'
    r'\|.*?lat\(u1ms=(\d+)\s+u10ms=(\d+)\s+u100ms=(\d+)\s+slow=(\d+)\)'
)

FIELDS = [
    'sample', 'profile', 'ops_per_s', 'puts_per_s', 'gets_per_s',
    'errors_per_s', 'lat_u1ms', 'lat_u10ms', 'lat_u100ms', 'lat_slow',
    'error_rate',
]


def parse(log_path: Path, out_path: Path):
    rows = []
    try:
        with open(log_path) as f:
            for i, line in enumerate(f):
                m = STATS_RE.search(line)
                if not m:
                    continue
                ops   = int(m.group(2))
                errs  = int(m.group(4))
                rows.append({
                    'sample':      len(rows),
                    'profile':     m.group(1).strip(),
                    'ops_per_s':   ops,
                    'puts_per_s':  int(m.group(3)),
                    'gets_per_s':  int(m.group(5)) if len(m.groups()) >= 5 else 0,
                    'errors_per_s': errs,
                    'lat_u1ms':    int(m.group(5)),
                    'lat_u10ms':   int(m.group(6)),
                    'lat_u100ms':  int(m.group(7)),
                    'lat_slow':    int(m.group(8)),
                    'error_rate':  round(errs / ops, 4) if ops > 0 else 0.0,
                })
    except FileNotFoundError:
        return

    if not rows:
        return

    with open(out_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"[parse_tester_stats] {len(rows)} samples → {out_path}")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    log = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else log.with_suffix('.csv')
    parse(log, out)
