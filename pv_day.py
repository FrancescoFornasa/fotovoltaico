#!/usr/bin/env python3
"""
pv_day.py -- Plot photovoltaic production for a single day from a Shelly Pro EM.

The Shelly Pro EM (Gen2) logs one energy record per minute on its internal
storage. This script pulls those records for a given local calendar date and
draws the power curve (Watts) at the device's finest stored resolution
(1 minute), plus a shaded band showing the min/max instantaneous power seen
within each minute.

Data model (per channel, per minute), from EM1Data.GetData:
    total_act_energy      Wh imported (consumed) during the minute
    total_act_ret_energy  Wh returned  (exported / PV production) during the minute
    max_act_power/min_act_power   signed W extremes seen within the minute

On this installation PV production flows in the "return" direction: production
shows up as total_act_ret_energy and as *negative* active power. Average power
for a minute is therefore  total_act_ret_energy (Wh) * 60  ->  Watts.

Examples:
    python pv_day.py 22-07-2026
    python pv_day.py 22-07-2026 --ip 192.168.0.103 --channel 1 -o day.png
    python pv_day.py 22-07-2026 --quantity consumption --channel 0
    python pv_day.py 22-07-2026 --weather   # overlay actual (cloud-affected) sun
    python pv_day.py 22-07-2026 --expected  # datasheet model: PR % and kWh lost
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_IP = "192.168.0.103"
DEFAULT_CHANNEL = 1          # channel 1 = PV production on this device
DEFAULT_TZ = "Europe/Rome"   # fallback if the device doesn't report one
DEFAULT_LAT = 45.40413928652185   # weather lookup location (override with --lat)
DEFAULT_LON = 10.985198862530082  # (override with --lon)
DEFAULT_TILT = 30.0               # panel tilt, degrees (0 = flat, 90 = vertical)
DEFAULT_AZIMUTH = 190.4           # panel azimuth, compass deg (0=N 90=E 180=S 270=W)
HTTP_TIMEOUT = 20            # seconds
PROD_THRESHOLD_W = 5.0       # minimum power counted as "producing"

# Expected-output model, from the panel + inverter datasheets (override on CLI):
DEFAULT_KWP = 3.0                 # array STC nameplate, kWp (6 x 500 Wp Peimar)
DEFAULT_TEMP_COEFF_PCT = -0.29    # Pmax temperature coefficient, % per degC
DEFAULT_NMOT = 43.0               # nominal module operating temperature, degC
DEFAULT_INV_EFF = 0.97            # Growatt MIC 3000TL-X conversion efficiency
DEFAULT_SYS_EFF = 0.95            # wiring / soiling / mismatch / reflection losses
DEFAULT_INV_AC_W = 3000.0         # inverter AC output cap, W (models clipping)

# ------------------------------------------------------------------ colours
# Palette from the dataviz design system (orange = solar). Both themes are
# hand-stepped for their surface, not an automatic flip.
THEMES = {
    "light": {
        "surface": "#fcfcfb", "page": "#f9f9f7",
        "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781",
        "grid": "#e1e0d9", "axis": "#c3c2b7",
        "series": "#eb6834", "fill": "#eb6834", "accent": "#2a78d6",
    },
    "dark": {
        "surface": "#1a1a19", "page": "#0d0d0d",
        "ink": "#ffffff", "ink2": "#c3c2b7", "muted": "#898781",
        "grid": "#2c2c2a", "axis": "#383835",
        "series": "#d95926", "fill": "#d95926", "accent": "#3987e5",
    },
}


# ------------------------------------------------------------------ RPC
def rpc(ip: str, method: str, params: dict | None = None) -> dict:
    """Call a Shelly Gen2 RPC method over HTTP GET and return the parsed JSON."""
    url = f"http://{ip}/rpc/{method}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        sys.exit(f"Could not reach {ip} ({method}): {exc}")
    except (ValueError, TimeoutError) as exc:
        sys.exit(f"Bad response from {ip} ({method}): {exc}")


# ------------------------------------------------------------------ timezone
def resolve_timezone(ip: str, override: str | None):
    """Return (tzinfo, label). Prefer the device's configured IANA zone; fall
    back to its current fixed UTC offset if the tz database is unavailable."""
    name = override
    if not name:
        cfg = rpc(ip, "Sys.GetConfig")
        name = (cfg.get("location") or {}).get("tz") or DEFAULT_TZ

    try:
        from zoneinfo import ZoneInfo  # stdlib; needs the 'tzdata' pkg on Windows
        return ZoneInfo(name), name
    except Exception:
        status = rpc(ip, "Sys.GetStatus")
        offset = int(status.get("utc_offset", 0))
        hours = offset / 3600
        print(
            f"  (tz database unavailable for '{name}'; using the device's current "
            f"fixed offset UTC{hours:+g}. Install 'tzdata' for DST-correct dates.)",
            file=sys.stderr,
        )
        return dt.timezone(dt.timedelta(seconds=offset)), f"UTC{hours:+g}"


# ------------------------------------------------------------------ fetch
def fetch_day(ip: str, channel: int, start_ts: int, end_ts: int):
    """Fetch every 1-minute record in [start_ts, end_ts). Returns (keys, rows)
    where rows is a list of (unix_ts, values_list), following pagination."""
    keys: list[str] | None = None
    rows: list[tuple[int, list[float]]] = []
    cursor = start_ts

    while cursor < end_ts:
        resp = rpc(ip, "EM1Data.GetData",
                   {"id": channel, "ts": cursor, "end_ts": end_ts})
        if keys is None:
            keys = resp.get("keys")
        if not keys:
            sys.exit("Device returned no data keys; is this an energy-metering "
                     "channel?")

        for block in resp.get("data", []):
            period = int(block.get("period", 60))
            base = int(block["ts"])
            for i, values in enumerate(block["values"]):
                rec_ts = base + i * period
                if start_ts <= rec_ts < end_ts:
                    rows.append((rec_ts, values))

        nxt = resp.get("next_record_ts")
        if not nxt or nxt <= cursor:
            break
        cursor = int(nxt)

    rows.sort(key=lambda r: r[0])
    return keys, rows


# ------------------------------------------------------------------ weather
def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def compass_to_solar_azimuth(compass_deg: float) -> float:
    """Compass bearing (0=N, 90=E, 180=S, 270=W) -> Open-Meteo panel azimuth
    (0 = south, -90 = east, +90 = west), normalised to (-180, 180]."""
    a = compass_deg - 180.0
    while a > 180.0:
        a -= 360.0
    while a <= -180.0:
        a += 360.0
    return a


def fetch_irradiance(lat: float, lon: float, day: dt.date, tz_name: str | None,
                     tilt: float, azimuth: float, model: str | None = None):
    """Plane-of-array (tilted) irradiance for `day` from Open-Meteo -- no API
    key. `tilt` is 0-90 deg; `azimuth` uses Open-Meteo's convention (0 = south,
    -90 = east, +90 = west). Uses the *instantaneous* products (`_instant`,
    sampled at each timestamp, not period-averaged) so they align in time with
    the 1-minute production data; still cloud-affected, so the curve dips on
    cloudy spells. Resolution ladder, finest first: 15-minute from the forecast
    endpoint (recent days, incl. today) -> hourly forecast -> hourly reanalysis
    archive (older days). `model` optionally forces an Open-Meteo weather model
    (e.g. 'italia_meteo_arpae_icon_2i') on the forecast endpoints; the archive is
    ERA5 and ignores it. Falls back to horizontal GHI if a source lacks the
    tilted product. Returns (hours, wm2, kind, temps) or None; temps is ambient
    air degC (NaN where the source lacks it)."""
    base_params = {
        "latitude": f"{lat:.6f}", "longitude": f"{lon:.6f}",
        "start_date": day.isoformat(), "end_date": day.isoformat(),
        "tilt": f"{tilt:g}", "azimuth": f"{azimuth:g}",
        "timezone": tz_name if (tz_name and "/" in tz_name) else "auto",
    }
    variables = ("global_tilted_irradiance_instant,"
                 "shortwave_radiation_instant,temperature_2m")
    stamp = day.isoformat()
    for base, block in (("https://api.open-meteo.com/v1/forecast", "minutely_15"),
                        ("https://api.open-meteo.com/v1/forecast", "hourly"),
                        ("https://archive-api.open-meteo.com/v1/archive", "hourly")):
        params = {**base_params, block: variables}
        if model and "forecast" in base:      # archive is ERA5, ignores models=
            params["models"] = model
        query = urllib.parse.urlencode(params)
        try:
            section = _get_json(f"{base}?{query}").get(block) or {}
        except (urllib.error.URLError, ValueError, TimeoutError):
            continue
        times = section.get("time") or []
        gti = section.get("global_tilted_irradiance_instant") or []
        ghi = section.get("shortwave_radiation_instant") or []
        temp_by_t = dict(zip(times, section.get("temperature_2m") or []))
        if any(v is not None and v > 0 for v in gti):
            values, kind = gti, "Sun on panels"          # plane-of-array
        elif any(v is not None and v > 0 for v in ghi):
            values, kind = ghi, "Sun available (horizontal)"
        else:
            continue
        hours, wm2, temps = [], [], []
        for t, v in zip(times, values):
            if v is not None and t.startswith(stamp):
                hours.append(int(t[11:13]) + int(t[14:16]) / 60.0)
                wm2.append(float(v))
                tv = temp_by_t.get(t)
                temps.append(float(tv) if tv is not None else float("nan"))
        if any(v > 0 for v in wm2):
            return hours, wm2, kind, temps
    return None


def build_reference(series, irradiance):
    """Scale actual irradiance to Watts so it overlays production as 'power the
    sun made available'. The factor k is a least-squares fit through the origin
    (production ~= k * GHI over daylight); because GHI already includes cloud
    losses, the reference dips on cloudy spells too. Returns hours, ref_w, k, r
    (r = how tightly production tracked the sun)."""
    import numpy as np

    w_hours, w_ghi = irradiance
    ph = np.array(series["hours"], dtype=float)
    pw = np.array(series["avg_w"], dtype=float)
    gi = np.interp(ph, w_hours, w_ghi)          # GHI at each production minute

    lit = gi > 5.0                              # daylight samples only
    if lit.sum() >= 2 and float((gi[lit] ** 2).sum()) > 0:
        k = float((pw[lit] * gi[lit]).sum() / (gi[lit] ** 2).sum())
    else:
        k = 0.0
    if lit.sum() >= 2 and pw[lit].std() > 0 and gi[lit].std() > 0:
        r = float(np.corrcoef(pw[lit], gi[lit])[0, 1])
    else:
        r = float("nan")
    return {"hours": series["hours"], "ref_w": (k * gi).tolist(), "k": k, "r": r}


def build_expected(series, w_hours, poa, temps, *, kwp, temp_coeff, nmot,
                   inv_eff, sys_eff, inv_ac_w, actual_kwh):
    """Physically-grounded expected AC output from the datasheets, per minute:

        expected = kWp * POA * [1 + tc*(Tcell-25)] * inv_eff * sys_eff   (capped)

    Tcell is estimated from ambient air temperature and the NMOT model. There is
    no shading term -- the gap between this and actual production IS the loss.
    `temp_coeff` is a fraction per degC (e.g. -0.0029). Returns hours, exp_w,
    expected_kwh, poa_insol (kWh/m2), pr, lost_kwh, temp_used."""
    import numpy as np

    ph = np.array(series["hours"], dtype=float)
    gi = np.interp(ph, w_hours, poa).clip(min=0.0)          # POA at each minute

    temp_used = bool(temps) and all(math.isfinite(t) for t in temps)
    if temp_used:
        t_air = np.interp(ph, w_hours, temps)
        t_cell = t_air + (gi / 800.0) * (nmot - 20.0)       # NMOT model
        temp_factor = 1.0 + temp_coeff * (t_cell - 25.0)
    else:
        temp_factor = 1.0

    exp = kwp * gi * temp_factor * inv_eff * sys_eff        # W (kWp*W/m2 -> W)
    exp = np.minimum(exp, inv_ac_w)                         # inverter clipping
    exp = np.where(gi > 0.0, exp, 0.0)

    expected_kwh = float(exp.sum()) / 60.0 / 1000.0         # 1 sample = 1 minute
    poa_insol = float(gi.sum()) / 60.0 / 1000.0             # kWh/m2
    pr = actual_kwh / (kwp * poa_insol) if poa_insol > 0 else float("nan")
    return {
        "hours": series["hours"], "exp_w": exp.tolist(),
        "expected_kwh": expected_kwh, "poa_insol": poa_insol,
        "pr": pr, "lost_kwh": expected_kwh - actual_kwh, "temp_used": temp_used,
    }


# ------------------------------------------------------------------ series
def build_series(keys, rows, tzinfo, midnight, quantity):
    """Turn raw records into plot-ready arrays.

    Returns dict with: hours, avg_w, band_lo, band_hi, energy_wh (per-minute
    energy used for the daily total)."""
    i_fwd = keys.index("total_act_energy")
    i_ret = keys.index("total_act_ret_energy")
    i_pmax = keys.index("max_act_power")
    i_pmin = keys.index("min_act_power")

    hours, avg_w, band_lo, band_hi, energy_wh = [], [], [], [], []
    for rec_ts, v in rows:
        local = dt.datetime.fromtimestamp(rec_ts, tz=tzinfo)
        hours.append((local - midnight).total_seconds() / 3600.0)

        fwd, ret = float(v[i_fwd]), float(v[i_ret])
        pmax, pmin = float(v[i_pmax]), float(v[i_pmin])

        if quantity == "production":       # export: positive Watts, negate power
            avg_w.append(ret * 60.0)
            band_lo.append(-pmax)          # least negative power -> lowest prod
            band_hi.append(-pmin)          # most negative power  -> highest prod
            energy_wh.append(ret)
        elif quantity == "consumption":    # import
            avg_w.append(fwd * 60.0)
            band_lo.append(pmin)
            band_hi.append(pmax)
            energy_wh.append(fwd)
        else:                              # net: positive = import
            avg_w.append((fwd - ret) * 60.0)
            band_lo.append(pmin)
            band_hi.append(pmax)
            energy_wh.append(fwd - ret)

    return {
        "hours": hours, "avg_w": avg_w,
        "band_lo": band_lo, "band_hi": band_hi, "energy_wh": energy_wh,
    }


def summarize(series, quantity):
    """Compute headline numbers for the day."""
    avg_w = series["avg_w"]
    if not avg_w:
        return None
    total_wh = sum(series["energy_wh"])
    peak_i = max(range(len(avg_w)), key=lambda i: avg_w[i])
    active = [h for h, w in zip(series["hours"], avg_w) if abs(w) > PROD_THRESHOLD_W]
    return {
        "total_kwh": total_wh / 1000.0,
        "peak_w": avg_w[peak_i],
        "peak_hour": series["hours"][peak_i],
        "peak_inst_w": max(series["band_hi"]) if series["band_hi"] else avg_w[peak_i],
        "first_h": min(active) if active else None,
        "last_h": max(active) if active else None,
        "n_minutes": len(avg_w),
    }


# ------------------------------------------------------------------ plot
def hm(hour_float: float) -> str:
    """Format an hours-since-midnight float as HH:MM."""
    total_min = int(round(hour_float * 60))
    return f"{total_min // 60:02d}:{total_min % 60:02d}"


def parse_hour(text: str) -> float:
    """Parse 'HH', 'HH:MM', or a decimal hour into hours-since-midnight."""
    text = str(text).strip()
    if ":" in text:
        h, m = text.split(":", 1)
        return int(h) + int(m) / 60.0
    return float(text)


def plot(series, summary, *, date_str, channel, tz_label, quantity,
         x_start, x_end, theme, show_band, out_path, do_show, weather=None,
         expected=None):
    import matplotlib
    if not do_show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter, MultipleLocator

    c = THEMES[theme]
    label = {"production": "PV production",
             "consumption": "Consumption",
             "net": "Net power (import +)"}[quantity]

    fig, ax = plt.subplots(figsize=(13, 6.2), dpi=110)
    fig.patch.set_facecolor(c["page"])
    ax.set_facecolor(c["surface"])

    if show_band and series["band_lo"]:
        ax.fill_between(series["hours"], series["band_lo"], series["band_hi"],
                        color=c["muted"], alpha=0.38, linewidth=0,
                        label="min-max within each minute")
    if weather:
        ax.plot(weather["hours"], weather["ref_w"], color=c["ink2"],
                linewidth=1.7, linestyle=(0, (6, 3)), alpha=0.85, zorder=1.8,
                label=f"{weather['kind']} (× {weather['k']:.2f})")
    ax.plot(series["hours"], series["avg_w"],
            color=c["series"], linewidth=2.0, solid_capstyle="round",
            zorder=4, label="1-minute average")

    if expected:
        exp_w = expected["exp_w"]
        ax.fill_between(series["hours"], series["avg_w"], exp_w,
                        where=[e > a for a, e in zip(series["avg_w"], exp_w)],
                        color="#d03b3b", alpha=0.10, linewidth=0, zorder=1.5,
                        label="shortfall vs expected")
        pr = expected["pr"]
        pr_txt = "" if math.isnan(pr) else f"  (PR {pr * 100:.0f}%)"
        ax.plot(expected["hours"], exp_w, color=c["accent"], linewidth=1.8,
                linestyle=(0, (5, 2.5)), alpha=0.9, zorder=2.2,
                label=f"Expected output{pr_txt}")

    ax.axhline(0, color=c["axis"], linewidth=1.0)

    # Peak marker + label
    if summary and summary["peak_w"] > PROD_THRESHOLD_W:
        px, py = summary["peak_hour"], summary["peak_w"]
        ax.plot([px], [py], "o", color=c["series"], markersize=7,
                markeredgecolor=c["surface"], markeredgewidth=1.5, zorder=5)
        ax.annotate(f"{py:,.0f} W  @ {hm(px)}",
                    (px, py), textcoords="offset points", xytext=(8, 8),
                    color=c["ink"], fontsize=10, fontweight="bold")

    # Axes chrome
    ax.set_xlim(x_start, x_end)
    ax.xaxis.set_major_locator(MultipleLocator(2))
    ax.xaxis.set_minor_locator(MultipleLocator(1))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _pos: hm(x)))
    ax.margins(y=0.08)

    ax.set_xlabel(f"Time of day  ({tz_label})", color=c["ink2"], fontsize=10)
    ax.set_ylabel("Power  (W)", color=c["ink2"], fontsize=10)
    ax.set_title(f"{label}  —  {date_str}   ·   Shelly Pro EM channel {channel}",
                 color=c["ink"], fontsize=14, fontweight="bold", pad=14)

    ax.grid(True, which="major", color=c["grid"], linewidth=0.8)
    ax.grid(True, which="minor", color=c["grid"], linewidth=0.4, alpha=0.6)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(c["axis"])
    ax.tick_params(colors=c["muted"], labelsize=9)

    # Hero number: total energy for the day, top-left where the curve is low
    if summary:
        hero_label = {"production": "Total production",
                      "consumption": "Total consumption",
                      "net": "Net energy"}[quantity]
        ax.text(0.015, 0.96, f"{summary['total_kwh']:.2f} kWh",
                transform=ax.transAxes, ha="left", va="top",
                color=c["ink"], fontsize=27, fontweight="bold", zorder=6)
        ax.text(0.017, 0.885, hero_label,
                transform=ax.transAxes, ha="left", va="top",
                color=c["muted"], fontsize=10.5, zorder=6)

    # Summary caption (peak + active window)
    if summary:
        parts = [f"Peak (1-min avg): {summary['peak_w']:,.0f} W",
                 f"Peak instant: {summary['peak_inst_w']:,.0f} W"]
        if summary["first_h"] is not None:
            parts.append(f"Active: {hm(summary['first_h'])}–{hm(summary['last_h'])}")
        if weather and not math.isnan(weather["r"]):
            parts.append(f"Sun-tracking r = {weather['r']:.2f}")
        if expected:
            pr = expected["pr"]
            parts.append(f"Expected {expected['expected_kwh']:.2f} kWh")
            if not math.isnan(pr):
                parts.append(f"PR {pr * 100:.0f}%")
            parts.append(f"shortfall {expected['lost_kwh']:+.2f} kWh")
        fig.text(0.5, 0.005, "     ".join(parts), ha="center", va="bottom",
                 color=c["ink2"], fontsize=10)

    leg = ax.legend(loc="upper right", frameon=False, fontsize=9)
    for txt in leg.get_texts():
        txt.set_color(c["ink2"])

    fig.tight_layout(rect=(0, 0.03, 1, 1))

    if out_path:
        out_dir = os.path.dirname(os.path.abspath(out_path))
        os.makedirs(out_dir, exist_ok=True)
        fig.savefig(out_path, facecolor=fig.get_facecolor(), bbox_inches="tight")
        print(f"Saved chart to {out_path}")
    if do_show:
        plt.show()
    plt.close(fig)


# ------------------------------------------------------------------ main
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Plot Shelly Pro EM power for a given day at 1-minute resolution.")
    p.add_argument("date", help="Local calendar date to plot, dd-mm-yyyy")
    p.add_argument("--ip", default=DEFAULT_IP,
                   help=f"Shelly device IP (default: {DEFAULT_IP})")
    p.add_argument("-c", "--channel", type=int, default=DEFAULT_CHANNEL,
                   help=f"EM channel id, 0 or 1 (default: {DEFAULT_CHANNEL} = PV)")
    p.add_argument("-q", "--quantity", default="production",
                   choices=["production", "consumption", "net"],
                   help="Which power to plot (default: production)")
    p.add_argument("--tz", default=None,
                   help="IANA timezone override (default: read from device)")
    p.add_argument("-w", "--weather", action="store_true",
                   help="Overlay actual (cloud-affected) solar irradiance, "
                        "scaled to Watts (Open-Meteo, no API key)")
    p.add_argument("--lat", type=float, default=DEFAULT_LAT,
                   help=f"Latitude for the weather lookup (default: {DEFAULT_LAT})")
    p.add_argument("--lon", type=float, default=DEFAULT_LON,
                   help=f"Longitude for the weather lookup (default: {DEFAULT_LON})")
    p.add_argument("--tilt", type=float, default=DEFAULT_TILT,
                   help=f"Panel tilt deg, 0=flat 90=vertical (default: {DEFAULT_TILT:g})")
    p.add_argument("--azimuth", type=float, default=DEFAULT_AZIMUTH,
                   help="Panel azimuth in compass degrees (0=N, 90=E, 180=S, "
                        f"270=W; default: {DEFAULT_AZIMUTH:g})")
    p.add_argument("-e", "--expected", action="store_true",
                   help="Overlay datasheet-based expected output; report "
                        "Performance Ratio and kWh lost (needs internet)")
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
    p.add_argument("--model", default=None,
                   help="Force an Open-Meteo weather model on the forecast "
                        "endpoints, e.g. italia_meteo_arpae_icon_2i (default: "
                        "Open-Meteo best-match blend)")
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
    p.add_argument("--no-band", action="store_true",
                   help="Hide the intra-minute min/max band")
    p.add_argument("--no-show", action="store_true",
                   help="Do not open an interactive window (just save)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    try:
        day = dt.datetime.strptime(args.date, "%d-%m-%Y").date()
    except ValueError:
        sys.exit(f"Invalid date '{args.date}'. Use dd-mm-yyyy, e.g. 22-07-2026.")
    date_disp = day.strftime("%d-%m-%Y")

    tzinfo, tz_label = resolve_timezone(args.ip, args.tz)
    midnight = dt.datetime.combine(day, dt.time.min, tzinfo=tzinfo)
    next_midnight = dt.datetime.combine(day + dt.timedelta(days=1),
                                        dt.time.min, tzinfo=tzinfo)
    start_ts = int(midnight.timestamp())
    end_ts = int(next_midnight.timestamp())
    span_hours = (next_midnight - midnight).total_seconds() / 3600.0

    try:
        x_start = parse_hour(args.start)
        x_end = parse_hour(args.end)
    except ValueError:
        sys.exit("Invalid --start/--end; use HH:MM or an hour (e.g. 05:00 or 5).")
    x_start = max(0.0, min(x_start, span_hours))
    x_end = max(x_start + 0.01, min(x_end, span_hours))

    print(f"Fetching {args.quantity} for {date_disp} ({tz_label}) "
          f"from {args.ip} channel {args.channel} ...")
    keys, rows = fetch_day(args.ip, args.channel, start_ts, end_ts)
    print(f"  {len(rows)} one-minute records retrieved.")

    if not rows:
        sys.exit("No stored data for that day (device may have been off, or the "
                 "date predates the earliest record).")

    series = build_series(keys, rows, tzinfo, midnight, args.quantity)
    summary = summarize(series, args.quantity)

    if summary:
        print(f"  Total: {summary['total_kwh']:.2f} kWh   "
              f"Peak 1-min avg: {summary['peak_w']:,.0f} W   "
              f"Peak instant: {summary['peak_inst_w']:,.0f} W")
        if summary["first_h"] is not None:
            print(f"  Active window: {hm(summary['first_h'])}"
                  f"–{hm(summary['last_h'])}")

    weather = expected = None
    want_wx = args.weather or args.expected
    if want_wx and args.quantity != "production":
        print("  (weather/expected overlays apply to --quantity production; "
              "skipped)", file=sys.stderr)
    elif want_wx:
        az_solar = compass_to_solar_azimuth(args.azimuth)
        print(f"  Fetching plane-of-array irradiance for {args.lat:.4f}, "
              f"{args.lon:.4f}  (tilt {args.tilt:g} deg, azimuth {args.azimuth:g} "
              f"deg compass) ...")
        irr = fetch_irradiance(args.lat, args.lon, day, tz_label,
                               args.tilt, az_solar, model=args.model)
        if not irr:
            print("  (irradiance unavailable for that date/location; plotting "
                  "production only)", file=sys.stderr)
        else:
            hours, wm2, kind, temps = irr
            if args.weather:
                weather = build_reference(series, (hours, wm2))
                weather["kind"] = kind
                extra = ("" if math.isnan(weather["r"])
                         else f"   (production vs sun  r = {weather['r']:.2f})")
                print(f"  {kind}: {weather['k']:.2f} W per W/m2{extra}")
            if args.expected:
                expected = build_expected(
                    series, hours, wm2, temps,
                    kwp=args.kwp, temp_coeff=args.temp_coeff / 100.0,
                    nmot=args.nmot, inv_eff=args.inverter_eff,
                    sys_eff=args.system_eff, inv_ac_w=args.inverter_ac,
                    actual_kwh=summary["total_kwh"] if summary else 0.0)
                pr = expected["pr"]
                pr_txt = "n/a" if math.isnan(pr) else f"{pr * 100:.0f}%"
                notemp = "" if expected["temp_used"] else ", no temp data"
                print(f"  Expected (model): {expected['expected_kwh']:.2f} kWh   "
                      f"Actual: {summary['total_kwh']:.2f} kWh   "
                      f"Shortfall: {expected['lost_kwh']:+.2f} kWh")
                print(f"  Performance ratio: {pr_txt}   "
                      f"(POA insolation {expected['poa_insol']:.2f} kWh/m2"
                      f"{notemp})")

    plot(series, summary, weather=weather, expected=expected,
         date_str=date_disp, channel=args.channel, tz_label=tz_label,
         quantity=args.quantity, x_start=x_start, x_end=x_end, theme=args.theme,
         show_band=not args.no_band, out_path=args.output,
         do_show=not args.no_show)


if __name__ == "__main__":
    main()
