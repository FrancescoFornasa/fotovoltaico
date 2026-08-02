# fotovoltaico — PV production monitoring & analysis

A small toolkit of Python scripts that read 1‑minute production data from a
**Shelly Pro EM** energy meter and turn it into readable charts: a single day,
a head‑to‑head comparison of two days, and a multi‑day trend. Optionally it
overlays a physically‑grounded **expected‑output model** (built from the panel
and inverter datasheets + live weather) so you can see not just *how much* the
array produced, but *how well* it performed relative to the sun that was
actually available.

The toolkit was built to answer one concrete question — **did the per‑panel
optimizers installed on 2026‑07‑23 fix the afternoon shading?** — but the
scripts are general‑purpose.

---

## Table of contents

- [What you get](#what-you-get)
- [The physical system](#the-physical-system)
- [How production is measured](#how-production-is-measured)
- [Requirements & setup](#requirements--setup)
- [Quick start](#quick-start)
- [The three scripts](#the-three-scripts)
  - [pv_day.py](#pv_daypy--one-day)
  - [pv_compare.py](#pv_comparepy--two-days-head-to-head)
  - [pv_trend.py](#pv_trendpy--all-days--optimizer-verdict)
- [Weather & irradiance (Open‑Meteo)](#weather--irradiance-openmeteo)
- [The expected‑output model — in detail](#the-expectedoutput-model--in-detail)
- [Performance Ratio & the fairness check](#performance-ratio--the-fairness-check)
- [Caveats & limitations](#caveats--limitations)
- [Repo layout](#repo-layout)

---

## What you get

- **Finest‑resolution production curves** — 1 minute, the finest the device stores.
- **Actual (cloud‑affected) irradiance overlay** — what the sun really did, from
  Open‑Meteo, no API key.
- **Expected‑output model** — datasheet + weather → the power a *healthy* copy of
  your system should have made, minute by minute, with a **Performance Ratio (%)**
  and **kWh lost**.
- **Fair two‑day comparison** — with a "sun‑match" correlation that tells you
  whether the two days are even comparable.
- **Multi‑day optimizer verdict** — before/after dashboards with whole‑day and
  afternoon Performance Ratio.
- Consistent look (light/dark themes), dd‑mm‑yyyy dates, configurable time
  windows, and selectable weather models — shared across all three scripts.

---

## The physical system

| Component | Detail |
|---|---|
| **Meter** | Shelly Pro EM 50 (Gen2), induction clamps, 1‑minute internal logging |
| **PV channel** | `em1` **channel 1** — production flows in the *return* (export) direction |
| **Array** | 6 × **Peimar OR10H500MNDB (BF)** = **3.0 kWp** (front), bifacial TOPCon N‑type |
| **Module** | 500 Wp, efficiency 23.17 %, area 1.903 × 1.134 m, Vmp 36.93 V / Imp 13.54 A |
| **Temp. coefficient (Pmax)** | −0.29 %/°C · thermal model Faiman **U0 22.5** / **U1 6.84** (NMOT 43 °C as fallback) |
| **Inverter** | **Growatt MIC 3000TL‑X** — 3000 W AC, single‑MPPT, transformerless, ~97 % efficient |
| **Orientation** | tilt **10°** (measured on the roof, 31-07-2026), azimuth **≈190°** compass (just west of due south) |
| **Location** | ≈ 45.404 N, 10.985 E (near Verona, Italy), tz Europe/Rome |
| **Optimizers** | per‑panel DC optimizers installed **2026‑07‑23 ~14:00** to mitigate afternoon shading |

These values are the built‑in defaults; every one can be overridden on the
command line.

---

## How production is measured

The Shelly Pro EM is a Gen2 device with an internal data logger. Each script
pulls per‑minute records over the local‑network **RPC API** (`EM1Data.GetData`,
paginated). Every minute record contains, among other fields:

| field | meaning |
|---|---|
| `total_act_energy` | Wh **imported** (consumed) during that minute |
| `total_act_ret_energy` | Wh **returned** (exported / PV production) during that minute |
| `min_act_power` / `max_act_power` | signed Watt extremes seen within that minute |

On this installation the PV inverter feeds power *back* through the clamp, so
**production shows up as returned energy** (and as *negative* active power) on
**channel 1**. The average power for a minute is therefore:

```
avg_power_W = total_act_ret_energy_Wh × 60
```

(1 minute = 1/60 h, so Wh × 60 = average W.) The shaded band on the single‑day
chart is the intra‑minute min/max power the device recorded.

> Channel 0 on this device is a consumption channel; use `--channel 0
> --quantity consumption` to look at it.

---

## Requirements & setup

- Python 3.12
- `matplotlib` (charts) and `tzdata` (correct DST handling on Windows)
- HTTP uses the standard library — no `requests` needed
- Network access to the Shelly (LAN) and to `api.open-meteo.com` for the
  optional weather/expected overlays

Create the environment (example with conda):

```bash
conda create -y -n fotovoltaico python=3.12 matplotlib tzdata
conda activate fotovoltaico
```

---

## Quick start

```bash
# one day
python pv_day.py 24-07-2026

# one day, with the expected-output model (needs internet)
python pv_day.py 24-07-2026 --expected

# compare two days, with per-day expected + fairness check
python pv_compare.py 24-07-2026 22-07-2026 --expected

# every stored day, before/after the optimizer install
python pv_trend.py

# save a PNG instead of opening a window
python pv_trend.py --no-show -o trend.png
```

Dates are **dd‑mm‑yyyy**. Saved PNGs are rendered at 200 dpi.

---

## The three scripts

All three share one data + model pipeline (defined in `pv_day.py` and imported
by the others), so a fix in one place benefits all.

### `pv_day.py` — one day

Plots a single day's power curve at 1‑minute resolution, a hero total (kWh), a
peak marker, and (optionally) the weather/expected overlays.

```bash
python pv_day.py <dd-mm-yyyy> [options]
```

| Option | Default | Meaning |
|---|---|---|
| `date` (positional) | — | Day to plot, dd‑mm‑yyyy |
| `--ip` | 192.168.0.103 | Shelly device IP |
| `-c, --channel` | 1 | EM channel (1 = PV) |
| `-q, --quantity` | production | `production` \| `consumption` \| `net` |
| `--tz` | (device) | IANA timezone override |
| `-w, --weather` | off | Overlay actual irradiance, scaled to W (fitted) |
| `-e, --expected` | off | Overlay datasheet expected output + PI + PR + kWh lost |
| `--lat` / `--lon` | 45.404 / 10.985 | Weather location |
| `--tilt` / `--azimuth` | 10 / 190.4 | Panel geometry (azimuth in compass degrees) |
| `--u0` / `--u1` | 22.5 / 6.84 | Faiman cell‑temperature coefficients |
| `--kwp` | 3.0 | Array STC nameplate (kWp) |
| `--temp-coeff` | −0.29 | Pmax temp coefficient (%/°C) |
| `--nmot` | 43 | Nominal module operating temp (°C) |
| `--inverter-eff` | 0.97 | Inverter efficiency (0–1) |
| `--system-eff` | 0.95 | Wiring/soiling/mismatch factor (0–1) |
| `--inverter-ac` | 3000 | Inverter AC cap for clipping (W) |
| `--model` | best‑match | Force an Open‑Meteo model (e.g. `italia_meteo_arpae_icon_2i`) |
| `--start` / `--end` | 05:00 / 22:00 | Plotted time‑of‑day window |
| `--theme` | light | `light` \| `dark` |
| `--no-band` | off | Hide the intra‑minute min/max band |
| `-o, --output` | — | Save PNG to this path |
| `--no-show` | off | Don't open a window (just save) |

`--weather` draws a *fitted* "sun available" line (irradiance scaled to Watts by
a least‑squares fit). `--expected` draws the *physical* model line described
[below](#the-expectedoutput-model--in-detail), a red shortfall fill, the
Performance Index and Ratio, the peak cell temperature, and the kWh lost.

### `pv_compare.py` — two days head‑to‑head

Overlays two days' production curves, a headline Δ kWh, per‑day totals, and a
full statistical comparison (peak, active window, Pearson correlation, MAE,
RMSE). With `--expected`, each day also gets a faint dashed expected curve, a
per‑day **PR**, and a **"sun‑match" fairness r** — the correlation between the
two days' expected curves, telling you whether the two days are comparable.

```bash
python pv_compare.py <dd-mm-yyyy A> <dd-mm-yyyy B> [options]
```

Same option set as `pv_day.py` for connection/geometry/model, plus `--band`
(shade each day's intra‑minute range) and `--start` / `--end` (view window).
It has no `-q`/`--no-band` differences worth memorising — run `--help`.

### `pv_trend.py` — all days & optimizer verdict

Auto‑discovers every day the device has stored (via `EM1Data.GetRecords`) and
draws a 2×2 dashboard:

|  | whole‑day (left) | afternoon (right) |
|---|---|---|
| **production (top)** | absolute daily kWh | afternoon kWh (from 14:00) |
| **normalised (bottom)** | whole‑day **PI** bars, PR as hollow ○ markers | afternoon PR % |

Days are coloured **before / install‑day / after** the optimizer date; the bottom
panels carry before/after mean lines; partial days (device started mid‑day, or
today so far) are faded and excluded from the averages.

The bottom‑left panel deliberately shows **both** metrics: the bars are PI and the
hollow markers are PR, so the vertical gap between them is the thermal model's
claimed loss. A gap that widens while the bars stay level means a hot spell, not a
fault — see [Performance Ratio & Index](#performance-ratio--the-fairness-check).

```bash
python pv_trend.py [options]
```

Key extra options: `--optimizer-date` (default 23‑07‑2026, the before/after
split), `--afternoon` (default 14:00, the afternoon window), `--start-date` /
`--end-date` (override the auto‑discovered range). Plus the same
geometry/model/theme flags as the others.

> **Note:** a full run fetches every day from the device (many paginated calls),
> so it takes a couple of minutes for a week and longer for a month.

---

## Weather & irradiance (Open‑Meteo)

All the weather comes from **[Open‑Meteo](https://open-meteo.com)** — free, no
API key. The scripts request **plane‑of‑array (tilted) irradiance**
(`global_tilted_irradiance`) for your exact tilt/azimuth, i.e. the sunlight
actually landing on the panels, not on a flat surface, plus `temperature_2m` and
`wind_speed_10m` — the two inputs the Faiman cell‑temperature model needs.

Three things were important to get right:

1. **Instantaneous, not hour‑averaged.** Open‑Meteo's default hourly radiation
   is the *average over the preceding hour*, stamped at the hour's end. Plotting
   that as if it were the instantaneous value shifts the curve ~30 minutes late
   (making mornings look under‑ and afternoons over‑performing). The scripts use
   the **`_instant`** products, which are sampled *at* each timestamp and line up
   with the 1‑minute production data.

2. **Finest resolution available.** A resolution ladder is tried in order:
   **15‑minute forecast → hourly forecast → hourly reanalysis archive (ERA5)**.
   Recent days (incl. today) get true 15‑minute detail; older days fall back to
   hourly. The values are then linearly interpolated onto the 1‑minute grid.

3. **Selectable model (`--model`).** By default Open‑Meteo picks its *best‑match*
   blend, which carries genuine sub‑hourly detail (it can show a sudden cloud
   passing). You can force a specific model, e.g.
   `--model italia_meteo_arpae_icon_2i` — the ItaliaMeteo‑ARPAE **ICON‑2I**, a
   ~2 km model over Italy that is spatially finer but *hourly‑native* (its
   15‑minute values are interpolated, so it smooths over fast cloud events).
   Rule of thumb: **best‑match for fast‑changing days and for the fairness check;
   ICON‑2I for the most accurate daily magnitude on calm days.** The archive
   endpoint is always ERA5 and ignores `--model`.

---

## The expected‑output model — in detail

This is the heart of `--expected`. The goal: for every minute, estimate the AC
power that a **healthy copy of this specific system** should have produced given
the sun that was actually available — with **no shading term**, so that the gap
between this line and your real production *is* the loss you want to find.

### The formula

For each minute `t`:

```
expected_W(t) = kWp × POA(t) × [ 1 + γ·(T_cell(t) − 25) ] × η_inv × η_sys
expected_W(t) = min( expected_W(t), inverter_AC_cap )        # inverter clipping
```

with the cell temperature estimated from ambient air temperature, sunlight and
**wind**, via the Faiman model:

```
T_cell(t) = T_air(t) + POA(t) / ( U0 + U1 × wind_ms(t) )
```

Read it left to right as *"ideal power → scaled by how much sun → minus heat →
minus conversion → minus everything else → capped by the inverter."* Term by
term, with this system's defaults:

**`kWp` — the nameplate (at STC).**
Your 6 × 500 Wp panels make **3.0 kW** of DC at *Standard Test Conditions*
(1000 W/m², 25 °C cell, reference spectrum). Everything else translates that lab
number into a real rooftop minute. (Unit note: `kWp × POA(W/m²)` yields Watts,
because `P_STC = kWp × 1000 W` at 1000 W/m², so `kWp × POA = P_STC × POA/1000`.)

**`POA(t)` — how much sun is really landing (W/m²).**
Plane‑of‑array irradiance from Open‑Meteo (instantaneous, cloud‑affected). A
cell's output is essentially linear in light intensity, so this term — the ratio
to the 1000 W/m² STC reference — is the big, minute‑to‑minute driver that gives
the curve its bell shape. Example: `POA = 900` → the array sees 90 % of STC light.

**`[1 + γ·(T_cell − 25)]` — the heat penalty.**
Silicon panels *lose* power as they heat up. `γ` is the Pmax temperature
coefficient, here **−0.29 %/°C** (`--temp-coeff -0.29`, used internally as the
fraction −0.0029), referenced to the 25 °C STC point:

- cell at 25 °C → factor **1.00** (no penalty)
- cell at 60 °C → `1 − 0.0029·35 =` **0.90** (a 10 % loss)
- cell at 10 °C on a crisp sunny day → `1 − 0.0029·(−15) =` **1.04** (a 4 % *bonus*)

This single term is the main reason the same sun yields different power on a hot
vs a cool day.

**`T_cell` — estimated, not measured (the Faiman model).**
Cells run hotter than the air because they absorb sunlight, and how *much* hotter
depends on how fast the wind carries that heat away. The **Faiman** model splits
the cooling into a still‑air term and a wind term:

```
T_cell = T_air + POA / (U0 + U1 × wind_ms)
```

- **`U0` — still‑air cooling (W/m²K), default 22.5.** Fitted to this array over
  24–31 July 2026. It sits below the ~25 typical of a free‑standing rack because
  these panels are mounted nearly flush at 10° tilt, so air can barely move behind
  them. Lower `U0` = worse cooling = hotter cells. Override with `--u0`.
- **`U1` — wind cooling (W/m³sK), default 6.84.** The standard literature value.
  Override with `--u1`.

`T_air` and `wind_speed_10m` both come from Open‑Meteo (wind arrives in km/h and
is converted to m/s internally). Example: air 30 °C, POA 900, wind 4 km/h (1.1 m/s)
→ `T_cell ≈ 30 + 900/(22.5 + 6.84·1.1) ≈ 60 °C`; the same minute at 15 km/h
(4.2 m/s) → `≈ 48 °C`. **Twelve degrees of cell temperature, ~3.5 % of output,
purely from wind.**

> **Why not NMOT?** The older model, `T_cell = T_air + (POA/800)×(NMOT−20)`, has no
> wind term at all — it bakes in one fixed cooling rate. During the windless
> heatwave of late July 2026 (wind fell from 10.4 to 3.9 km/h over a week) it
> steadily under‑predicted cell temperature, and the expected curve drifted too
> high. Switching to Faiman removed about half of that drift. NMOT is still used
> as an automatic fallback when a weather source returns no wind data, which is
> what `--nmot` now controls; `expected["wind_used"]` reports which model ran, and
> `pv_day.py --expected` prints the peak cell temperature and the model behind it.

**`η_inv` — inverter conversion loss (≈ 0.97).**
The Growatt converts DC→AC at ~97 %. This matters because the Shelly measures the
**AC** output (after the inverter).

**`η_sys` — everything else, lumped (≈ 0.95).**
A single catch‑all (~5 %) for DC cable resistance, module mismatch, soiling, and
reflection/angle‑of‑incidence losses. (For reference, the industry PVWatts tool
assumes ~14 % *total* losses; here temperature and the inverter are broken out
separately, so this bucket only holds the remainder.)

**`min(…, inverter_AC_cap)` — clipping.**
The inverter can't output more than **3000 W** AC, so the expected curve is capped
there. (In practice this array peaks around ~2200 W, so clipping never bites — but
it's modelled for correctness.)

### From power to the daily numbers

Integrating the per‑minute series over the day (1 sample = 1 minute, so divide by
60 for Wh and by 1000 for kWh):

```
expected_kWh  = Σ expected_W(t) / 60 / 1000
POA_insolation H (kWh/m²) = Σ POA(t) / 60 / 1000
Performance Ratio  PR = actual_kWh / (kWp × H)          sun only
Performance Index  PI = actual_kWh / expected_kWh       sun + heat + wind + losses
kWh lost = expected_kWh − actual_kWh
```

Note both are integrals of *different* curves: PR divides by the area under the
**weather** curve, PI by the area under the **expected‑power** curve. The heat
factor sits *inside* the second sum and cannot be pulled out of it, because the
panels are hottest exactly when the sun is strongest — using the day's average
heat factor instead overstates expected output by ~2 %.

`pv_trend.py` computes the same quantities restricted to the **afternoon window**
(default from 14:00) to isolate the shading — using per‑minute POA exposed by the
model — giving an *afternoon PR* alongside the whole‑day PR.

### Worked example (one clear midday minute)

`POA = 900 W/m²`, air `30 °C`, wind `4 km/h` (1.1 m/s) → `T_cell ≈ 60 °C`:

```
expected_W = 3.0 × 900 × [1 − 0.0029·(60 − 25)] × 0.97 × 0.95
           = 3.0 × 900 × 0.899 × 0.97 × 0.95
           ≈ 2237 W
```

That's an implied instantaneous conversion of ~2.5 W per W/m² — a healthy value
for a 3 kWp array. If actual production that minute were only 500 W, the model
says **~1760 W was lost** to shading at that instant.

### What is deliberately *not* in the formula

- **No shading term.** That's the whole point — shading reveals itself as the gap
  between this curve and real production. Bake it in and you'd hide what you're
  hunting for.
- **No bifacial rear‑side gain.** The panels are bifacial, but rear irradiance is
  hard to predict, so it's omitted. Consequence: the expected curve is slightly
  **conservative**, and real production can occasionally edge *above* it — that's
  expected, not a fault.

So the expected line is a physics‑based *"what a healthy version of your specific
system should make this minute,"* built entirely from your two datasheets plus
live irradiance and temperature — no per‑day curve fitting.

---

## Performance Ratio & the fairness check

**Performance Ratio (PR)** = actual energy ÷ (nameplate × available insolation).
It divides out *how sunny the day was*, so it's the fair way to compare days: a
cloudy day and a clear day can have the same PR if the system behaved the same.
Typical healthy residential PR is ~0.75–0.85.

**PR normalises sunlight only — not temperature.** This trips people up, so be
explicit about it:

```
PR = actual_kWh / (kWp × POA_insolation)
```

The denominator is *cloud‑affected* POA, so **clouds are divided out**: an overcast
day shrinks both the numerator and the denominator and PR barely moves. But there
is no temperature term anywhere in that formula, so **heat is not divided out**.
Cell temperature is weather too, and a heatwave drags PR down on a perfectly
healthy array. Late July 2026 is the worked example: PR fell 68 % → 64 % over a
week in which the array was fine and the air went from 27 °C to 34 °C.

Nor does PR account for wind, spectrum, soiling, or panel ageing — they all land
in the same bucket.

### Performance Index (PI) — the temperature‑aware companion

**PI = actual ÷ expected.** Same numerator as PR, but the denominator is the
integral of the whole expected curve, so sun *and* heat *and* wind *and* the
modelled losses are all divided out. 100 % means "exactly what the physics says";
all three scripts print it, and `pv_trend.py` plots it.

The two are related by an exact identity — no new measurement is involved:

```
PI = PR / ( heat_factor × η_inv × η_sys )
```

So `PR/PI` **is** the model's claimed explanation, made explicit. Over the late‑July
2026 heatwave it slid 0.869 → 0.836, i.e. the model claiming "it got hotter."

| question | use |
|---|---|
| Is the array healthy *today*, heat and wind accounted for? | **PI** ← the headline |
| What did the meter and the sun actually do, with no model in between? | **PR** ← the audit trail |

**Why keep PR when PI is more informative?** Because PI is flat *by construction*
when the model is right, which makes a flat PI weak evidence. If the array lost
3 % and `U0` happened to over‑correct by 3 %, PI would not move and you would see
nothing; PR would show the drop. PR is also immune to model revisions (re‑fit `U0`
and every historical PI changes, every historical PR does not) and is comparable
to the 0.75–0.85 industry benchmark, which PI is not.

Read them together:

| pattern | meaning |
|---|---|
| PR down, PI flat | environmental — a hot or still spell. Nothing to fix. |
| PR and PI both down | something in the system actually changed. |
| both spike on one day | suspect that day's irradiance data, not the array. |

Worked example, 24‑07 → 31‑07 2026: PR fell 3.5 points while PI fell 1.1. The model
claims heat explains 2.4 of them — well supported in direction, with the remainder
inside the noise `U0` allows.

**Sun‑match fairness r** (in `pv_compare.py --expected`) is the Pearson
correlation between the two days' *expected* curves over daytime minutes. A high
r (≥ 0.95, labelled *fair*) means the two days had near‑identical sun, so a
production difference reflects the *system*, not the weather. Lower r →
*some caution* / *weather‑confounded*. (Use `best‑match` rather than a smooth
hourly model here — a smooth model inflates r and can call two genuinely
different days "fair".)

---

## Caveats & limitations

- **Bifacial gain is unmodelled** → the expected line is mildly conservative.
- **Absolute PR carries model uncertainty** — it depends on `--system-eff`, the
  POA model, and (unmodelled) bifacial gain. Trust the *shape* and the
  *before/after difference* more than the absolute percentage; all loss factors
  are CLI‑overridable if you want to tune them.
- **PR ignores temperature** — see
  [Performance Ratio](#performance-ratio--the-fairness-check). Clouds are divided
  out; heat is not. Compare PR only between days of similar temperature, or use
  actual÷expected instead.
- **`U0 = 22.5` is barely constrained by the data** — it was fitted on 7 hot, mostly
  clear days (24–31 July 2026) with no cell‑temperature sensor to check against,
  and it is by far the softest number in the model. Anything in **U0 ≈ 15–32.5**
  fits those days within 5 % of the best residual spread, and leave‑one‑out refits
  swing over **12.5–26.5** — drop a single day and the answer nearly doubles. That
  range is worth ±16 °C of peak cell temperature and ~5 % of expected kWh. The
  underlying reason is that the fitting week spanned only 4–14 km/h of daily‑mean
  wind, and `U1` multiplies wind, so `U0` and `U1` cannot be separated over such a
  narrow range. **Treat `U0` as an order‑of‑magnitude default, not a measurement**:
  trust day‑to‑day *changes* in actual÷expected, not its absolute level. A
  backsheet temperature probe would settle it properly; failing that, re‑fit once
  there are windy and cool days in the record.
- **The irradiance denominator is a *forecast*, not a measurement** — even for
  past days. Open‑Meteo also publishes a satellite‑derived product
  (`satellite_radiation_seamless`, hourly only) which measures the cloud field
  instead of predicting it; on 26‑07‑2026 the two disagreed by 11 % (6.05 vs
  6.71 kWh/m²), which is enough to move a daily PR by ~8 points. Not yet wired in.
- **Partial days** (device started mid‑day, or "today" so far) are flagged and
  excluded from trend averages, but their bars still appear (faded).
- **String voltage note** — 6 panels in one series string sit a little below the
  inverter's full‑power MPP window, and module Imp marginally exceeds the
  inverter's input‑current limit; a minor, non‑fault design quirk that shaves a
  touch off peak harvest.
- **`pv_trend.py` is slow** — it re‑paginates every day from the device on each
  run (no cache yet).
- **ERA5 archive lag** — the reanalysis archive trails real time by ~5 days, so
  very recent days always come from the forecast endpoint.

---

## Repo layout

```
pv_day.py       one day — curve, weather & expected overlays, PR, kWh lost
pv_compare.py   two days — overlay, stats, per-day expected + sun-match fairness
pv_trend.py     all days — 2×2 before/after-optimizer dashboard
README.md       this file
results/         saved charts (if you save there)
```

`pv_day.py` is the library: `pv_compare.py` and `pv_trend.py` import its data
fetch (`fetch_day`), series builder (`build_series`), summary (`summarize`),
irradiance loader (`fetch_irradiance`), and expected‑output model
(`build_expected`). Change the physics once, in `pv_day.py`, and all three stay
in sync.
