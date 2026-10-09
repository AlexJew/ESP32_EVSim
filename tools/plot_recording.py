#!/usr/bin/env python3
"""Plot a sweep recorded by evsim_recorder: where the delay goes, and the power at each current setpoint.

    python3 tools/plot_recording.py [recording.sqlite] [--out DIR]

Needs matplotlib (not part of the server's requirements). Without a recording it takes the newest in data/recordings/.
Two figures are written to DIR (default data/figures/): <name>-waterfall.png and <name>-power.png.
"""
import argparse
import glob
import os
import sqlite3
import statistics
import sys

NS = 1e9
BASELINE_S = 5          # the level before a write is the median of this many seconds
SETTLED_S = 10          # the settled level, and the steady-state power, use the last seconds of a step
MIN_STEP_W = 300        # a hop is only measured when its signal moved by at least this much
DIP_W = 400             # inside a settled window, an EV setpoint this far from its first half ends the window; the inverter
                        # follows the setpoint, so its own oscillation when saturated (15-16 A) is kept as jitter
LARGE_JUMP_A = 3        # a change of at least this many amps is plotted apart from the 1 A steps
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def level(rows, start_ns, end_ns):
    """Median of the values in [start_ns, end_ns), or None when there are none."""
    values = [v for t, v in rows if start_ns <= t < end_ns and v is not None]
    return statistics.median(values) if values else None


def crossing_time(rows, start_ns, base, settled):
    """Time of the first row, from start_ns on, that is past half-way from `base` to `settled` and whose successor is too
    (one noisy sample is not a step). The last row needs no successor. None when the signal never gets there."""
    if base is None or settled is None or base == settled:
        return None
    half = (base + settled) / 2
    rising = settled > base
    beyond = [(t, (v > half) if rising else (v < half)) for t, v in rows if t >= start_ns and v is not None]
    for index, (t, past) in enumerate(beyond):
        if past and (index + 1 == len(beyond) or beyond[index + 1][1]):
            return t
    return None


def dip_start(rows, start_ns, end_ns):
    """Time of the first setpoint reading in [start_ns, end_ns) that is DIP_W or more away from the median of the window's first half,
    or end_ns when there is none. The steady-state figures of every series stop there, so a dip is not averaged into them."""
    window = [(t, v) for t, v in rows if start_ns <= t < end_ns and v is not None]
    reference = level(window, start_ns, start_ns + (end_ns - start_ns) // 2)
    if reference is None:
        return end_ns
    return next((t for t, v in window if abs(v - reference) >= DIP_W), end_ns)


def kind_of(from_a, to_a):
    return "large" if abs(to_a - from_a) >= LARGE_JUMP_A else "small"


def latest_db(folder):
    found = glob.glob(os.path.join(folder, "*.sqlite"))
    return max(found, key=os.path.getmtime) if found else None


def _series(db, sql):
    return [(t, v) for t, v in db.execute(sql) if t is not None]


def analyse(path):
    db = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        writes = db.execute("SELECT run, step, t_write_ns, from_a, to_a FROM limit_writes "
                            "WHERE run > 0 AND error IS NULL ORDER BY t_write_ns").fetchall()
        cp = _series(db, "SELECT t_ns, setpoint_w FROM cp")
        server = _series(db, "SELECT t_ns, setpoint_w FROM setpoint")
        inverter = _series(db, "SELECT read_ts_ms * 1000000, dc_w FROM inverter WHERE read_ts_ms IS NOT NULL")
        ac = _series(db, "SELECT read_ts_ms * 1000000, ac_w FROM inverter WHERE read_ts_ms IS NOT NULL")
        charger = _series(db, "SELECT t_start_ns, power_w FROM charger")
        last_ns = max([rows[-1][0] for rows in (cp, server, inverter) if rows] or [0])
    finally:
        db.close()
    hops, skipped, powers = [], [], []
    for index, (run, step, t0, from_a, to_a) in enumerate(writes):
        end = writes[index + 1][2] if index + 1 < len(writes) else last_ns + 1
        before, tail = t0 - int(BASELINE_S * NS), end - int(SETTLED_S * NS)
        stop = dip_start(server, tail, end)
        powers.append({"trimmed": stop < end, "run": run, "step": step, "from_a": from_a, "to_a": to_a,
                       "charger_w": level_mean(charger, tail, stop), "dc_w": level_mean(inverter, tail, stop),
                       "ac_w": level_mean(ac, tail, stop), "setpoint_w": level_mean(server, tail, stop), "charger_sd": level_sd(charger, tail, stop),
                       "dc_sd": level_sd(inverter, tail, stop), "ac_sd": level_sd(ac, tail, stop)})
        times, reason = [], None
        for name, rows in (("board", cp), ("server", server), ("inverter", inverter)):
            window = [(t, v) for t, v in rows if t < end]
            base, settled = level(window, before, t0), level(window, tail, end)
            if base is None or settled is None or abs(settled - base) < MIN_STEP_W:
                reason = "the %s did not move by %d W" % (name, MIN_STEP_W)
                break
            moment = crossing_time(window, t0, base, settled)
            if moment is None:
                reason = "the %s never crossed half of the step" % name
                break
            times.append(moment)
        if reason:
            skipped.append((step, reason))
            continue
        hops.append({"run": run, "step": step, "from_a": from_a, "to_a": to_a, "kind": kind_of(from_a, to_a),
                     "hop1": (times[0] - t0) / NS, "hop2": (times[1] - times[0]) / NS,
                     "hop3": (times[2] - times[1]) / NS, "total": (times[2] - t0) / NS})
    return {"db": path, "hops": hops, "skipped": skipped, "powers": powers}


def level_mean(rows, start_ns, end_ns):
    values = [v for t, v in rows if start_ns <= t < end_ns and v is not None]
    return statistics.fmean(values) if values else None


def level_sd(rows, start_ns, end_ns):
    values = [v for t, v in rows if start_ns <= t < end_ns and v is not None]
    return statistics.pstdev(values) if len(values) > 1 else 0.0


HOPS = (("hop1", "Car charger → board", "#c27b14"), ("hop2", "Board → server", "#176b4a"),
        ("hop3", "Server → inverter", "#2a5fa8"))


def _pyplot():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def plot_waterfall(result, out):
    plt = _pyplot()
    groups = [(title, [h for h in result["hops"] if h["kind"] == kind]) for kind, title in
              (("small", "1 A steps"), ("large", "Large jumps"))]
    groups = [(title, hops) for title, hops in groups if hops]
    fig, axes = plt.subplots(len(groups) or 1, 1, figsize=(9, 2.4 * (len(groups) or 1) + 1.0), squeeze=False)
    for ax, (title, hops) in zip(axes[:, 0], groups):
        left, reach = 0.0, 0.0
        names = []
        for row, (key, label, color) in enumerate(HOPS):
            values = [h[key] for h in hops]
            mean = statistics.fmean(values)
            sd = statistics.pstdev(values) if len(values) > 1 else 0.0
            ax.barh(row, mean, left=left, color=color, height=0.6, xerr=sd, error_kw={"capsize": 4})
            ax.text(left + mean + sd + 0.05, row, "%.2f s" % mean, va="center", fontsize=9)
            names.append(label)
            left += mean
            reach = max(reach, left + sd)
        totals = [h["total"] for h in hops]
        sd = statistics.pstdev(totals) if len(totals) > 1 else 0.0
        ax.barh(len(HOPS), left, color="#444444", height=0.6, xerr=sd, error_kw={"capsize": 4})
        ax.text(left + sd + 0.05, len(HOPS), "%.2f s" % left, va="center", fontsize=9, fontweight="bold")
        names.append("Total (n = %d)" % len(hops))
        ax.set_yticks(range(len(names)))
        ax.set_yticklabels(names)
        ax.invert_yaxis()
        ax.set_xlim(0, (reach + sd) * 1.12)
        ax.set_title(title + " (bars: mean, whiskers: standard deviation)", fontsize=10, loc="left")
        ax.grid(axis="x", color="#dce4df")
        ax.set_axisbelow(True)
    axes[-1, 0].set_xlabel("Time since the limit was written to the charger (s)")
    if not groups:
        axes[0, 0].text(0.5, 0.5, "No step was measurable", ha="center", transform=axes[0, 0].transAxes)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)


def plot_waterfall_vertical(result, out):
    """The 1 A steps as a thin vertical waterfall: time runs down, one floating bar per hop, then the total."""
    plt = _pyplot()
    hops = [h for h in result["hops"] if h["kind"] == "small"]
    fig, ax = plt.subplots(figsize=(3.4, 6.5))
    if not hops:
        ax.text(0.5, 0.5, "No 1 A step was measurable", ha="center", transform=ax.transAxes)
    else:
        bottom, reach, names = 0.0, 0.0, []
        for column, (key, label, color) in enumerate(HOPS):
            values = [h[key] for h in hops]
            mean = statistics.fmean(values)
            sd = statistics.pstdev(values) if len(values) > 1 else 0.0
            ax.bar(column, mean, bottom=bottom, color=color, width=0.6)
            ax.errorbar(column, bottom + mean, yerr=sd, color="black", capsize=4, linewidth=1)
            ax.text(column, bottom + mean + sd + 0.08, "%.2f s" % mean, ha="center", va="top", fontsize=9)
            names.append(label.replace(" → ", "\n→ "))
            bottom += mean
            reach = max(reach, bottom + sd)
        totals = [h["total"] for h in hops]
        sd = statistics.pstdev(totals) if len(totals) > 1 else 0.0
        ax.bar(len(HOPS), bottom, color="#444444", width=0.6)
        ax.errorbar(len(HOPS), bottom, yerr=sd, color="black", capsize=4, linewidth=1)
        ax.text(len(HOPS), bottom + sd + 0.08, "%.2f s" % bottom, ha="center", va="top", fontsize=9, fontweight="bold")
        names.append("Total\n(n = %d)" % len(hops))
        ax.set_xticks(range(len(names)))
        ax.set_xticklabels(names, fontsize=8)
        ax.set_ylim(0, (reach + sd) * 1.12)
        ax.invert_yaxis()
        ax.set_ylabel("Time since the limit was written (s)")
        ax.set_title("1 A steps", fontsize=10, loc="left")
        ax.grid(axis="y", color="#dce4df")
        ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(out, dpi=200)
    plt.close(fig)


def plot_power(result, out):
    plt = _pyplot()
    by_amp = {}
    for p in result["powers"]:
        by_amp.setdefault(p["to_a"], []).append(p)
    amps = sorted(by_amp)
    fig, ax = plt.subplots(figsize=(10, 5.5))
    right = ax.twinx()

    def mean_of(key, a):
        values = [p[key] for p in by_amp[a] if p[key] is not None]
        return statistics.fmean(values) if values else None

    ev = [(a, mean_of("setpoint_w", a)) for a in amps if mean_of("setpoint_w", a) is not None]
    if ev:
        ax.plot([a for a, _ in ev], [w for _, w in ev], label="EV setpoint", color="#176b4a", marker="s", linewidth=2)
    for key, sd, label, color in (("charger_w", "charger_sd", "Car charger", "#c27b14"), ("dc_w", "dc_sd", "Inverter DC input", "#2a5fa8"),
                                  ("ac_w", "ac_sd", "Inverter AC output", "#7b4fa3")):
        points = [(a, mean_of(key, a), mean_of(sd, a), mean_of("setpoint_w", a)) for a in amps if mean_of(key, a) is not None]
        if not points:
            continue
        ax.plot([a for a, *_ in points], [w for _, w, *_ in points], label=label, color=color, marker="o", linewidth=2)
        if key == "dc_w":
            errors = [(a, abs(w - target)) for a, w, _, target in points if target is not None]
            right.plot([a for a, _ in errors], [e for _, e in errors], color=color, linestyle="--", linewidth=1.5)
        if key == "ac_w":
            right.plot([a for a, *_ in points], [j for _, _, j, _ in points], color=color, linestyle=":", linewidth=1.8)
    ax.set_xlabel("Current limit written to the charger (A)")
    ax.set_ylabel("Power (W), mean of the last %d s of each step" % SETTLED_S)
    right.set_ylabel("DC error (dashed) and AC jitter (dotted), W")
    right.set_ylim(bottom=0)
    ax.set_xticks(amps)
    ax.set_ylim(bottom=0)
    ax.grid(color="#dce4df")
    handles, labels = ax.get_legend_handles_labels()
    from matplotlib.lines import Line2D
    handles += [Line2D([], [], color="#2a5fa8", linestyle="--"), Line2D([], [], color="#7b4fa3", linestyle=":")]
    labels += ["Error", "Jitter"]
    ax.legend(handles, labels, loc="upper left", fontsize=8)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("db", nargs="?")
    parser.add_argument("--out", default=os.path.join(ROOT, "data", "figures"))
    args = parser.parse_args(argv)
    path = args.db or latest_db(os.path.join(ROOT, "data", "recordings"))
    if not path or not os.path.exists(path):
        sys.exit("no recording found")
    result = analyse(path)
    os.makedirs(args.out, exist_ok=True)
    stem = os.path.join(args.out, os.path.splitext(os.path.basename(path))[0])
    plot_waterfall(result, stem + "-waterfall.png")
    plot_waterfall_vertical(result, stem + "-waterfall-vertical.png")
    plot_power(result, stem + "-power.png")
    for h in result["hops"]:
        print("%2d -> %2d A  %-5s  %.2f + %.2f + %.2f = %.2f s" % (h["from_a"], h["to_a"], h["kind"], h["hop1"], h["hop2"], h["hop3"], h["total"]))
    for p in result["powers"]:
        if p["trimmed"]:
            print("%d A: steady-state window cut at a dip of the EV setpoint" % p["to_a"])
    for step, reason in result["skipped"]:
        print("step %d skipped: %s" % (step, reason))
    print("wrote", stem + "-waterfall.png", stem + "-power.png")


if __name__ == "__main__":
    main()
