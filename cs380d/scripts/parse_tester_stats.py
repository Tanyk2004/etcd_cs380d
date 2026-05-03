#!/usr/bin/env python3
"""
parse_tester_stats.py <tester_log> [<output_csv>]

Parses traffic-sim and noise-client [stats] log lines into a structured CSV
with one row per second. When multiple noise-client instances write to the same
log file (merged with >>), lines sharing the same ts= timestamp are summed into
one row so ops/s reflects aggregate throughput across all instances.

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
  lat_stalled     - ops with lat >= stall-ms (election-induced stall indicator)
  error_rate      - errors_per_s / ops_per_s (0..1)
"""

import re
import sys
import csv
from collections import defaultdict
from pathlib import Path

# Matches noise-client lines with ts= field:
#   [stats] ts=1234567890 profile=noise       ops/s=8234  put=8100 get=0 ...
# Also matches legacy traffic-sim lines without ts= field:
#   [stats] profile=spike           ops/s=8234  put=8100 get=0 ...
STATS_RE = re.compile(
    r'\[stats\]\s+'
    r'(?:ts=(\d+)\s+)?'                    # optional unix timestamp (group 1)
    r'profile=(\S+)\s+'                    # group 2
    r'ops/s=\s*(\d+)\s+'                   # group 3
    r'put=\s*(\d+)\s+'                     # group 4
    r'get=\s*(\d+)\s+'                     # group 5
    r'del=\s*\d+\s+'
    r'txn=\s*\d+\s+'
    r'lease=\s*\d+\s+'
    r'err=\s*(\d+)\s+'                     # group 6
    r'(?:avg_ms=\s*([\d.]+)\s+)?'          # group 7 — optional average latency (ms)
    r'\|.*?lat\(u1ms=(\d+)\s+u10ms=(\d+)\s+u100ms=(\d+)\s+slow=(\d+)'
    r'(?:\s+stalled=(\d+))?'               # group 13 — optional (backward compatible)
    r'\)'
    # groups 8-12, optional 13
)

FIELDS = [
    'sample', 'ts', 'profile', 'ops_per_s', 'puts_per_s', 'gets_per_s',
    'errors_per_s', 'avg_lat_ms', 'lat_u1ms', 'lat_u10ms', 'lat_u100ms',
    'lat_slow', 'lat_stalled', 'error_rate',
]


def parse(log_path: Path, out_path: Path):
    # Keyed by (ts, profile) for timestamped lines; None key for legacy lines.
    # Value: summed numeric fields.
    ts_buckets: dict = defaultdict(lambda: {
        'ops': 0, 'puts': 0, 'gets': 0, 'errs': 0,
        'avg_ms_sum': 0.0, 'avg_ms_count': 0,
        'u1': 0, 'u10': 0, 'u100': 0, 'slow': 0, 'stalled': 0,
    })
    ts_order = []   # insertion-order list of (ts, profile) keys
    legacy_rows = []  # for lines without ts=

    try:
        with open(log_path) as f:
            for line in f:
                m = STATS_RE.search(line)
                if not m:
                    continue
                ts_raw   = m.group(1)   # None if no ts= field
                profile  = m.group(2).strip()
                ops      = int(m.group(3))
                puts     = int(m.group(4))
                gets     = int(m.group(5))
                errs     = int(m.group(6))
                avg_ms   = float(m.group(7)) if m.group(7) is not None else 0.0
                u1       = int(m.group(8))
                u10      = int(m.group(9))
                u100     = int(m.group(10))
                slow     = int(m.group(11))
                stalled  = int(m.group(12)) if m.group(12) is not None else 0

                if ts_raw is not None:
                    key = (int(ts_raw), profile)
                    if key not in ts_buckets:
                        ts_order.append(key)
                    b = ts_buckets[key]
                    b['ops']          += ops
                    b['puts']         += puts
                    b['gets']         += gets
                    b['errs']         += errs
                    if avg_ms > 0:
                        b['avg_ms_sum']   += avg_ms
                        b['avg_ms_count'] += 1
                    b['u1']           += u1
                    b['u10']          += u10
                    b['u100']         += u100
                    b['slow']         += slow
                    b['stalled']      += stalled
                else:
                    # Legacy format: no timestamp — keep as separate sample
                    legacy_rows.append({
                        'profile': profile,
                        'ops': ops, 'puts': puts, 'gets': gets, 'errs': errs,
                        'avg_ms': avg_ms,
                        'u1': u1, 'u10': u10, 'u100': u100, 'slow': slow,
                        'stalled': stalled,
                    })
    except FileNotFoundError:
        return

    rows = []

    # Emit timestamped rows in arrival order
    for key in ts_order:
        ts_val, profile = key
        b = ts_buckets[key]
        ops = b['ops']
        errs = b['errs']
        avg_ms = round(b['avg_ms_sum'] / b['avg_ms_count'], 2) if b['avg_ms_count'] > 0 else 0.0
        rows.append({
            'sample':       len(rows),
            'ts':           ts_val,
            'profile':      profile,
            'ops_per_s':    ops,
            'puts_per_s':   b['puts'],
            'gets_per_s':   b['gets'],
            'errors_per_s': errs,
            'avg_lat_ms':   avg_ms,
            'lat_u1ms':     b['u1'],
            'lat_u10ms':    b['u10'],
            'lat_u100ms':   b['u100'],
            'lat_slow':     b['slow'],
            'lat_stalled':  b['stalled'],
            'error_rate':   round(errs / ops, 4) if ops > 0 else 0.0,
        })

    # Emit legacy rows (no ts field)
    for r in legacy_rows:
        ops = r['ops']
        errs = r['errs']
        rows.append({
            'sample':       len(rows),
            'ts':           0,
            'profile':      r['profile'],
            'ops_per_s':    ops,
            'puts_per_s':   r['puts'],
            'gets_per_s':   r['gets'],
            'errors_per_s': errs,
            'avg_lat_ms':   r.get('avg_ms', 0.0),
            'lat_u1ms':     r['u1'],
            'lat_u10ms':    r['u10'],
            'lat_u100ms':   r['u100'],
            'lat_slow':     r['slow'],
            'lat_stalled':  r['stalled'],
            'error_rate':   round(errs / ops, 4) if ops > 0 else 0.0,
        })

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
