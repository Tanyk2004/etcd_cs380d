#!/usr/bin/env python3
"""
plot_results.py <results-dir>

Generates a PDF report (and individual PNGs) from a run_test_suite.sh results
directory.  Focuses on the causal chain:

    application pressure (proposals_pending)
        → heartbeat send failures (hb_send_failures)
            → leader elections (leader_changes delta)

Usage:
    python3 cs380d/scripts/plot_results.py ./results/20260409_120000
"""

import sys
import json
import glob
from pathlib import Path

import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.backends.backend_pdf import PdfPages
import numpy as np

# ── colour palette ────────────────────────────────────────────────────────────
C_PRESSURE  = "#e07b39"   # orange  – proposals pending
C_HB_FAIL   = "#c0392b"   # red     – heartbeat failures
C_ELECTION  = "#8e44ad"   # purple  – leader election events
C_FAILED    = "#e74c3c"   # bright red – failed proposals
C_SUCCESS   = "#27ae60"   # green   – success rate
C_PENDING   = "#f39c12"   # yellow-orange

SCENARIO_ORDER = [
    "baseline_steady_state",
    "burst_recovery",
    "gradual_rampup",
    "competing_traffic",
    "wan_simulation",
    "sustained_write_flood",
]


# ── helpers ───────────────────────────────────────────────────────────────────

def load_scenario(scenario_dir: Path):
    """Return (df, analysis) for a scenario directory, or (None, None)."""
    csv = scenario_dir / "timeseries.csv"
    ana = scenario_dir / "analysis.json"
    if not csv.exists():
        return None, None

    df = pd.read_csv(csv)
    # Fill any missing values with forward-fill then 0
    df = df.ffill().fillna(0)

    # Normalise timestamp to seconds-from-start
    df["t"] = df["timestamp"] - df["timestamp"].iloc[0]
    df["_epoch"] = float(df["timestamp"].iloc[0])

    # Derive election events (points where leader_changes increments).
    # Use > 0.5 threshold to be robust against float noise; also handle counter
    # resets (node restart) where the value drops then climbs again.
    raw_diff = df["leader_changes"].diff().fillna(0)
    df["election_event"] = raw_diff.where(raw_diff > 0.5, 0)

    # Rate of heartbeat failures per second (finite diff)
    df["hb_fail_rate"] = df["hb_send_failures"].diff().fillna(0).clip(lower=0)

    analysis = json.loads(ana.read_text()) if ana.exists() else {}
    return df, analysis


def load_ops_stats(scenario_dir: Path, epoch: float = 0) -> pd.DataFrame:
    """Load and concatenate all ops_phaseN.csv files into one DataFrame.

    When epoch is provided (unix timestamp of the first timeseries row), the
    returned DataFrame gains a column ``t`` (seconds from scenario start) so
    it can be plotted on the same axis as timeseries data.  Multiple
    noise-client profiles at the same second are summed into one row.
    """
    frames = []
    for csv_path in sorted(scenario_dir.glob("ops_phase*.csv")):
        try:
            f = pd.read_csv(csv_path)
        except Exception:
            continue
        if f.empty:
            continue
        frames.append(f)
    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)

    # If ts column is present and non-zero, aggregate by second across profiles.
    if "ts" in df.columns and df["ts"].gt(0).any():
        agg = {
            "ops_per_s":    "sum",
            "puts_per_s":   "sum",
            "gets_per_s":   "sum",
            "errors_per_s": "sum",
            "lat_u1ms":     "sum",
            "lat_u10ms":    "sum",
            "lat_u100ms":   "sum",
            "lat_slow":     "sum",
            "error_rate":   "mean",
        }
        if "lat_stalled" in df.columns:
            agg["lat_stalled"] = "sum"
        # avg_lat_ms is a weighted average across profiles — compute manually
        # after groupby so we don't just mean the per-profile means.
        has_avg = "avg_lat_ms" in df.columns and df["avg_lat_ms"].gt(0).any()
        if has_avg:
            df["_lat_sum"] = df["avg_lat_ms"] * df["ops_per_s"]
            agg["_lat_sum"]  = "sum"
            agg["ops_per_s"] = "sum"   # already there; keep for weight
        df = df[df["ts"] > 0].groupby("ts", as_index=False).agg(agg)
        if has_avg:
            df["avg_lat_ms"] = (df["_lat_sum"] / df["ops_per_s"].replace(0, np.nan)).fillna(0)
            df.drop(columns=["_lat_sum"], inplace=True)
        df = df.sort_values("ts").reset_index(drop=True)
        df["sample"] = range(len(df))
        df["t"] = df["ts"] - (epoch if epoch > 0 else df["ts"].iloc[0])
    else:
        df["t"] = df["sample"].astype(float)

    return df


def election_times(df):
    """Return t values where an election was observed."""
    return df.loc[df["election_event"] > 0, "t"].tolist()


def add_election_vlines(ax, etimes, label=True):
    for i, t in enumerate(etimes):
        ax.axvline(t, color=C_ELECTION, linewidth=1.2, linestyle="--",
                   alpha=0.8, label="election" if (label and i == 0) else None)


def pass_fail_color(ok):
    return C_SUCCESS if ok else C_HB_FAIL


# ── per-scenario: causal-chain plot ──────────────────────────────────────────

def plot_causal_chain(df, analysis, name, ax_top, ax_mid, ax_bot):
    """
    Three vertically-stacked axes sharing the x-axis:
      top : proposals_pending  (application pressure)
      mid : hb_send_failures rate  (heartbeat drops)
      bot : cumulative leader_changes + election event markers
    Election vlines are drawn across all three to show the causal link.
    """
    etimes = election_times(df)

    # ── top: application pressure ────────────────────────────────────────────
    ax_top.fill_between(df["t"], df["proposals_pending"],
                        color=C_PRESSURE, alpha=0.35, linewidth=0)
    ax_top.plot(df["t"], df["proposals_pending"],
                color=C_PRESSURE, linewidth=1.2, label="proposals pending")
    ax_top.set_ylabel("Proposals\nPending", fontsize=8)
    ax_top.set_title(f"{name}", fontsize=10, fontweight="bold")
    add_election_vlines(ax_top, etimes)
    ax_top.legend(fontsize=7, loc="upper left")
    ax_top.tick_params(labelbottom=False)

    # ── mid: heartbeat failure rate ───────────────────────────────────────────
    ax_mid.fill_between(df["t"], df["hb_fail_rate"],
                        color=C_HB_FAIL, alpha=0.35, linewidth=0)
    ax_mid.plot(df["t"], df["hb_fail_rate"],
                color=C_HB_FAIL, linewidth=1.2, label="hb failures / s")
    ax_mid.set_ylabel("HB Failures\n(rate/s)", fontsize=8)
    add_election_vlines(ax_mid, etimes, label=False)
    ax_mid.legend(fontsize=7, loc="upper left")
    ax_mid.tick_params(labelbottom=False)

    # ── bot: cumulative elections ─────────────────────────────────────────────
    ax_bot.step(df["t"], df["leader_changes"],
                color=C_ELECTION, linewidth=1.5, where="post",
                label="leader changes (cumulative)")
    # Mark each election with a scatter point
    if etimes:
        yvals = [df.iloc[(df["t"] - t).abs().argmin()]["leader_changes"]
                 for t in etimes]
        ax_bot.scatter(etimes, yvals, color=C_ELECTION, zorder=5, s=40,
                       label=f"{len(etimes)} election(s)")
    ax_bot.set_ylabel("Leader\nChanges", fontsize=8)
    ax_bot.set_xlabel("Time (s)", fontsize=8)
    add_election_vlines(ax_bot, etimes, label=False)
    ax_bot.legend(fontsize=7, loc="upper left")


# ── per-scenario: supporting metrics ─────────────────────────────────────────

C_WAL     = "#2980b9"   # blue  – WAL fsync latency
C_BACKEND = "#16a085"   # teal  – backend commit latency


def plot_supporting(df, analysis, name, axes):
    """
    axes[0]: proposals_failed rate
    axes[1]: proposals_pending (smoothed)
    axes[2]: WAL fsync + backend commit latency (if present)
    """
    etimes = election_times(df)
    has_latency = "wal_fsync_avg_ms" in df.columns and \
                  df["wal_fsync_avg_ms"].abs().sum() > 0

    df["failed_rate"] = df["proposals_failed"].diff().fillna(0).clip(lower=0)

    ax0, ax1, ax2 = axes

    ax0.fill_between(df["t"], df["failed_rate"],
                     color=C_FAILED, alpha=0.4, linewidth=0)
    ax0.plot(df["t"], df["failed_rate"],
             color=C_FAILED, linewidth=1.2, label="failed proposals / s")
    ax0.set_ylabel("Failed\nProposals/s", fontsize=8)
    ax0.set_title(f"{name} – supporting metrics", fontsize=9)
    add_election_vlines(ax0, etimes)
    ax0.legend(fontsize=7)
    ax0.tick_params(labelbottom=False)

    smooth = df["proposals_pending"].rolling(5, min_periods=1).mean()
    ax1.plot(df["t"], df["proposals_pending"],
             color=C_PENDING, alpha=0.3, linewidth=0.8)
    ax1.plot(df["t"], smooth,
             color=C_PENDING, linewidth=1.5, label="pending (5 s avg)")
    ax1.set_ylabel("Proposals\nPending", fontsize=8)
    add_election_vlines(ax1, etimes, label=False)
    ax1.legend(fontsize=7)
    ax1.tick_params(labelbottom=False)

    # Latency axes
    if has_latency:
        wal_smooth     = df["wal_fsync_avg_ms"].rolling(5, min_periods=1).mean()
        backend_smooth = df["backend_commit_avg_ms"].rolling(5, min_periods=1).mean()
        ax2.plot(df["t"], wal_smooth,
                 color=C_WAL, linewidth=1.5, label="WAL fsync avg (ms)")
        ax2.plot(df["t"], backend_smooth,
                 color=C_BACKEND, linewidth=1.5, label="backend commit avg (ms)")
        ax2.set_ylabel("Latency\n(ms)", fontsize=8)
        add_election_vlines(ax2, etimes, label=False)
        ax2.legend(fontsize=7)
    else:
        ax2.text(0.5, 0.5, "latency data not available\n(run collect_metrics.sh again)",
                 ha="center", va="center", transform=ax2.transAxes,
                 fontsize=8, color="grey")
        ax2.set_ylabel("Latency\n(ms)", fontsize=8)

    ax2.set_xlabel("Time (s)", fontsize=8)


# ── cross-scenario summary ────────────────────────────────────────────────────

def plot_summary(scenarios_data, pdf):
    """Bar chart comparing key metrics across all scenarios."""
    names      = [s["name"]                for s in scenarios_data]
    elections  = [s["analysis"].get("elections", 0)            for s in scenarios_data]
    hb_total   = [s["df"]["hb_send_failures"].iloc[-1]
                  - s["df"]["hb_send_failures"].iloc[0]        for s in scenarios_data]
    max_pend   = [s["df"]["proposals_pending"].max()           for s in scenarios_data]
    success    = [s["analysis"].get("success_rate", 1.0) * 100 for s in scenarios_data]

    # Pass/fail colours per scenario
    bar_colors = []
    for s in scenarios_data:
        a = s["analysis"]
        ok = a.get("elections_pass", True) and \
             a.get("latency_pass", True) and \
             a.get("success_pass", True)
        bar_colors.append(C_SUCCESS if ok else C_HB_FAIL)

    short = [n.replace("_", "\n") for n in names]
    x = np.arange(len(names))
    w = 0.22

    fig, axes = plt.subplots(1, 4, figsize=(16, 5))
    fig.suptitle("Cross-scenario summary", fontsize=13, fontweight="bold")

    for ax, vals, title, color, fmt in zip(
        axes,
        [elections, hb_total, max_pend, success],
        ["Leader Elections", "Total HB Failures", "Max Proposals Pending", "Success Rate (%)"],
        [C_ELECTION, C_HB_FAIL, C_PRESSURE, C_SUCCESS],
        ["{:.0f}", "{:.0f}", "{:.0f}", "{:.1f}"],
    ):
        bars = ax.bar(x, vals, color=bar_colors if title == "Leader Elections" else color,
                      alpha=0.8, edgecolor="white", linewidth=0.5)
        ax.set_xticks(x)
        ax.set_xticklabels(short, fontsize=7)
        ax.set_title(title, fontsize=9)
        for bar, v in zip(bars, vals):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() * 1.02,
                    fmt.format(v), ha="center", va="bottom", fontsize=7)

    # Legend
    from matplotlib.patches import Patch
    axes[0].legend(handles=[Patch(color=C_SUCCESS, label="PASS"),
                             Patch(color=C_HB_FAIL, label="FAIL")],
                   fontsize=8, loc="upper right")

    fig.tight_layout()
    pdf.savefig(fig)
    plt.close(fig)


# ── pressure→election correlation scatter ────────────────────────────────────

def plot_pressure_election_correlation(scenarios_data, pdf):
    """
    One scatter point per second per scenario.
    X = proposals_pending, Y = hb_fail_rate.
    Points are coloured by whether an election occurred within the next 5 s.
    Helps visualise the threshold at which pressure causes heartbeat drops.
    """
    fig, ax = plt.subplots(figsize=(9, 6))

    for s in scenarios_data:
        df = s["df"].copy()
        df["hb_fail_rate"] = df["hb_send_failures"].diff().fillna(0).clip(lower=0)
        # Flag rows that precede an election within 5 seconds
        df["near_election"] = False
        for t in election_times(df):
            df.loc[(df["t"] >= t - 5) & (df["t"] <= t), "near_election"] = True

        normal = df[~df["near_election"]]
        danger = df[df["near_election"]]

        ax.scatter(normal["proposals_pending"], normal["hb_fail_rate"],
                   s=10, alpha=0.3, color="#7f8c8d", linewidths=0)
        ax.scatter(danger["proposals_pending"], danger["hb_fail_rate"],
                   s=18, alpha=0.7, color=C_ELECTION, linewidths=0,
                   label=f"{s['name']}" if len(danger) > 0 else None)

    from matplotlib.lines import Line2D
    legend_elements = [
        Line2D([0], [0], marker='o', color='w', markerfacecolor="#7f8c8d",
               markersize=7, label="Normal operation"),
        Line2D([0], [0], marker='o', color='w', markerfacecolor=C_ELECTION,
               markersize=7, label="Within 5 s of election"),
    ]
    ax.legend(handles=legend_elements, fontsize=9)
    ax.set_xlabel("Proposals Pending (application pressure)", fontsize=10)
    ax.set_ylabel("Heartbeat Failures / s", fontsize=10)
    ax.set_title(
        "Application pressure vs heartbeat failures\n"
        "(purple = seconds leading up to a leader election)",
        fontsize=11, fontweight="bold",
    )
    fig.tight_layout()
    pdf.savefig(fig)
    plt.close(fig)


# ── throughput degradation ────────────────────────────────────────────────────

C_OPS       = "#2ecc71"   # green  – ops/s
C_ERR       = "#e74c3c"   # red    – errors/s
C_COMMIT    = "#3498db"   # blue   – commit rate
C_SLOW      = "#e67e22"   # orange – slow ops
C_ELECTION_BG = "#f9ebea" # light red – election period shading


def shade_elections(ax, df):
    """Shade periods where has_leader == 0 (cluster is electing)."""
    if "has_leader" not in df.columns:
        return
    in_election = False
    start = None
    for _, row in df.iterrows():
        if row["has_leader"] == 0 and not in_election:
            in_election = True
            start = row["t"]
        elif row["has_leader"] != 0 and in_election:
            in_election = False
            ax.axvspan(start, row["t"], color=C_ELECTION_BG, alpha=0.6, zorder=0,
                       label="election period" if start == df.loc[df["has_leader"] == 0, "t"].iloc[0] else None)
    if in_election:
        ax.axvspan(start, df["t"].iloc[-1], color=C_ELECTION_BG, alpha=0.6, zorder=0)


def plot_throughput_degradation(df, ops_df, analysis, name):
    """
    Three-panel page proving the raft performance penalty:

    Top   : Client ops/s + errors/s from traffic-sim (direct throughput measure).
            Red shading = period with no cluster leader (election in progress).
    Middle: Server-side commit rate (proposals_committed/s) + slow_apply rate.
            Drop here confirms writes are blocked, not just client-side throttling.
    Bottom: Peer sent bytes/s (network utilisation) + proposals_pending.
            Proves the pipeline is saturated, not just randomly slow.
    """
    etimes = election_times(df)
    has_ops = not ops_df.empty and "ops_per_s" in ops_df.columns
    has_avg_lat = has_ops and "avg_lat_ms" in ops_df.columns and ops_df["avg_lat_ms"].gt(0).any()

    # Derive server-side commit rate from raw counter
    df["commit_rate"] = df["proposals_committed_total"].diff().fillna(0).clip(lower=0)
    df["slow_apply_rate"] = df["slow_apply_total"].diff().fillna(0).clip(lower=0)
    df["peer_bytes_rate"] = df["peer_sent_bytes_total"].diff().fillna(0).clip(lower=0) / 1024  # KB/s

    n_panels = 4 if has_avg_lat else 3
    fig, axes = plt.subplots(n_panels, 1, figsize=(14, 4 * n_panels), sharex=True)
    ax_thr, ax_lat_client, ax_srv, ax_net = (axes if n_panels == 4
                                              else (axes[0], None, axes[1], axes[2]))
    fig.suptitle(
        f"{name} — throughput & raft performance penalty\n"
        f"({len(etimes)} election(s) detected — red shading = leaderless period)",
        fontsize=11, fontweight="bold",
    )

    # ── top: client throughput ────────────────────────────────────────────────
    # ops_df["t"] is seconds from scenario start (same epoch as df["t"]),
    # so election vlines align correctly with the client throughput trace.
    if has_ops and "t" in ops_df.columns:
        x_ops = ops_df["t"]
        ax_thr.fill_between(x_ops, ops_df["ops_per_s"],
                            color=C_OPS, alpha=0.25, linewidth=0)
        ax_thr.plot(x_ops, ops_df["ops_per_s"],
                    color=C_OPS, linewidth=1.5, label="ops / s (client)")
        ax_err = ax_thr.twinx()
        ax_err.fill_between(x_ops, ops_df["errors_per_s"],
                            color=C_ERR, alpha=0.3, linewidth=0)
        ax_err.plot(x_ops, ops_df["errors_per_s"],
                    color=C_ERR, linewidth=1.2, linestyle="--", label="errors / s")
        ax_err.set_ylabel("Errors / s", fontsize=8, color=C_ERR)

        # Shade stalled-op periods (lat_stalled > 0) — election-induced stalls
        if "lat_stalled" in ops_df.columns:
            stall_mask = ops_df["lat_stalled"] > 0
            if stall_mask.any():
                ax_thr.fill_between(x_ops, 0, ops_df["ops_per_s"].max(),
                                    where=stall_mask, color=C_ELECTION, alpha=0.15,
                                    zorder=0, label=f"election stalls (lat≥{ops_df['lat_stalled'].gt(0).sum()}s)")
        elif ops_df["lat_slow"].gt(0).any():
            ax_thr.fill_between(x_ops, 0, ops_df["ops_per_s"].max(),
                                where=ops_df["lat_slow"] > 0, color=C_SLOW, alpha=0.15,
                                zorder=0, label="slow ops (>100ms)")
        lines1, labels1 = ax_thr.get_legend_handles_labels()
        lines2, labels2 = ax_err.get_legend_handles_labels()
        ax_thr.legend(lines1 + lines2, labels1 + labels2, fontsize=7, loc="upper left")
    else:
        ax_thr.text(0.5, 0.5,
                    "No ops_phaseN.csv found\n(traffic-sim output not captured)",
                    ha="center", va="center", transform=ax_thr.transAxes,
                    fontsize=9, color="grey")

    ax_thr.set_ylabel("Ops / s", fontsize=8, color=C_OPS)
    ax_thr.set_title("Client-side throughput — drops during elections (aligned to server timeline)", fontsize=9)
    add_election_vlines(ax_thr, etimes)

    # ── client avg latency (only when avg_lat_ms is present) ─────────────────
    C_LAT = "#2980b9"   # blue
    if has_avg_lat and ax_lat_client is not None:
        x_ops = ops_df["t"]
        lat_smooth = ops_df["avg_lat_ms"].rolling(3, min_periods=1).mean()
        ax_lat_client.fill_between(x_ops, lat_smooth, color=C_LAT, alpha=0.2, linewidth=0)
        ax_lat_client.plot(x_ops, lat_smooth, color=C_LAT, linewidth=1.5, label="avg RTT (ms, 3s smooth)")
        # Shade stall periods so the correlation is visually explicit
        if "lat_stalled" in ops_df.columns and ops_df["lat_stalled"].gt(0).any():
            ax_lat_client.fill_between(x_ops, 0, lat_smooth.max() * 1.05,
                                       where=ops_df["lat_stalled"] > 0,
                                       color=C_ELECTION, alpha=0.15, zorder=0,
                                       label="election stall window")
        add_election_vlines(ax_lat_client, etimes, label=True)
        ax_lat_client.set_ylabel("Avg RTT\n(ms)", fontsize=8, color=C_LAT)
        ax_lat_client.set_title(
            "Client avg request latency — spikes when WLock stalls all handlers during election",
            fontsize=9,
        )
        ax_lat_client.legend(fontsize=7, loc="upper left")

    # ── server commit rate + slow applies ────────────────────────────────────
    ax_srv.fill_between(df["t"], df["commit_rate"],
                        color=C_COMMIT, alpha=0.25, linewidth=0)
    ax_srv.plot(df["t"], df["commit_rate"],
                color=C_COMMIT, linewidth=1.5, label="proposals committed / s")
    shade_elections(ax_srv, df)
    ax_slow = ax_srv.twinx()
    ax_slow.fill_between(df["t"], df["slow_apply_rate"],
                         color=C_SLOW, alpha=0.4, linewidth=0)
    ax_slow.plot(df["t"], df["slow_apply_rate"],
                 color=C_SLOW, linewidth=1.2, linestyle="--", label="slow applies / s")
    ax_slow.set_ylabel("Slow applies / s", fontsize=8, color=C_SLOW)
    add_election_vlines(ax_srv, etimes, label=False)
    lines1, labels1 = ax_srv.get_legend_handles_labels()
    lines2, labels2 = ax_slow.get_legend_handles_labels()
    ax_srv.legend(lines1 + lines2, labels1 + labels2, fontsize=7, loc="upper left")
    ax_srv.set_ylabel("Committed proposals / s", fontsize=8, color=C_COMMIT)
    ax_srv.set_title("Server-side commit rate — confirms writes stall, not just client throttling", fontsize=9)

    # ── bottom: peer bytes/s + proposals pending ──────────────────────────────
    ax_net.fill_between(df["t"], df["peer_bytes_rate"],
                        color="#9b59b6", alpha=0.25, linewidth=0)
    ax_net.plot(df["t"], df["peer_bytes_rate"],
                color="#9b59b6", linewidth=1.5, label="peer sent KB/s")
    shade_elections(ax_net, df)
    ax_pend = ax_net.twinx()
    ax_pend.plot(df["t"], df["proposals_pending"],
                 color=C_PRESSURE, linewidth=1.2, linestyle="--", label="proposals pending")
    ax_pend.set_ylabel("Proposals Pending", fontsize=8, color=C_PRESSURE)
    add_election_vlines(ax_net, etimes, label=False)
    lines1, labels1 = ax_net.get_legend_handles_labels()
    lines2, labels2 = ax_pend.get_legend_handles_labels()
    ax_net.legend(lines1 + lines2, labels1 + labels2, fontsize=7, loc="upper left")
    ax_net.set_ylabel("Peer sent KB/s", fontsize=8, color="#9b59b6")
    ax_net.set_title("Network utilisation — saturated pipeline blocks heartbeats", fontsize=9)
    ax_net.set_xlabel("Time (s)", fontsize=8)

    fig.tight_layout(rect=[0, 0, 1, 0.95])
    return fig


# ── tc queue saturation ───────────────────────────────────────────────────────

C_BACKLOG   = "#8e44ad"   # purple – backlog fill
C_DROPS     = "#c0392b"   # red    – drops/s
C_OVERLIMIT = "#e67e22"   # orange – overlimits/s
C_SENT      = "#2980b9"   # blue   – throughput


def plot_tc_queue(scenario_dir: Path, df, name):
    """
    Three-panel page showing network queue saturation over time.

    Top   : Instantaneous backlog_bytes in the tbf (or netem) qdisc — how full
            the queue is.  A horizontal reference line marks the queue burst
            capacity if derivable from the data.
    Middle: Drop rate and overlimit rate per second (derived from cumulative
            counter deltas) — directly shows when tc is throttling packets.
    Bottom: Sent throughput (KB/s through the qdisc).

    All panels share the x-axis (seconds from scenario start) and carry the
    election vlines from timeseries data so saturation events align with Raft.
    """
    tc_path = scenario_dir / "tc_stats.csv"
    if not tc_path.exists():
        return None

    try:
        tc = pd.read_csv(tc_path)
    except Exception:
        return None

    if tc.empty:
        return None

    # Prefer tbf (rate-limiting qdisc); fall back to netem, then any qdisc.
    for preferred in ("tbf", "netem"):
        sub = tc[tc["type"] == preferred]
        if not sub.empty:
            tc = sub.copy()
            qdisc_type = preferred
            break
    else:
        tc = tc.copy()
        qdisc_type = tc["type"].iloc[0] if not tc.empty else "unknown"

    # Align to scenario epoch from timeseries df
    epoch = float(df["_epoch"].iloc[0]) if "_epoch" in df.columns else 0
    tc["t"] = tc["timestamp_ms"] / 1000.0 - (epoch if epoch > 0 else tc["timestamp_ms"].iloc[0] / 1000.0)

    # Sort by time, reset index
    tc = tc.sort_values("t").reset_index(drop=True)

    # Derive per-sample rates from cumulative counters (diff, clip negatives = resets)
    dt = tc["t"].diff().fillna(0.1).clip(lower=0.01)
    tc["drops_per_s"]      = tc["dropped"].diff().fillna(0).clip(lower=0) / dt
    tc["overlimits_per_s"] = tc["overlimits"].diff().fillna(0).clip(lower=0) / dt
    tc["sent_kb_per_s"]    = tc["sent_bytes"].diff().fillna(0).clip(lower=0) / dt / 1024

    # Smooth 1s rolling window (10 samples at 0.1s interval)
    w = 10
    tc["drops_s"]      = tc["drops_per_s"].rolling(w, min_periods=1).mean()
    tc["overlimits_s"] = tc["overlimits_per_s"].rolling(w, min_periods=1).mean()
    tc["sent_s"]       = tc["sent_kb_per_s"].rolling(w, min_periods=1).mean()

    etimes = election_times(df)

    fig, (ax_bl, ax_dr, ax_bw) = plt.subplots(3, 1, figsize=(14, 10), sharex=True)
    fig.suptitle(
        f"{name} — network queue saturation ({qdisc_type} qdisc on lo)\n"
        f"({len(etimes)} election(s) — purple dashed = election event)",
        fontsize=11, fontweight="bold",
    )

    # ── top: backlog bytes ────────────────────────────────────────────────────
    ax_bl.fill_between(tc["t"], tc["backlog_bytes"] / 1024,
                       color=C_BACKLOG, alpha=0.3, linewidth=0)
    ax_bl.plot(tc["t"], tc["backlog_bytes"] / 1024,
               color=C_BACKLOG, linewidth=1.0, label="backlog (KB)")
    # Burst capacity reference: 90th-percentile max as a proxy if we can't read tc params
    cap_kb = tc["backlog_bytes"].quantile(0.99) / 1024
    if cap_kb > 0:
        ax_bl.axhline(cap_kb, color=C_BACKLOG, linewidth=1.0, linestyle=":",
                      alpha=0.7, label=f"p99 peak {cap_kb:.0f} KB")
    ax_bl.set_ylabel("Queue backlog\n(KB)", fontsize=8)
    ax_bl.set_title("Queue depth — spikes indicate buffer-fill preceding drops", fontsize=9)
    add_election_vlines(ax_bl, etimes)
    ax_bl.legend(fontsize=7, loc="upper left")
    ax_bl.tick_params(labelbottom=False)

    # ── middle: drop + overlimit rate ─────────────────────────────────────────
    ax_dr.fill_between(tc["t"], tc["drops_s"],
                       color=C_DROPS, alpha=0.35, linewidth=0)
    ax_dr.plot(tc["t"], tc["drops_s"],
               color=C_DROPS, linewidth=1.2, label="drops / s")
    ax_ov = ax_dr.twinx()
    ax_ov.fill_between(tc["t"], tc["overlimits_s"],
                       color=C_OVERLIMIT, alpha=0.25, linewidth=0)
    ax_ov.plot(tc["t"], tc["overlimits_s"],
               color=C_OVERLIMIT, linewidth=1.0, linestyle="--", label="overlimits / s")
    ax_ov.set_ylabel("Overlimits / s", fontsize=8, color=C_OVERLIMIT)
    add_election_vlines(ax_dr, etimes, label=False)
    lines1, lbl1 = ax_dr.get_legend_handles_labels()
    lines2, lbl2 = ax_ov.get_legend_handles_labels()
    ax_dr.legend(lines1 + lines2, lbl1 + lbl2, fontsize=7, loc="upper left")
    ax_dr.set_ylabel("Drops / s", fontsize=8, color=C_DROPS)
    ax_dr.set_title("Drop & overlimit rates — packet loss when queue overflows", fontsize=9)
    ax_dr.tick_params(labelbottom=False)

    # ── bottom: sent throughput ───────────────────────────────────────────────
    ax_bw.fill_between(tc["t"], tc["sent_s"],
                       color=C_SENT, alpha=0.25, linewidth=0)
    ax_bw.plot(tc["t"], tc["sent_s"],
               color=C_SENT, linewidth=1.2, label="sent KB/s")
    add_election_vlines(ax_bw, etimes, label=False)
    ax_bw.set_ylabel("Throughput\n(KB/s)", fontsize=8)
    ax_bw.set_title("Sent throughput — drops when queue is saturated", fontsize=9)
    ax_bw.set_xlabel("Time (s)", fontsize=8)
    ax_bw.legend(fontsize=7, loc="upper left")

    fig.tight_layout(rect=[0, 0, 1, 0.95])
    return fig


# ── latency vs elections ──────────────────────────────────────────────────────

def plot_latency_vs_elections(df, name):
    """
    Three-panel page focused on latency around election events.

    Top   : WAL fsync latency + backend commit latency (ms) with election vlines.
    Middle: proposals_pending overlaid with hb_fail_rate on twin axes — shows
            the pressure that precedes each latency spike.
    Bottom: Per-election zoom strips (±20 s window around each election).
            If no elections occurred a placeholder message is shown instead.
    """
    etimes = election_times(df)
    has_latency = "wal_fsync_avg_ms" in df.columns and \
                  df["wal_fsync_avg_ms"].abs().sum() > 0

    fig = plt.figure(figsize=(14, 10))
    fig.suptitle(
        f"{name} — latency vs leader elections  "
        f"({'%d election(s)' % len(etimes) if etimes else 'no elections detected'})",
        fontsize=11, fontweight="bold",
    )

    gs = gridspec.GridSpec(3, 1, figure=fig, hspace=0.35,
                           height_ratios=[2, 1.5, 2])

    # ── top: latency timeseries ───────────────────────────────────────────────
    ax_lat = fig.add_subplot(gs[0])
    if has_latency:
        wal_s  = df["wal_fsync_avg_ms"].rolling(3, min_periods=1).mean()
        bck_s  = df["backend_commit_avg_ms"].rolling(3, min_periods=1).mean()
        ax_lat.fill_between(df["t"], wal_s, alpha=0.2, color=C_WAL)
        ax_lat.plot(df["t"], wal_s, color=C_WAL, linewidth=1.5,
                    label="WAL fsync avg (ms)")
        ax_lat.fill_between(df["t"], bck_s, alpha=0.2, color=C_BACKEND)
        ax_lat.plot(df["t"], bck_s, color=C_BACKEND, linewidth=1.5,
                    label="backend commit avg (ms)")
    else:
        ax_lat.text(0.5, 0.5, "latency columns not in CSV\n(re-run with updated collect_metrics.sh)",
                    ha="center", va="center", transform=ax_lat.transAxes,
                    fontsize=9, color="grey")
    add_election_vlines(ax_lat, etimes)
    ax_lat.set_ylabel("Latency (ms)", fontsize=8)
    ax_lat.set_title("Disk latency — spikes here indicate I/O pressure blocking heartbeats",
                     fontsize=9)
    ax_lat.legend(fontsize=8, loc="upper left")

    # ── middle: proposals_pending + hb_fail_rate ──────────────────────────────
    ax_pres = fig.add_subplot(gs[1], sharex=ax_lat)
    ax_hb   = ax_pres.twinx()
    ax_pres.fill_between(df["t"], df["proposals_pending"],
                         alpha=0.25, color=C_PRESSURE)
    ax_pres.plot(df["t"], df["proposals_pending"],
                 color=C_PRESSURE, linewidth=1.2, label="proposals pending")
    ax_hb.plot(df["t"], df["hb_fail_rate"],
               color=C_HB_FAIL, linewidth=1.2, linestyle=":", label="HB failures/s")
    ax_pres.set_ylabel("Proposals Pending", fontsize=8, color=C_PRESSURE)
    ax_hb.set_ylabel("HB Failures/s", fontsize=8, color=C_HB_FAIL)
    add_election_vlines(ax_pres, etimes, label=False)
    lines1, labels1 = ax_pres.get_legend_handles_labels()
    lines2, labels2 = ax_hb.get_legend_handles_labels()
    ax_pres.legend(lines1 + lines2, labels1 + labels2, fontsize=7, loc="upper left")

    # ── bottom: per-election zoom strips ──────────────────────────────────────
    ax_zoom = fig.add_subplot(gs[2])
    WINDOW = 20  # seconds before and after each election

    if not etimes or not has_latency:
        msg = ("No elections detected — nothing to zoom into."
               if not etimes else
               "Latency data unavailable for zoom.")
        ax_zoom.text(0.5, 0.5, msg, ha="center", va="center",
                     transform=ax_zoom.transAxes, fontsize=9, color="grey")
        ax_zoom.set_title("Per-election latency zoom (±20 s)", fontsize=9)
    else:
        # Stack each election window horizontally with a divider gap
        offset = 0
        gap    = 5
        tick_positions, tick_labels = [], []

        for ei, et in enumerate(etimes):
            win = df[(df["t"] >= et - WINDOW) & (df["t"] <= et + WINDOW)].copy()
            if win.empty:
                continue
            local_t = win["t"] - et + offset  # centre on election at x=offset

            wal_s  = win["wal_fsync_avg_ms"].rolling(3, min_periods=1).mean()
            bck_s  = win["backend_commit_avg_ms"].rolling(3, min_periods=1).mean()
            ax_zoom.fill_between(local_t, wal_s, alpha=0.2, color=C_WAL)
            ax_zoom.plot(local_t, wal_s, color=C_WAL, linewidth=1.2,
                         label="WAL fsync" if ei == 0 else None)
            ax_zoom.fill_between(local_t, bck_s, alpha=0.15, color=C_BACKEND)
            ax_zoom.plot(local_t, bck_s, color=C_BACKEND, linewidth=1.2,
                         label="backend commit" if ei == 0 else None)
            ax_zoom.axvline(offset, color=C_ELECTION, linewidth=1.5,
                            linestyle="--", alpha=0.9,
                            label="election" if ei == 0 else None)

            tick_positions.append(offset)
            tick_labels.append(f"E{ei + 1}\nt={et:.0f}s")

            offset += 2 * WINDOW + gap

        ax_zoom.set_xticks(tick_positions)
        ax_zoom.set_xticklabels(tick_labels, fontsize=7)
        ax_zoom.set_ylabel("Latency (ms)", fontsize=8)
        ax_zoom.set_title(
            "Per-election zoom (±20 s) — each strip is centred on one election event",
            fontsize=9,
        )
        ax_zoom.legend(fontsize=7, loc="upper right")

    return fig


# ── main ──────────────────────────────────────────────────────────────────────

def main(results_dir: str):
    root = Path(results_dir)
    if not root.exists():
        print(f"ERROR: {results_dir} does not exist", file=sys.stderr)
        sys.exit(1)

    # Discover scenario directories that have a timeseries.csv.
    # Support two layouts:
    #   a) results/<timestamp>/          → iterate subdirs (normal case)
    #   b) results/<timestamp>/<scenario>/ → root itself has timeseries.csv
    if (root / "timeseries.csv").exists():
        candidates = [root]
    else:
        candidates = sorted(d for d in root.iterdir() if d.is_dir())

    scenarios_data = []
    for d in candidates:
        df, analysis = load_scenario(d)
        if df is None:
            print(f"  skip {d.name}/ (no timeseries.csv)")
            continue
        print(f"  loaded {d.name}/ ({len(df)} rows)")
        epoch = float(df["_epoch"].iloc[0]) if "_epoch" in df.columns else 0
        ops_df = load_ops_stats(d, epoch=epoch)
        scenarios_data.append({"name": d.name, "dir": d, "df": df, "analysis": analysis, "ops_df": ops_df})

    if not scenarios_data:
        print("No scenario data found (no timeseries.csv files).", file=sys.stderr)
        sys.exit(1)

    # Sort by canonical order, unknown scenarios go at the end
    order = {n: i for i, n in enumerate(SCENARIO_ORDER)}
    scenarios_data.sort(key=lambda s: order.get(s["name"], 99))

    out_pdf = root / "report.pdf"
    png_dir = root / "plots"
    png_dir.mkdir(exist_ok=True)

    with PdfPages(out_pdf) as pdf:

        # 1. Cross-scenario summary
        plot_summary(scenarios_data, pdf)

        # 2. Pressure → election correlation (all scenarios combined)
        plot_pressure_election_correlation(scenarios_data, pdf)

        # 3. Per-scenario pages
        for s in scenarios_data:
            df       = s["df"]
            analysis = s["analysis"]
            name     = s["name"]
            ops_df   = s.get("ops_df", pd.DataFrame())
            sdir     = s.get("dir", root / name)

            # ── causal chain page ─────────────────────────────────────────
            fig = plt.figure(figsize=(12, 8))
            fig.suptitle(
                f"{name}  |  elections={analysis.get('elections','?')}  "
                f"hb_failures={int(df['hb_send_failures'].iloc[-1] - df['hb_send_failures'].iloc[0])}  "
                f"success={analysis.get('success_rate', 0.0):.3f}",
                fontsize=10,
            )
            gs = gridspec.GridSpec(3, 1, figure=fig, hspace=0.08)
            ax_top = fig.add_subplot(gs[0])
            ax_mid = fig.add_subplot(gs[1], sharex=ax_top)
            ax_bot = fig.add_subplot(gs[2], sharex=ax_top)

            plot_causal_chain(df, analysis, name, ax_top, ax_mid, ax_bot)
            fig.tight_layout(rect=[0, 0, 1, 0.96])
            pdf.savefig(fig)
            fig.savefig(png_dir / f"{name}_causal_chain.png", dpi=150)
            plt.close(fig)

            # ── supporting metrics page ───────────────────────────────────
            fig2, axes2 = plt.subplots(3, 1, figsize=(12, 9), sharex=True)
            plot_supporting(df, analysis, name, axes2)
            fig2.tight_layout()
            pdf.savefig(fig2)
            fig2.savefig(png_dir / f"{name}_supporting.png", dpi=150)
            plt.close(fig2)

            # ── throughput degradation page ───────────────────────────────
            fig3 = plot_throughput_degradation(df, ops_df, analysis, name)
            pdf.savefig(fig3)
            fig3.savefig(png_dir / f"{name}_throughput.png", dpi=150)
            plt.close(fig3)

            # ── latency vs elections page ─────────────────────────────────
            fig4 = plot_latency_vs_elections(df, name)
            pdf.savefig(fig4)
            fig4.savefig(png_dir / f"{name}_latency_elections.png", dpi=150)
            plt.close(fig4)

            # ── tc queue saturation page (optional — needs tc_stats.csv) ──
            fig5 = plot_tc_queue(sdir, df, name)
            if fig5 is not None:
                pdf.savefig(fig5)
                fig5.savefig(png_dir / f"{name}_tc_queue.png", dpi=150)
                plt.close(fig5)

        # PDF metadata
        d = pdf.infodict()
        d["Title"]   = f"etcd Raft observability report – {root.name}"
        d["Subject"] = "Leader election / heartbeat pressure analysis"

    print(f"\nReport written to:  {out_pdf}")
    print(f"Individual PNGs in: {png_dir}/")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1])
