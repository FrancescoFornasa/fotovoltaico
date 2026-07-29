#!/usr/bin/env python3
"""
pv_trend.py -- Multi-day PV comparison to gauge optimizer effectiveness.

Companion to pv_day.py / pv_compare.py. It auto-discovers every day the Shelly
Pro EM has stored and lays out a 2x2 dashboard:

               whole day                 afternoon (from 14:00)
    top:   absolute production (kWh)   afternoon production (kWh)
    bot:   Performance Ratio (%)       afternoon Performance Ratio (%)

PR = production / (kWp * available sun), normalised by modelled plane-of-array
irradiance (Open-Meteo), so weather is divided out -- the fair way to judge the
per-panel optimizers. The afternoon column isolates the window where the shading
(and thus the optimizers) act, so it is the sharpest signal.

Days are coloured BEFORE / INSTALL DAY / AFTER the optimizer installation, the PR
panels carry before/after mean lines, and partial days (device started mid-day,
or today so far) are faded and left out of the averages.

Examples:
    python pv_trend.py
    python pv_trend.py --model italia_meteo_arpae_icon_2i -o trend.png
    python pv_trend.py --afternoon 15:00 --start-date 18-07-2026 --theme dark
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import os
import sys

# Reuse the whole data + model pipeline from the single-day script.
from pv_day import (
    DEFAULT_AZIMUTH,
    DEFAULT_CHANNEL,
    DEFAULT_INV_AC_W,
    DEFAULT_INV_EFF,
    DEFAULT_IP,
    DEFAULT_KWP,
    DEFAULT_LAT,
    DEFAULT_LON,
    DEFAULT_NMOT,
    DEFAULT_SYS_EFF,
    DEFAULT_TEMP_COEFF_PCT,
    DEFAULT_TILT,
    THEMES,
    build_expected,
    build_series,
    compass_to_solar_azimuth,
    fetch_day,
    fetch_irradiance,
    hm,
    parse_hour,
    resolve_timezone,
    rpc,
    summarize,
)

DEFAULT_OPTIMIZER_DATE = "23-07-2026"   # per-panel optimizers installed ~14:00
DEFAULT_AFTERNOON = "14:00"             # afternoon-window start
FULL_DAY_MIN = 1380                     # minutes present to count a day as "full"

# Category colours (before / install day / after), stepped per theme.
CAT_COLOR = {
    "pre":     {"light": "#2a78d6", "dark": "#3987e5"},   # blue
    "install": {"light": "#c98500", "dark": "#eda100"},   # amber
    "post":    {"light": "#0ca30c", "dark": "#0ca30c"},   # green (the fix)
}
CAT_LABEL = {"pre": "before optimizers", "install": "install day",
             "post": "after optimizers"}


# ------------------------------------------------------------------ helpers
def parse_date(text: str) -> dt.date:
    try:
        return dt.datetime.strptime(text, "%d-%m-%Y").date()
    except ValueError:
        sys.exit(f"Invalid date '{text}'. Use dd-mm-yyyy, e.g. 22-07-2026.")


def discover_range(ip, channel, tzinfo):
    """Earliest..latest local dates with stored data, from EM1Data.GetRecords
    (tiny stray blocks under an hour are ignored)."""
    rec = rpc(ip, "EM1Data.GetRecords", {"id": channel})
    blocks = [b for b in rec.get("data_blocks", [])
              if int(b.get("records", 0)) >= 60]
    if not blocks:
        sys.exit("Device reports no stored energy data for that channel.")
    start_ts = min(int(b["ts"]) for b in blocks)
    end_ts = max(int(b["ts"]) + int(b["records"]) * int(b.get("period", 60))
                 for b in blocks)
    first = dt.datetime.fromtimestamp(start_ts, tz=tzinfo).date()
    last = dt.datetime.fromtimestamp(end_ts, tz=tzinfo).date()
    return first, last


def classify(day, opt_date):
    if day < opt_date:
        return "pre"
    if day > opt_date:
        return "post"
    return "install"


def _energy_after(hours, watts, start_h):
    """kWh made from start_h onward (1 sample = 1 minute)."""
    return sum(w for h, w in zip(hours, watts) if h >= start_h) / 60.0 / 1000.0


def collect(args, tzinfo, tz_label, first, last, opt_date, az_solar, model_kw,
            aft_start):
    """Walk every day in [first, last]; return per-day metric dicts."""
    rows = []
    day = first
    print(f"  {'date':<12}{'status':<19}{'kWh':>6}{'aPM':>6}{'PR':>6}"
          f"{'PRpm':>6}{'sun':>7}")
    print("  " + "-" * 62)
    while day <= last:
        midnight = dt.datetime.combine(day, dt.time.min, tzinfo=tzinfo)
        nxt = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min,
                                  tzinfo=tzinfo)
        keys, recs = fetch_day(args.ip, args.channel,
                               int(midnight.timestamp()), int(nxt.timestamp()))
        if not recs:
            day += dt.timedelta(days=1)
            continue

        series = build_series(keys, recs, tzinfo, midnight, "production")
        summ = summarize(series, "production")
        kwh = summ["total_kwh"] if summ else 0.0
        n = summ["n_minutes"] if summ else 0
        kwh_pm = _energy_after(series["hours"], series["avg_w"], aft_start)

        pr = poa = pr_pm = float("nan")
        irr = fetch_irradiance(args.lat, args.lon, day, tz_label,
                               args.tilt, az_solar, model=args.model)
        if irr:
            hrs, wm2, _kind, temps = irr
            exp = build_expected(series, hrs, wm2, temps, actual_kwh=kwh,
                                 **model_kw)
            pr, poa = exp["pr"], exp["poa_insol"]
            aft_poa = _energy_after(series["hours"], exp["poa_wm2"], aft_start)
            if aft_poa > 0:
                pr_pm = kwh_pm / (args.kwp * aft_poa)

        cat = classify(day, opt_date)
        partial = n < FULL_DAY_MIN
        rows.append({"date": day, "cat": cat, "kwh": kwh, "kwh_pm": kwh_pm,
                     "pr": pr, "pr_pm": pr_pm, "n": n, "partial": partial})

        pct = lambda v: "n/a" if math.isnan(v) else f"{v * 100:.0f}%"
        sun = "n/a" if math.isnan(poa) else f"{poa:.2f}"
        print(f"  {day.strftime('%d-%m-%Y'):<12}{CAT_LABEL[cat]:<19}{kwh:>6.1f}"
              f"{kwh_pm:>6.1f}{pct(pr):>6}{pct(pr_pm):>6}{sun:>7}"
              f"{'  part' if partial else ''}")
        day += dt.timedelta(days=1)
    return rows


# ------------------------------------------------------------------ plot
def _style(ax, c):
    ax.grid(True, axis="y", color=c["grid"], linewidth=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(c["axis"])
    ax.tick_params(colors=c["muted"], labelsize=9)


def plot_trend(rows, *, theme, tz_label, channel, aft_start, stats,
               out_path, do_show):
    import matplotlib
    if not do_show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    c = THEMES[theme]
    n = len(rows)
    x = list(range(n))
    labels = [r["date"].strftime("%d-%m") for r in rows]
    label_bars = n <= 16
    col = lambda cat: CAT_COLOR[cat][theme]
    post_x = [xi for xi, r in zip(x, rows) if r["cat"] == "post"]
    divider = post_x[0] - 0.5 if post_x else None

    fig, ((axTL, axTR), (axBL, axBR)) = plt.subplots(2, 2, figsize=(15, 9.2),
                                                     dpi=110)
    fig.patch.set_facecolor(c["page"])
    for ax in (axTL, axTR, axBL, axBR):
        ax.set_facecolor(c["surface"])

    def bars(ax, key, scale, fmt):
        for xi, r in zip(x, rows):
            v = r[key]
            if math.isnan(v):
                continue
            ax.bar(xi, v * scale, width=0.72, color=col(r["cat"]),
                   alpha=0.45 if r["partial"] else 1.0,
                   edgecolor=c["surface"], linewidth=0.8, zorder=3)
            if label_bars:
                ax.text(xi, v * scale, fmt.format(v * scale), ha="center",
                        va="bottom", fontsize=7.5, color=c["ink2"])

    def mean_lines(ax, pre, post):
        for mean, cat in ((pre, "pre"), (post, "post")):
            if not math.isnan(mean):
                ax.axhline(mean * 100, color=col(cat), linestyle=(0, (5, 2)),
                           linewidth=1.5, zorder=2)

    bars(axTL, "kwh", 1.0, "{:.1f}")
    axTL.set_ylabel("kWh", color=c["ink2"], fontsize=10)
    axTL.set_title("Absolute daily production", color=c["ink"], fontsize=12,
                   fontweight="bold", loc="left", pad=8)

    bars(axTR, "kwh_pm", 1.0, "{:.1f}")
    axTR.set_ylabel("kWh", color=c["ink2"], fontsize=10)
    axTR.set_title(f"Afternoon production (from {hm(aft_start)})",
                   color=c["ink"], fontsize=12, fontweight="bold", loc="left",
                   pad=8)

    bars(axBL, "pr", 100.0, "{:.0f}")
    mean_lines(axBL, stats["pre"], stats["post"])
    axBL.set_ylabel("PR (%)", color=c["ink2"], fontsize=10)
    axBL.set_title("Performance ratio — whole day", color=c["ink"], fontsize=12,
                   fontweight="bold", loc="left", pad=8)

    bars(axBR, "pr_pm", 100.0, "{:.0f}")
    mean_lines(axBR, stats["pre_pm"], stats["post_pm"])
    axBR.set_ylabel("PR (%)", color=c["ink2"], fontsize=10)
    axBR.set_title(f"Performance ratio — afternoon (from {hm(aft_start)})",
                   color=c["ink"], fontsize=12, fontweight="bold", loc="left",
                   pad=8)

    for ax in (axTL, axTR, axBL, axBR):
        ax.set_xticks(x)
        if divider is not None:
            ax.axvline(divider, color=c["ink2"], linewidth=1.0,
                       linestyle=(0, (2, 2)), zorder=1)
        _style(ax, c)
    for ax in (axTL, axTR):
        ax.set_xticklabels([])
    for ax in (axBL, axBR):
        ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
        ax.set_xlabel(f"Day  ({tz_label})", color=c["ink2"], fontsize=10)

    cats = [k for k in ("pre", "install", "post")
            if any(r["cat"] == k for r in rows)]
    handles = [Patch(facecolor=CAT_COLOR[k][theme], label=CAT_LABEL[k])
               for k in cats]
    if any(r["partial"] for r in rows):
        handles.append(Patch(facecolor=c["muted"], alpha=0.45,
                             label="partial day"))
    leg = axTL.legend(handles=handles, loc="upper left", frameon=False,
                      fontsize=8.5)
    for t in leg.get_texts():
        t.set_color(c["ink2"])

    fig.suptitle(f"Optimizer effectiveness  —  Shelly Pro EM channel {channel}",
                 color=c["ink"], fontsize=15, fontweight="bold")

    def dv(a, b):
        return "n/a" if (math.isnan(a) or math.isnan(b)) else \
            f"{a * 100:.0f}%→{b * 100:.0f}% ({(b - a) * 100:+.0f})"
    verdict = (f"Whole-day PR {dv(stats['pre'], stats['post'])}          "
               f"Afternoon PR {dv(stats['pre_pm'], stats['post_pm'])}")
    fig.text(0.5, 0.945, verdict, ha="center", va="top", color=c["ink2"],
             fontsize=11)

    fig.tight_layout(rect=(0, 0, 1, 0.93))

    if out_path:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        fig.savefig(out_path, facecolor=fig.get_facecolor(), bbox_inches="tight",
                    dpi=200)
        print(f"Saved chart to {out_path}")
    if do_show:
        plt.show()
    plt.close(fig)


# ------------------------------------------------------------------ main
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Compare daily PV production across days to gauge optimizers.")
    p.add_argument("--ip", default=DEFAULT_IP,
                   help=f"Shelly device IP (default: {DEFAULT_IP})")
    p.add_argument("-c", "--channel", type=int, default=DEFAULT_CHANNEL,
                   help=f"EM channel id, 0 or 1 (default: {DEFAULT_CHANNEL} = PV)")
    p.add_argument("--tz", default=None,
                   help="IANA timezone override (default: read from device)")
    p.add_argument("--start-date", default=None,
                   help="First day, dd-mm-yyyy (default: earliest stored)")
    p.add_argument("--end-date", default=None,
                   help="Last day, dd-mm-yyyy (default: latest stored)")
    p.add_argument("--optimizer-date", default=DEFAULT_OPTIMIZER_DATE,
                   help="Install date, dd-mm-yyyy; days after it are highlighted "
                        f"(default: {DEFAULT_OPTIMIZER_DATE})")
    p.add_argument("--afternoon", default=DEFAULT_AFTERNOON,
                   help="Afternoon-window start for the afternoon panels, "
                        f"HH:MM or hour (default: {DEFAULT_AFTERNOON})")
    p.add_argument("--lat", type=float, default=DEFAULT_LAT,
                   help=f"Latitude for the weather lookup (default: {DEFAULT_LAT})")
    p.add_argument("--lon", type=float, default=DEFAULT_LON,
                   help=f"Longitude for the weather lookup (default: {DEFAULT_LON})")
    p.add_argument("--tilt", type=float, default=DEFAULT_TILT,
                   help=f"Panel tilt deg (default: {DEFAULT_TILT:g})")
    p.add_argument("--azimuth", type=float, default=DEFAULT_AZIMUTH,
                   help="Panel azimuth compass deg, 0=N 90=E 180=S 270=W "
                        f"(default: {DEFAULT_AZIMUTH:g})")
    p.add_argument("--kwp", type=float, default=DEFAULT_KWP,
                   help=f"Array STC nameplate, kWp (default: {DEFAULT_KWP:g})")
    p.add_argument("--temp-coeff", type=float, default=DEFAULT_TEMP_COEFF_PCT,
                   help="Pmax temp coefficient, percent per degC "
                        f"(default: {DEFAULT_TEMP_COEFF_PCT:g})")
    p.add_argument("--nmot", type=float, default=DEFAULT_NMOT,
                   help=f"Nominal module operating temp, degC (default: {DEFAULT_NMOT:g})")
    p.add_argument("--inverter-eff", type=float, default=DEFAULT_INV_EFF,
                   help=f"Inverter efficiency, 0-1 (default: {DEFAULT_INV_EFF:g})")
    p.add_argument("--system-eff", type=float, default=DEFAULT_SYS_EFF,
                   help=f"Wiring/soiling/mismatch factor (default: {DEFAULT_SYS_EFF:g})")
    p.add_argument("--inverter-ac", type=float, default=DEFAULT_INV_AC_W,
                   help=f"Inverter AC cap, W (default: {DEFAULT_INV_AC_W:g})")
    p.add_argument("--model", default=None,
                   help="Force an Open-Meteo model on the forecast endpoints, "
                        "e.g. italia_meteo_arpae_icon_2i (default: best-match)")
    p.add_argument("-o", "--output", default=None,
                   help="Save the chart to this PNG path")
    p.add_argument("--theme", default="light", choices=["light", "dark"],
                   help="Colour theme (default: light)")
    p.add_argument("--no-show", action="store_true",
                   help="Do not open an interactive window (just save)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    opt_date = parse_date(args.optimizer_date)
    try:
        aft_start = parse_hour(args.afternoon)
    except ValueError:
        sys.exit("Invalid --afternoon; use HH:MM or an hour (e.g. 14:00 or 14).")
    tzinfo, tz_label = resolve_timezone(args.ip, args.tz)

    first, last = discover_range(args.ip, args.channel, tzinfo)
    if args.start_date:
        first = parse_date(args.start_date)
    if args.end_date:
        last = parse_date(args.end_date)

    print(f"Scanning {first.strftime('%d-%m-%Y')} .. {last.strftime('%d-%m-%Y')} "
          f"(optimizers {opt_date.strftime('%d-%m-%Y')}, afternoon from "
          f"{hm(aft_start)})  channel {args.channel} @ {args.ip}\n")

    az_solar = compass_to_solar_azimuth(args.azimuth)
    model_kw = dict(kwp=args.kwp, temp_coeff=args.temp_coeff / 100.0,
                    nmot=args.nmot, inv_eff=args.inverter_eff,
                    sys_eff=args.system_eff, inv_ac_w=args.inverter_ac)
    rows = collect(args, tzinfo, tz_label, first, last, opt_date, az_solar,
                   model_kw, aft_start)
    if not rows:
        sys.exit("No days with stored production in that range.")

    full = [r for r in rows if not r["partial"]]

    def mean_of(key, cat):
        vals = [r[key] for r in full
                if r["cat"] == cat and not math.isnan(r[key])]
        return sum(vals) / len(vals) if vals else float("nan")

    stats = {"pre": mean_of("pr", "pre"), "post": mean_of("pr", "post"),
             "pre_pm": mean_of("pr_pm", "pre"), "post_pm": mean_of("pr_pm", "post")}

    def verdict(name, a, b):
        if math.isnan(a) or math.isnan(b):
            return f"  {name}: need full days both sides of the install"
        return (f"  {name}: {a * 100:.1f}% -> {b * 100:.1f}%  "
                f"(delta {(b - a) * 100:+.1f} pts)")

    print()
    print(verdict("Whole-day PR", stats["pre"], stats["post"]))
    print(verdict("Afternoon PR", stats["pre_pm"], stats["post_pm"]))

    plot_trend(rows, theme=args.theme, tz_label=tz_label, channel=args.channel,
               aft_start=aft_start, stats=stats, out_path=args.output,
               do_show=not args.no_show)


if __name__ == "__main__":
    main()
