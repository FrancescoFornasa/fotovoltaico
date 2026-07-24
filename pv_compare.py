#!/usr/bin/env python3
"""
pv_compare.py -- Compare photovoltaic production of two days from a Shelly Pro EM.

Companion to pv_day.py: it reuses the same data pipeline (RPC fetch, per-minute
series building, per-day summary) but overlays TWO days on one graph and prints a
statistical comparison between them.

The chart shows both 1-minute average power curves (day A vs day B), a headline
delta in kWh, and each day's total. The terminal prints a full comparison table
(totals, peaks, active windows, plus curve-similarity metrics: Pearson
correlation, mean absolute difference and RMSE over the overlapping minutes).

Examples:
    python pv_compare.py 22-07-2026 20-07-2026
    python pv_compare.py 22-07-2026 20-07-2026 --band --theme dark -o cmp.png
    python pv_compare.py 22-07-2026 20-07-2026 -q consumption -c 0
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import os
import sys

# Reuse everything data-related from the single-day script (same directory).
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
    PROD_THRESHOLD_W,
    THEMES,
    build_expected,
    build_series,
    compass_to_solar_azimuth,
    fetch_day,
    fetch_irradiance,
    hm,
    parse_hour,
    resolve_timezone,
    summarize,
)

# Two-series colours: categorical slots 2 (orange) & 1 (blue) from the dataviz
# palette -- a CVD-safe pair, stepped per theme.
PAIR = {
    "light": {"a": "#eb6834", "b": "#2a78d6"},
    "dark":  {"a": "#d95926", "b": "#3987e5"},
}

TITLE = {"production": "PV production comparison",
         "consumption": "Consumption comparison",
         "net": "Net power comparison"}


# ------------------------------------------------------------------ load
def parse_date(text: str) -> dt.date:
    try:
        return dt.datetime.strptime(text, "%d-%m-%Y").date()
    except ValueError:
        sys.exit(f"Invalid date '{text}'. Use dd-mm-yyyy, e.g. 22-07-2026.")


def load_day(ip, channel, day, tzinfo, quantity):
    """Fetch + build the per-minute series and summary for one day.
    Returns (series, summary, span_hours)."""
    midnight = dt.datetime.combine(day, dt.time.min, tzinfo=tzinfo)
    next_midnight = dt.datetime.combine(day + dt.timedelta(days=1),
                                        dt.time.min, tzinfo=tzinfo)
    start_ts = int(midnight.timestamp())
    end_ts = int(next_midnight.timestamp())
    span = (next_midnight - midnight).total_seconds() / 3600.0

    keys, rows = fetch_day(ip, channel, start_ts, end_ts)
    if not rows:
        sys.exit(f"No stored data for {day} (device off, or before the earliest "
                 f"record).")
    series = build_series(keys, rows, tzinfo, midnight, quantity)
    return series, summarize(series, quantity), span


# ------------------------------------------------------------------ stats
def _minute_map(series):
    """minute-of-day -> average power, so two days align by clock time."""
    return {int(round(h * 60)): w
            for h, w in zip(series["hours"], series["avg_w"])}


def _active_values(series):
    return [w for w in series["avg_w"] if abs(w) > PROD_THRESHOLD_W]


def _window_hours(summary):
    if summary and summary["first_h"] is not None:
        return summary["last_h"] - summary["first_h"]
    return 0.0


def compare_stats(series_a, series_b, sum_a, sum_b):
    """Curve-similarity metrics over the minutes present in BOTH days."""
    import numpy as np

    ma, mb = _minute_map(series_a), _minute_map(series_b)
    common = sorted(set(ma) & set(mb))
    a = np.array([ma[k] for k in common], dtype=float)
    b = np.array([mb[k] for k in common], dtype=float)

    if len(common) >= 2 and a.std() > 0 and b.std() > 0:
        r = float(np.corrcoef(a, b)[0, 1])
    else:
        r = float("nan")
    diff = a - b
    mae = float(np.mean(np.abs(diff))) if len(common) else float("nan")
    rmse = float(np.sqrt(np.mean(diff ** 2))) if len(common) else float("nan")

    act_a, act_b = _active_values(series_a), _active_values(series_b)
    total_b = sum_b["total_kwh"]
    return {
        "r": r, "mae": mae, "rmse": rmse, "n_common": len(common),
        "dur_a": _window_hours(sum_a), "dur_b": _window_hours(sum_b),
        "mean_a": (sum(act_a) / len(act_a)) if act_a else 0.0,
        "mean_b": (sum(act_b) / len(act_b)) if act_b else 0.0,
        "d_total": sum_a["total_kwh"] - total_b,
        "pct": ((sum_a["total_kwh"] - total_b) / total_b * 100.0)
               if total_b else float("nan"),
    }


def expected_fairness(exp_a, exp_b):
    """Pearson correlation between the two days' expected (available-sun) curves
    over daytime minutes -- an 'is this comparison fair?' indicator. High r means
    the two days had near-identical sun, so production differences reflect the
    system rather than the weather."""
    import numpy as np

    ma = {int(round(h * 60)): w for h, w in zip(exp_a["hours"], exp_a["exp_w"])}
    mb = {int(round(h * 60)): w for h, w in zip(exp_b["hours"], exp_b["exp_w"])}
    common = sorted(set(ma) & set(mb))
    a = np.array([ma[k] for k in common], dtype=float)
    b = np.array([mb[k] for k in common], dtype=float)
    lit = (a > 10.0) | (b > 10.0)                       # daytime only
    if lit.sum() >= 2 and a[lit].std() > 0 and b[lit].std() > 0:
        return float(np.corrcoef(a[lit], b[lit])[0, 1])
    return float("nan")


def fairness_verdict(r):
    """Plain-language read on the expected-sun correlation."""
    if math.isnan(r):
        return "n/a"
    if r >= 0.95:
        return "fair"
    if r >= 0.85:
        return "some caution"
    return "weather-confounded"


def signed_hm(delta_hours: float) -> str:
    """Signed H:MM string for a time-of-day difference (ASCII sign)."""
    sign = "+" if delta_hours >= 0 else "-"
    minutes = int(round(abs(delta_hours) * 60))
    return f"{sign}{minutes // 60}:{minutes % 60:02d}"


def print_comparison(date_a, date_b, sum_a, sum_b, cs):
    """Aligned, ASCII-only comparison table (safe on any Windows console)."""
    def w(x):    return f"{x:,.0f} W"
    def kwh(x):  return f"{x:.2f} kWh"

    def window(s):
        if s and s["first_h"] is not None:
            return f"{hm(s['first_h'])}-{hm(s['last_h'])}"
        return "-"

    pct = "" if math.isnan(cs["pct"]) else f" ({cs['pct']:+.1f}%)"
    rows = [
        ("Total energy", kwh(sum_a["total_kwh"]), kwh(sum_b["total_kwh"]),
         f"{cs['d_total']:+.2f} kWh{pct}"),
        ("Peak (1-min avg)", w(sum_a["peak_w"]), w(sum_b["peak_w"]),
         f"{sum_a['peak_w'] - sum_b['peak_w']:+,.0f} W"),
        ("Peak time", hm(sum_a["peak_hour"]), hm(sum_b["peak_hour"]),
         signed_hm(sum_a["peak_hour"] - sum_b["peak_hour"])),
        ("Peak instant", w(sum_a["peak_inst_w"]), w(sum_b["peak_inst_w"]),
         f"{sum_a['peak_inst_w'] - sum_b['peak_inst_w']:+,.0f} W"),
        ("Active window", window(sum_a), window(sum_b), ""),
        ("Active duration", f"{cs['dur_a']:.1f} h", f"{cs['dur_b']:.1f} h",
         f"{cs['dur_a'] - cs['dur_b']:+.1f} h"),
        ("Mean power (active)", w(cs["mean_a"]), w(cs["mean_b"]),
         f"{cs['mean_a'] - cs['mean_b']:+,.0f} W"),
    ]
    r_txt = "n/a" if math.isnan(cs["r"]) else f"r = {cs['r']:.3f}"
    tail = [
        ("Curve correlation", r_txt),
        ("Mean abs difference", w(cs["mae"])),
        ("RMSE", w(cs["rmse"])),
        ("Overlapping minutes", str(cs["n_common"])),
    ]

    print(f"\nComparison   A: {date_a}   vs   B: {date_b}\n")
    print(f"  {'':22}{'A: ' + date_a:<17}{'B: ' + date_b:<17}diff (A-B)")
    print("  " + "-" * 70)
    for label, a, b, d in rows:
        print(f"  {label:22}{a:<17}{b:<17}{d}")
    print()
    for label, val in tail:
        print(f"  {label:22}{val}")
    print()


# ------------------------------------------------------------------ plot
def plot_compare(series_a, series_b, sum_a, sum_b, cs, *, date_a, date_b,
                 channel, tz_label, quantity, x_start, x_end, theme, show_band,
                 out_path, do_show, expected_a=None, expected_b=None,
                 fair_r=float("nan")):
    import matplotlib
    if not do_show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter, MultipleLocator

    c = THEMES[theme]
    col_a, col_b = PAIR[theme]["a"], PAIR[theme]["b"]

    fig, ax = plt.subplots(figsize=(13, 6.2), dpi=110)
    fig.patch.set_facecolor(c["page"])
    ax.set_facecolor(c["surface"])

    if show_band:
        for s, col in ((series_a, col_a), (series_b, col_b)):
            if s["band_lo"]:
                ax.fill_between(s["hours"], s["band_lo"], s["band_hi"],
                                color=col, alpha=0.13, linewidth=0)
    # Expected (available-sun) curves: dashed and behind the production lines so
    # the raw production stays the visual focus.
    for exp, col, d in ((expected_a, col_a, date_a), (expected_b, col_b, date_b)):
        if exp:
            ax.plot(exp["hours"], exp["exp_w"], color=col, linewidth=1.2,
                    linestyle=(0, (5, 2.5)), alpha=0.32, zorder=2,
                    label=f"{d} expected")
    ax.plot(series_b["hours"], series_b["avg_w"], color=col_b, linewidth=2.0,
            solid_capstyle="round", label=f"{date_b} production", zorder=3)
    ax.plot(series_a["hours"], series_a["avg_w"], color=col_a, linewidth=2.0,
            solid_capstyle="round", label=f"{date_a} production", zorder=4)
    ax.axhline(0, color=c["axis"], linewidth=1.0)

    # Axes chrome
    ax.set_xlim(x_start, x_end)
    ax.xaxis.set_major_locator(MultipleLocator(2))
    ax.xaxis.set_minor_locator(MultipleLocator(1))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _pos: hm(x)))
    ax.margins(y=0.08)

    ax.set_xlabel(f"Time of day  ({tz_label})", color=c["ink2"], fontsize=10)
    ax.set_ylabel("Power  (W)", color=c["ink2"], fontsize=10)
    ax.set_title(f"{TITLE[quantity]}  —  {date_a}  vs  {date_b}"
                 f"   ·   Shelly Pro EM channel {channel}",
                 color=c["ink"], fontsize=14, fontweight="bold", pad=14)

    ax.grid(True, which="major", color=c["grid"], linewidth=0.8)
    ax.grid(True, which="minor", color=c["grid"], linewidth=0.4, alpha=0.6)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(c["axis"])
    ax.tick_params(colors=c["muted"], labelsize=9)

    # Top-left comparison panel: headline delta + each day's total (colour =
    # its curve, so this doubles as the legend).
    ax.text(0.015, 0.965, f"Δ {cs['d_total']:+.2f} kWh",
            transform=ax.transAxes, va="top", ha="left",
            color=c["ink"], fontsize=21, fontweight="bold", zorder=6)
    if not math.isnan(cs["pct"]):
        ax.text(0.017, 0.888, f"({cs['pct']:+.1f}% vs {date_b})",
                transform=ax.transAxes, va="top", ha="left",
                color=c["muted"], fontsize=10.5, zorder=6)
    a_pr = ("" if not expected_a or math.isnan(expected_a["pr"])
            else f"   PR {expected_a['pr'] * 100:.0f}%")
    b_pr = ("" if not expected_b or math.isnan(expected_b["pr"])
            else f"   PR {expected_b['pr'] * 100:.0f}%")
    ax.text(0.017, 0.815, f"{date_a}:  {sum_a['total_kwh']:.2f} kWh{a_pr}",
            transform=ax.transAxes, va="top", ha="left",
            color=col_a, fontsize=12.5, fontweight="bold", zorder=6)
    ax.text(0.017, 0.760, f"{date_b}:  {sum_b['total_kwh']:.2f} kWh{b_pr}",
            transform=ax.transAxes, va="top", ha="left",
            color=col_b, fontsize=12.5, fontweight="bold", zorder=6)
    if expected_a and expected_b:
        verdict = fairness_verdict(fair_r)
        vcol = {"fair": "#0ca30c", "some caution": "#c98500",
                "weather-confounded": "#d03b3b"}.get(verdict, c["muted"])
        rtxt = "n/a" if math.isnan(fair_r) else f"{fair_r:.2f}"
        ax.text(0.017, 0.700, f"Sun match  r = {rtxt}  ({verdict})",
                transform=ax.transAxes, va="top", ha="left",
                color=vcol, fontsize=11.5, fontweight="bold", zorder=6)

    # Bottom caption: the comparison metrics.
    parts = [f"Peak: {sum_a['peak_w']:,.0f} vs {sum_b['peak_w']:,.0f} W",
             f"Active: {cs['dur_a']:.1f} vs {cs['dur_b']:.1f} h"]
    if not math.isnan(cs["r"]):
        parts.append(f"production r = {cs['r']:.2f}")
    parts.append(f"mean abs diff {cs['mae']:,.0f} W")
    parts.append(f"RMSE {cs['rmse']:,.0f} W")
    fig.text(0.5, 0.005, "     ·     ".join(parts), ha="center", va="bottom",
             color=c["ink2"], fontsize=10)

    if expected_a or expected_b:
        leg = ax.legend(loc="upper right", frameon=False, fontsize=8.5)
        for txt in leg.get_texts():
            txt.set_color(c["ink2"])

    fig.tight_layout(rect=(0, 0.03, 1, 1))

    if out_path:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        fig.savefig(out_path, facecolor=fig.get_facecolor(), bbox_inches="tight")
        print(f"Saved chart to {out_path}")
    if do_show:
        plt.show()
    plt.close(fig)


# ------------------------------------------------------------------ main
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Compare Shelly Pro EM power for two days on one graph.")
    p.add_argument("date_a", help="First day (A), dd-mm-yyyy")
    p.add_argument("date_b", help="Second day (B), dd-mm-yyyy")
    p.add_argument("--ip", default=DEFAULT_IP,
                   help=f"Shelly device IP (default: {DEFAULT_IP})")
    p.add_argument("-c", "--channel", type=int, default=DEFAULT_CHANNEL,
                   help=f"EM channel id, 0 or 1 (default: {DEFAULT_CHANNEL} = PV)")
    p.add_argument("-q", "--quantity", default="production",
                   choices=["production", "consumption", "net"],
                   help="Which power to compare (default: production)")
    p.add_argument("--tz", default=None,
                   help="IANA timezone override (default: read from device)")
    p.add_argument("-o", "--output", default=None,
                   help="Save the chart to this PNG path")
    p.add_argument("--theme", default="light", choices=["light", "dark"],
                   help="Colour theme (default: light)")
    p.add_argument("--start", default="05:00",
                   help="Start of the plotted time-of-day window, HH:MM or hour "
                        "(default: 05:00)")
    p.add_argument("--end", default="22:00",
                   help="End of the plotted time-of-day window, HH:MM or hour "
                        "(default: 22:00)")
    p.add_argument("--band", action="store_true",
                   help="Also shade each day's intra-minute min/max band")
    p.add_argument("-e", "--expected", action="store_true",
                   help="Overlay each day's datasheet-based expected output "
                        "(dashed) and report per-day PR + a sun-match fairness r")
    p.add_argument("--lat", type=float, default=DEFAULT_LAT,
                   help=f"Latitude for the weather lookup (default: {DEFAULT_LAT})")
    p.add_argument("--lon", type=float, default=DEFAULT_LON,
                   help=f"Longitude for the weather lookup (default: {DEFAULT_LON})")
    p.add_argument("--tilt", type=float, default=DEFAULT_TILT,
                   help=f"Panel tilt deg, 0=flat 90=vertical (default: {DEFAULT_TILT:g})")
    p.add_argument("--azimuth", type=float, default=DEFAULT_AZIMUTH,
                   help="Panel azimuth in compass degrees (0=N, 90=E, 180=S, "
                        f"270=W; default: {DEFAULT_AZIMUTH:g})")
    p.add_argument("--kwp", type=float, default=DEFAULT_KWP,
                   help=f"Array STC nameplate, kWp (default: {DEFAULT_KWP:g})")
    p.add_argument("--temp-coeff", type=float, default=DEFAULT_TEMP_COEFF_PCT,
                   help="Pmax temperature coefficient, percent per degC "
                        f"(default: {DEFAULT_TEMP_COEFF_PCT:g})")
    p.add_argument("--nmot", type=float, default=DEFAULT_NMOT,
                   help=f"Nominal module operating temp, degC (default: {DEFAULT_NMOT:g})")
    p.add_argument("--inverter-eff", type=float, default=DEFAULT_INV_EFF,
                   help=f"Inverter efficiency, 0-1 (default: {DEFAULT_INV_EFF:g})")
    p.add_argument("--system-eff", type=float, default=DEFAULT_SYS_EFF,
                   help="Wiring/soiling/mismatch loss factor, 0-1 "
                        f"(default: {DEFAULT_SYS_EFF:g})")
    p.add_argument("--inverter-ac", type=float, default=DEFAULT_INV_AC_W,
                   help=f"Inverter AC cap for clipping, W (default: {DEFAULT_INV_AC_W:g})")
    p.add_argument("--no-show", action="store_true",
                   help="Do not open an interactive window (just save)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    day_a, day_b = parse_date(args.date_a), parse_date(args.date_b)
    disp_a, disp_b = day_a.strftime("%d-%m-%Y"), day_b.strftime("%d-%m-%Y")

    tzinfo, tz_label = resolve_timezone(args.ip, args.tz)
    print(f"Fetching {args.quantity} for {disp_a} and {disp_b} ({tz_label}) "
          f"from {args.ip} channel {args.channel} ...")
    series_a, sum_a, span_a = load_day(args.ip, args.channel, day_a, tzinfo,
                                       args.quantity)
    series_b, sum_b, span_b = load_day(args.ip, args.channel, day_b, tzinfo,
                                       args.quantity)
    print(f"  {len(series_a['avg_w'])} + {len(series_b['avg_w'])} "
          f"one-minute records retrieved.")

    span = max(span_a, span_b)
    try:
        x_start = parse_hour(args.start)
        x_end = parse_hour(args.end)
    except ValueError:
        sys.exit("Invalid --start/--end; use HH:MM or an hour (e.g. 05:00 or 5).")
    x_start = max(0.0, min(x_start, span))
    x_end = max(x_start + 0.01, min(x_end, span))

    cs = compare_stats(series_a, series_b, sum_a, sum_b)
    print_comparison(disp_a, disp_b, sum_a, sum_b, cs)

    expected_a = expected_b = None
    fair_r = float("nan")
    if args.expected and args.quantity != "production":
        print("  (expected overlay applies to --quantity production; skipped)",
              file=sys.stderr)
    elif args.expected:
        az_solar = compass_to_solar_azimuth(args.azimuth)
        print(f"  Fetching plane-of-array irradiance for {args.lat:.4f}, "
              f"{args.lon:.4f}  (tilt {args.tilt:g} deg, azimuth {args.azimuth:g} "
              f"deg compass) ...")
        model = dict(kwp=args.kwp, temp_coeff=args.temp_coeff / 100.0,
                     nmot=args.nmot, inv_eff=args.inverter_eff,
                     sys_eff=args.system_eff, inv_ac_w=args.inverter_ac)

        def _expected_for(day, series, summ):
            irr = fetch_irradiance(args.lat, args.lon, day, tz_label,
                                   args.tilt, az_solar)
            if not irr:
                return None
            hrs, wm2, _kind, temps = irr
            return build_expected(series, hrs, wm2, temps, **model,
                                  actual_kwh=summ["total_kwh"] if summ else 0.0)

        expected_a = _expected_for(day_a, series_a, sum_a)
        expected_b = _expected_for(day_b, series_b, sum_b)
        for disp, exp in ((disp_a, expected_a), (disp_b, expected_b)):
            if exp:
                pr = exp["pr"]
                prt = "n/a" if math.isnan(pr) else f"{pr * 100:.0f}%"
                print(f"  {disp}: expected {exp['expected_kwh']:.2f} kWh, "
                      f"PR {prt}, available sun {exp['poa_insol']:.2f} kWh/m2")
            else:
                print(f"  ({disp}: irradiance unavailable)", file=sys.stderr)
        if expected_a and expected_b:
            fair_r = expected_fairness(expected_a, expected_b)
            print(f"  Expected-sun match: r = {fair_r:.3f} "
                  f"({fairness_verdict(fair_r)})")

    plot_compare(series_a, series_b, sum_a, sum_b, cs,
                 date_a=disp_a, date_b=disp_b,
                 channel=args.channel, tz_label=tz_label, quantity=args.quantity,
                 x_start=x_start, x_end=x_end, theme=args.theme,
                 show_band=args.band, out_path=args.output,
                 do_show=not args.no_show,
                 expected_a=expected_a, expected_b=expected_b, fair_r=fair_r)


if __name__ == "__main__":
    main()
