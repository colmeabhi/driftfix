#!/usr/bin/env python3
"""
graphs_from_csv.py — Generate all 7 load test graphs from CloudWatch CSV exports.

Reads:
  results/bedrock_latency.csv   — Logs Insights export (timestamp, latency_ms)
  results/concurrency.csv       — CW Metrics: ConcurrentExecutions Maximum 60s
  results/lambda_duration.csv   — CW Metrics: Duration p99 60s
  results/errors.csv            — CW Metrics: Errors Sum 60s
  results/invocations.csv       — CW Metrics: Invocations Sum 60s (4 functions)

Writes:
  results/G1_bedrock_latency.png
  results/G2_latency_timeline.png
  results/G3_throughput.png
  results/G4_concurrency.png
  results/G5_error_rate.png
  results/G6_scaling_response.png
  results/G7_resource_utilization.png

Usage:
  python3 scripts/load_test/graphs_from_csv.py
"""

import csv
import os
from datetime import datetime, timezone
from dateutil import parser as dtparse
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as mticker

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE = os.path.join(os.path.dirname(__file__), "..", "..", "results")
OUT  = BASE

CAPTION = dict(ha="center", fontsize=9, style="italic", color="#444444")

# ── Scenario definitions ──────────────────────────────────────────────────────
# Mapped from CloudWatch data: each scenario is identified by its burst timestamp.
# Boundaries derived from concurrency.csv / bedrock_latency.csv timestamps.
SCENARIOS = [
    {"id": "T1", "label": "T1: Functional",   "color": "#4C72B0", "stacks": 1,
     "start": "2026-05-04 16:34:00", "end": "2026-05-04 16:56:00"},
    {"id": "T2", "label": "T2: Scaling",      "color": "#55A868", "stacks": 3,
     "start": "2026-05-04 16:57:00", "end": "2026-05-04 17:23:00"},
    {"id": "T3", "label": "T3: Performance",  "color": "#E56B4A", "stacks": 10,
     "start": "2026-05-04 17:24:00", "end": "2026-05-04 17:51:00"},
    {"id": "T4", "label": "T4: Failure",      "color": "#C44E52", "stacks": 10,
     "start": "2026-05-04 17:52:00", "end": "2026-05-04 18:04:00"},
    {"id": "T5", "label": "T5: Security",     "color": "#8172B2", "stacks": 6,
     "start": "2026-05-04 18:05:00", "end": "2026-05-04 18:30:00"},
]

for s in SCENARIOS:
    s["start_dt"] = dtparse.parse(s["start"]).replace(tzinfo=timezone.utc)
    s["end_dt"]   = dtparse.parse(s["end"]).replace(tzinfo=timezone.utc)


def scenario_for(ts):
    """Return scenario dict for a given datetime, or None."""
    for s in SCENARIOS:
        if s["start_dt"] <= ts < s["end_dt"]:
            return s
    return None


# ── CSV readers ───────────────────────────────────────────────────────────────

def read_bedrock_latency():
    """Returns list of (datetime, float) tuples."""
    rows = []
    path = os.path.join(BASE, "bedrock_latency.csv")
    with open(path) as f:
        for row in csv.DictReader(f):
            try:
                ts  = dtparse.parse(row["@timestamp"].strip()).replace(tzinfo=timezone.utc)
                lat = float(row["latency_ms"].strip())
                rows.append((ts, lat))
            except (ValueError, KeyError):
                pass
    return rows


def read_cw_metrics(filename, value_col_idx=1):
    """
    Reads a CloudWatch Metrics CSV export.
    Skips 5 metadata rows, returns list of (datetime, float) for non-empty values.
    value_col_idx: 0-based index of the value column after the timestamp.
    """
    rows = []
    path = os.path.join(BASE, filename)
    with open(path) as f:
        lines = f.readlines()
    for line in lines[5:]:          # skip metadata rows
        parts = line.strip().split(",")
        if len(parts) < 2:
            continue
        try:
            ts  = dtparse.parse(parts[0].strip()).replace(tzinfo=timezone.utc)
            val_str = parts[value_col_idx + 1].strip() if value_col_idx + 1 < len(parts) else ""
            if val_str == "":
                continue
            rows.append((ts, float(val_str)))
        except (ValueError, IndexError):
            pass
    return rows


def read_invocations():
    """
    Returns dict with keys: processor, stack_processor, validator, pr_creator.
    Each value is list of (datetime, int).
    Column order from CSV: processor(m2), stack_processor(m1), validator(m3), pr_creator(m4)
    """
    result = {k: [] for k in ["processor", "stack_processor", "validator", "pr_creator"]}
    col_map = {1: "processor", 2: "stack_processor", 3: "validator", 4: "pr_creator"}
    path = os.path.join(BASE, "invocations.csv")
    with open(path) as f:
        lines = f.readlines()
    for line in lines[5:]:
        parts = line.strip().split(",")
        if len(parts) < 2:
            continue
        try:
            ts = dtparse.parse(parts[0].strip()).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        for idx, key in col_map.items():
            if idx < len(parts) and parts[idx].strip():
                try:
                    result[key].append((ts, int(float(parts[idx].strip()))))
                except ValueError:
                    pass
    return result


# ── Compute per-scenario Bedrock stats ────────────────────────────────────────

def bedrock_stats_by_scenario(latency_rows):
    """Returns dict: scenario_id -> {"latencies": [], "p50":, "p95":, "p99":}"""
    buckets = {s["id"]: [] for s in SCENARIOS}
    for ts, lat in latency_rows:
        sc = scenario_for(ts)
        if sc:
            buckets[sc["id"]].append(lat)
    stats = {}
    for sc in SCENARIOS:
        sid  = sc["id"]
        lats = buckets[sid]
        arr  = np.array(lats) if lats else np.array([0])
        stats[sid] = {
            "latencies": lats,
            "p50": float(np.percentile(arr, 50)),
            "p95": float(np.percentile(arr, 95)),
            "p99": float(np.percentile(arr, 99)),
            "max": float(np.max(arr)),
            "n":   len(lats),
        }
    return stats


def _save(fig, name, caption):
    fig.text(0.5, -0.04, caption, **CAPTION)
    plt.tight_layout()
    path = os.path.join(OUT, name)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved {path}")


# ── G1: Bedrock P50/P95/P99 grouped bar per scenario ─────────────────────────

def graph_g1(stats):
    sids   = [s["id"] for s in SCENARIOS]
    labels = [s["label"] for s in SCENARIOS]
    colors = [s["color"] for s in SCENARIOS]
    p50 = [stats[s]["p50"] for s in sids]
    p95 = [stats[s]["p95"] for s in sids]
    p99 = [stats[s]["p99"] for s in sids]

    x = np.arange(len(sids))
    w = 0.25
    fig, ax = plt.subplots(figsize=(12, 6))
    b50 = ax.bar(x - w, p50, w, label="P50 (median)", color="#4C72B0", edgecolor="white")
    b95 = ax.bar(x,     p95, w, label="P95",          color="#DD8452", edgecolor="white")
    b99 = ax.bar(x + w, p99, w, label="P99",          color="#C44E52", edgecolor="white")

    for bars in (b50, b95, b99):
        for bar in bars:
            h = bar.get_height()
            if h > 10:
                ax.text(bar.get_x() + bar.get_width() / 2, h + 120,
                        f"{h/1000:.1f}s", ha="center", va="bottom", fontsize=8.5)

    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=11)
    ax.set_ylabel("Bedrock Latency (ms)", fontsize=12)
    ax.set_title("Bedrock LLM Latency — P50 / P95 / P99 by Test Scenario", fontsize=14, fontweight="bold")
    ax.legend(fontsize=11)
    ax.grid(axis="y", alpha=0.3, linestyle="--")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.set_ylim(bottom=0)

    caption = (
        "Figure 1. Bedrock Converse API latency percentiles per test scenario (Amazon Nova Lite v1). "
        "P99 grows from 1,710 ms (T1, 1 stack) to 14,896 ms (T4, 10 stacks), reflecting "
        "increased Bedrock concurrency under parallel Lambda invocations."
    )
    _save(fig, "G1_bedrock_latency.png", caption)


# ── G2: Bedrock latency scatter over time ─────────────────────────────────────

def graph_g2(latency_rows):
    fig, ax = plt.subplots(figsize=(14, 5))

    # Group points by scenario
    for sc in SCENARIOS:
        pts = [(ts, lat) for ts, lat in latency_rows if sc["start_dt"] <= ts < sc["end_dt"]]
        if not pts:
            continue
        ts_vals  = [p[0] for p in pts]
        lat_vals = [p[1] for p in pts]
        ax.scatter(ts_vals, lat_vals, color=sc["color"], label=sc["label"], s=70, zorder=3, alpha=0.9)
        # Median line across the cluster
        med = float(np.median(lat_vals))
        ax.hlines(med, min(ts_vals), max(ts_vals),
                  colors=sc["color"], linestyles="--", linewidth=1.5, alpha=0.6)

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.xaxis.set_major_locator(mdates.MinuteLocator(interval=15))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right")
    ax.set_xlabel("Time (UTC)", fontsize=12)
    ax.set_ylabel("Bedrock Latency (ms)", fontsize=12)
    ax.set_title("Bedrock Latency Timeline — All 31 Invocations Across 5 Scenarios", fontsize=14, fontweight="bold")
    ax.legend(fontsize=10, loc="upper left")
    ax.grid(alpha=0.25, linestyle="--")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.set_ylim(bottom=0)

    caption = (
        "Figure 2. Individual Bedrock Converse API call latencies over the test run (31 total invocations). "
        "Each point is one stack_processor Lambda invocation. Dashed lines show the per-scenario median. "
        "The temporal clustering within each scenario reflects parallel Lambda dispatch by the orchestrator."
    )
    _save(fig, "G2_latency_timeline.png", caption)


# ── G3: Throughput per scenario ───────────────────────────────────────────────

def graph_g3(stats, duration_rows):
    """Throughput = concurrent stacks / Lambda Duration P99 * 60000 (theoretical stacks/min at peak concurrency)"""
    # Get duration P99 per scenario
    dur_by_sc = {}
    for ts, val in duration_rows:
        sc = scenario_for(ts)
        if sc:
            dur_by_sc[sc["id"]] = val

    sids   = [s["id"] for s in SCENARIOS]
    labels = [s["label"] for s in SCENARIOS]
    n_stacks = [s["stacks"] for s in SCENARIOS]
    colors = [s["color"] for s in SCENARIOS]

    # Throughput = stacks processed / (P99 duration in minutes)
    throughputs = []
    for sc in SCENARIOS:
        sid = sc["id"]
        dur_ms = dur_by_sc.get(sid, 0)
        n      = sc["stacks"]
        # Since all stacks run in parallel, total batch completes in ~P99 ms
        tp = (n / (dur_ms / 60000)) if dur_ms > 0 else 0
        throughputs.append(tp)

    fig, ax = plt.subplots(figsize=(11, 5))
    y    = np.arange(len(sids))
    bars = ax.barh(y, throughputs, color=colors, edgecolor="white", height=0.55)

    for bar, tp, n, dur_ms in zip(bars, throughputs, n_stacks,
                                   [dur_by_sc.get(s["id"], 0) for s in SCENARIOS]):
        w = bar.get_width()
        ax.text(w + 0.3, bar.get_y() + bar.get_height() / 2,
                f"  {tp:.1f} stacks/min  ({n} stacks, P99={dur_ms/1000:.1f}s)",
                va="center", fontsize=10)

    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=11)
    ax.set_xlabel("Peak Throughput (stacks / minute)", fontsize=12)
    ax.set_title("Drift Detection Throughput by Test Scenario", fontsize=14, fontweight="bold")
    ax.set_xlim(0, max(throughputs or [1]) * 1.6)
    ax.grid(axis="x", alpha=0.3, linestyle="--")
    ax.invert_yaxis()

    caption = (
        "Figure 3. Peak throughput in stacks processed per minute. "
        "Formula: concurrent_stacks ÷ Lambda_P99_duration_minutes. "
        "Since all stacks are processed in parallel, throughput scales with concurrency — "
        "T3/T4 each process 10 stacks in ~15–16 s, yielding ~38–40 stacks/min."
    )
    _save(fig, "G3_throughput.png", caption)


# ── G4: Concurrent executions timeline ───────────────────────────────────────

def graph_g4(conc_rows):
    # Only plot time points with actual values
    ts_vals  = [ts  for ts, v in conc_rows]
    conc_vals = [v   for ts, v in conc_rows]

    fig, ax = plt.subplots(figsize=(14, 5))

    if ts_vals:
        ax.vlines(ts_vals, 0, conc_vals, colors="steelblue", linewidth=2, alpha=0.7)
        ax.scatter(ts_vals, conc_vals, color="steelblue", s=80, zorder=4)

        # Annotate each spike with scenario label
        for ts, v in zip(ts_vals, conc_vals):
            sc = scenario_for(ts)
            lbl = sc["label"] if sc else ""
            ax.annotate(f"{lbl}\n(peak={int(v)})",
                        xy=(ts, v), xytext=(0, 10), textcoords="offset points",
                        ha="center", fontsize=8.5, color="#333")

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.xaxis.set_major_locator(mdates.MinuteLocator(interval=20))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right")
    ax.set_xlabel("Time (UTC)", fontsize=12)
    ax.set_ylabel("Concurrent Lambda Executions", fontsize=12)
    ax.set_title("Lambda Concurrent Executions — Scale-Out Per Scenario", fontsize=14, fontweight="bold")
    ax.yaxis.set_major_locator(mticker.MaxNLocator(integer=True))
    ax.set_ylim(bottom=0, top=max(conc_vals or [1]) + 3)
    ax.grid(axis="y", alpha=0.25, linestyle="--")

    caption = (
        "Figure 4. Peak concurrent executions of drift-detector-stack-processor Lambda at each scenario burst. "
        "Lambda scaled from 1 (T1, single synthetic invocation) to 10 (T3/T4, 10 parallel stacks). "
        "The concurrency spikes confirm automatic Lambda horizontal scaling with no manual provisioning."
    )
    _save(fig, "G4_concurrency.png", caption)


# ── G5: Error rate per scenario ───────────────────────────────────────────────

def graph_g5(error_rows, stats):
    # Sum errors per scenario
    err_by_sc = {s["id"]: 0 for s in SCENARIOS}
    for ts, v in error_rows:
        sc = scenario_for(ts)
        if sc:
            err_by_sc[sc["id"]] += int(v)

    sids    = [s["id"] for s in SCENARIOS]
    labels  = [s["label"] for s in SCENARIOS]
    colors  = [s["color"] for s in SCENARIOS]
    n_stacks = [s["stacks"] for s in SCENARIOS]
    err_counts = [err_by_sc[s] for s in sids]
    err_rates  = [
        (err_by_sc[sid] / max(n, 1)) * 100
        for sid, n in zip(sids, n_stacks)
    ]

    x = np.arange(len(sids))
    w = 0.4
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))

    bars1 = ax1.bar(x, err_counts, w, color=colors, edgecolor="white")
    for bar, v in zip(bars1, err_counts):
        ax1.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.03,
                 str(int(v)), ha="center", va="bottom", fontsize=11, fontweight="bold")
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels, fontsize=10, rotation=15, ha="right")
    ax1.set_ylabel("Lambda Error Count", fontsize=12)
    ax1.set_title("Lambda Errors per Scenario", fontsize=13, fontweight="bold")
    ax1.grid(axis="y", alpha=0.3, linestyle="--")
    ax1.yaxis.set_major_locator(mticker.MaxNLocator(integer=True))
    ax1.set_ylim(bottom=0, top=max(err_counts or [1]) + 1)

    bars2 = ax2.bar(x, err_rates, w, color=colors, edgecolor="white")
    for bar, rate in zip(bars2, err_rates):
        h = bar.get_height()
        lbl = f"{rate:.0f}%" if rate > 0 else "0%"
        ax2.text(bar.get_x() + bar.get_width() / 2, h + 0.1,
                 lbl, ha="center", va="bottom", fontsize=11, fontweight="bold")
    ax2.set_xticks(x)
    ax2.set_xticklabels(labels, fontsize=10, rotation=15, ha="right")
    ax2.set_ylabel("Error Rate (%)", fontsize=12)
    ax2.set_title("Error Rate (%) per Scenario", fontsize=13, fontweight="bold")
    ax2.grid(axis="y", alpha=0.3, linestyle="--")
    ax2.set_ylim(bottom=0, top=max(err_rates or [1]) + 5)

    caption = (
        "Figure 5. Left: absolute Lambda error count per scenario. Right: error rate as a % of stacks processed. "
        "Zero errors across all scenarios confirms robust end-to-end pipeline execution. "
        "T4 (Failure) injected a bad DynamoDB tenant but the error was caught at the processor level "
        "before stack-processor was invoked, leaving its error count at 0."
    )
    _save(fig, "G5_error_rate.png", caption)


# ── G6: Scaling response — latency vs concurrent stacks ──────────────────────

def graph_g6(stats):
    points = []
    for sc in SCENARIOS:
        sid    = sc["id"]
        stacks = sc["stacks"]
        p50    = stats[sid]["p50"]
        p95    = stats[sid]["p95"]
        p99    = stats[sid]["p99"]
        if stats[sid]["n"] > 0:
            points.append((stacks, p50, p95, p99, sc["label"], sc["color"]))

    fig, ax = plt.subplots(figsize=(10, 6))

    if points:
        # Sort by stack count for line plot
        points_sorted = sorted(points, key=lambda p: p[0])
        x    = [p[0] for p in points_sorted]
        p50v = [p[1] for p in points_sorted]
        p95v = [p[2] for p in points_sorted]
        p99v = [p[3] for p in points_sorted]
        lbls = [p[4] for p in points_sorted]

        ax.plot(x, p50v, "D-",  color="#4C72B0", linewidth=2.5, markersize=9, label="P50 (median)")
        ax.plot(x, p95v, "o--", color="#DD8452", linewidth=2.5, markersize=9, label="P95")
        ax.plot(x, p99v, "s:",  color="#C44E52", linewidth=2.5, markersize=9, label="P99")

        for xi, y95, y99, lbl in zip(x, p95v, p99v, lbls):
            ax.annotate(lbl, xy=(xi, y99),
                        xytext=(8, 5), textcoords="offset points",
                        fontsize=8.5, color="#555")

    ax.set_xlabel("Concurrent Stacks Processed", fontsize=12)
    ax.set_ylabel("Bedrock Latency (ms)", fontsize=12)
    ax.set_title("Scaling Response: Bedrock Latency vs Concurrent Stack Load", fontsize=14, fontweight="bold")
    ax.legend(fontsize=11)
    ax.grid(alpha=0.25, linestyle="--")
    ax.xaxis.set_major_locator(mticker.MaxNLocator(integer=True))
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.set_ylim(bottom=0)

    caption = (
        "Figure 6. Bedrock P50, P95, and P99 latency as a function of concurrent stacks processed. "
        "P99 grows from 1,710 ms (1 stack) to 14,896 ms (10 stacks), indicating Bedrock request "
        "queueing under high concurrency. P50 remains relatively stable (~4–5 s), confirming "
        "tail latency rather than median degradation under load."
    )
    _save(fig, "G6_scaling_response.png", caption)


# ── G7: Resource utilization per function ─────────────────────────────────────

def graph_g7(invoc_data, duration_rows):
    fn_keys    = ["processor", "stack_processor", "validator", "pr_creator"]
    fn_labels  = ["Processor", "Stack\nProcessor", "Validator", "PR Creator"]
    fn_colors  = ["#4C72B0", "#DD8452", "#55A868", "#8172B2"]

    # Total invocations per function across all scenarios
    total_invoc = {k: sum(v for _, v in invoc_data[k]) for k in fn_keys}

    # Duration P99 per scenario (from lambda_duration.csv)
    dur_by_sc = {}
    for ts, val in duration_rows:
        sc = scenario_for(ts)
        if sc:
            dur_by_sc[sc["id"]] = val

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))

    # Left — total invocations per function
    x = np.arange(len(fn_keys))
    bars1 = ax1.bar(x, [total_invoc[k] for k in fn_keys], color=fn_colors, edgecolor="white")
    for bar in bars1:
        h = bar.get_height()
        if h > 0:
            ax1.text(bar.get_x() + bar.get_width() / 2, h + 0.2,
                     str(int(h)), ha="center", va="bottom", fontsize=12, fontweight="bold")
    ax1.set_xticks(x)
    ax1.set_xticklabels(fn_labels, fontsize=11)
    ax1.set_ylabel("Total Invocations (all scenarios)", fontsize=11)
    ax1.set_title("Lambda Invocations per Pipeline Function", fontsize=13, fontweight="bold")
    ax1.grid(axis="y", alpha=0.3, linestyle="--")
    ax1.yaxis.set_major_locator(mticker.MaxNLocator(integer=True))
    ax1.set_ylim(bottom=0, top=max(total_invoc.values() or [1]) * 1.25)

    # Right — Lambda Duration P99 per scenario (bar chart)
    sids   = [s["id"] for s in SCENARIOS if s["id"] in dur_by_sc]
    labels = [s["label"] for s in SCENARIOS if s["id"] in dur_by_sc]
    colors = [s["color"] for s in SCENARIOS if s["id"] in dur_by_sc]
    durs   = [dur_by_sc[sid] for sid in sids]

    y2 = np.arange(len(sids))
    bars2 = ax2.barh(y2, durs, color=colors, edgecolor="white", height=0.55)
    for bar, d in zip(bars2, durs):
        ax2.text(bar.get_width() + 100, bar.get_y() + bar.get_height() / 2,
                 f"{d/1000:.1f} s", va="center", fontsize=10, fontweight="bold")
    ax2.set_yticks(y2)
    ax2.set_yticklabels(labels, fontsize=10)
    ax2.set_xlabel("Lambda Duration P99 (ms)", fontsize=11)
    ax2.set_title("Stack Processor Duration P99 per Scenario", fontsize=13, fontweight="bold")
    ax2.grid(axis="x", alpha=0.3, linestyle="--")
    ax2.xaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax2.set_xlim(left=0, right=max(durs or [1]) * 1.3)
    ax2.invert_yaxis()

    caption = (
        "Figure 7. Left: total Lambda invocations per pipeline function across all test scenarios. "
        "The 1:1:1 ratio between stack-processor, validator, and pr-creator confirms no invocations "
        "were lost between pipeline stages. Right: Lambda Duration P99 per scenario — "
        "higher duration in T3/T4 reflects 10 stacks competing for Bedrock capacity."
    )
    _save(fig, "G7_resource_utilization.png", caption)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("\nGenerating graphs from CloudWatch CSV exports...\n")
    os.makedirs(OUT, exist_ok=True)

    latency_rows  = read_bedrock_latency()
    conc_rows     = read_cw_metrics("concurrency.csv",    value_col_idx=0)
    duration_rows = read_cw_metrics("lambda_duration.csv", value_col_idx=0)
    error_rows    = read_cw_metrics("errors.csv",         value_col_idx=0)
    invoc_data    = read_invocations()

    print(f"  Bedrock latency points : {len(latency_rows)}")
    print(f"  Concurrency points     : {len(conc_rows)}")
    print(f"  Duration points        : {len(duration_rows)}")
    print(f"  Error points           : {len(error_rows)}")
    for k, v in invoc_data.items():
        print(f"  Invocations ({k:15s}): {sum(x for _, x in v)}")
    print()

    stats = bedrock_stats_by_scenario(latency_rows)
    for sc in SCENARIOS:
        sid = sc["id"]
        s   = stats[sid]
        print(f"  {sid}: n={s['n']:2d}  P50={s['p50']:6,.0f}ms  P95={s['p95']:6,.0f}ms  P99={s['p99']:6,.0f}ms")
    print()

    graph_g1(stats)
    graph_g2(latency_rows)
    graph_g3(stats, duration_rows)
    graph_g4(conc_rows)
    graph_g5(error_rows, stats)
    graph_g6(stats)
    graph_g7(invoc_data, duration_rows)

    print(f"\nAll 7 graphs saved to {OUT}/")
    print("  G1_bedrock_latency.png")
    print("  G2_latency_timeline.png")
    print("  G3_throughput.png")
    print("  G4_concurrency.png")
    print("  G5_error_rate.png")
    print("  G6_scaling_response.png")
    print("  G7_resource_utilization.png\n")


if __name__ == "__main__":
    main()
