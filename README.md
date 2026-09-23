# Wolta for Home Assistant

[![HACS Default](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://hacs.xyz)
[![GitHub release](https://img.shields.io/github/v/release/Seblin01/wolta-homeassistant)](https://github.com/Seblin01/wolta-homeassistant/releases)
[![License: MIT](https://img.shields.io/github/license/Seblin01/wolta-homeassistant)](LICENSE)
[![Validate](https://github.com/Seblin01/wolta-homeassistant/actions/workflows/validate.yml/badge.svg)](https://github.com/Seblin01/wolta-homeassistant/actions/workflows/validate.yml)

Grade how well your home battery is optimised against **day-ahead electricity prices** (Nord Pool / ENTSO-E), directly in Home Assistant. [Wolta](https://wolta.se) analyses your 15-minute battery and grid data and scores how much of your battery's achievable value your operation actually captured. The optimisation grade and measured battery value work across all supported European price zones. Full battery economics (IRR, payback and whole-plant savings) are currently available for Swedish price zones (SE1–SE4).

## What it does

After setup the integration automatically:

1. Uploads 15-minute energy statistics from your HA sensors to Wolta.
2. Triggers periodic recomputes on the Wolta backend.
3. Exposes the results as Home Assistant sensors.

## Sensors

Entity names are translated (English and Swedish bundled; other languages fall back to English), and HA generates entity IDs from the translated names at install time. Default IDs on an English-language instance:

| Entity | Unit | Description |
|--------|------|-------------|
| `sensor.wolta_optimisation_grade` | % | Holistic optimisation score (0–100): the share of the battery's theoretically perfect total value your actual operation captured. Appears as a **preliminary** score after about 7 days of data (server v0.43.0+) and matures at 30 days — the attributes `preliminary` and `n_days` tell you which; a preliminary score can swing from day to day. All price zones. |
| `sensor.wolta_battery_value_per_year` | SEK / EUR | The battery's **own** annual value — the incremental saving vs. the same plant without a battery. Measured from your actual flows (the same number wolta.se shows as "You captured") once the window is **180 days** old (server v0.71.1+); before that it falls back to the modelled battery share — a short window multiplied up to a year is not an honest measured figure (a 32-day summer window has read 55 % high). Attribute `source` tells you which (`measured`/`modelled`); `plant_total_sek` carries the plant total. All price zones. Solar's value is **not** included — it belongs to the solar investment. |
| `sensor.wolta_plant_savings_per_year` | SEK / EUR | Total annual saving of the whole plant (solar + battery), i.e. what a combined solar-plus-battery investment earns. Attributes `battery_sek`/`solar_sek` give the split. SE zones only. |
| `sensor.wolta_internal_rate_of_return_irr` | % | IRR of the **battery investment**: the battery-only savings stream against what you paid for the battery (incremental cash-flow principle). Can be negative — that means the battery alone does not carry its cost. SE zones only. |
| `sensor.wolta_payback_time` | yr | Payback of the battery investment from the battery-only savings stream. `unknown` when the stream never repays the cost within the projection horizon. SE zones only. |
| `sensor.wolta_actual_savings_this_year` | SEK / EUR | Actual battery revenue this year. SE zones only. |
| `sensor.wolta_data_status` | timestamp | Last data point uploaded (diagnostic). Always available. |
| `sensor.wolta_status` | enum | Computation status: `done` / `computing` / `waiting_for_data` / `measuring_battery` / `needs_battery_input` / `error` (displayed translated). The two battery states apply only while the backend is measuring a newly declared battery — see Setup flow. Always available. |

A **Recompute** button lets you trigger an immediate recompute outside the automatic schedule.

Economy sensors that require the decision engine (plant savings, IRR, payback, actual savings) are only available for Swedish price zones (SE1–SE4). The grade and the measured battery value work for all supported zones.

### Why battery value ≠ plant savings

Before v0.5.0 the battery-value sensor showed the whole plant's saving (solar + battery),
which overstated the battery. A battery's value is the *incremental* value versus running
the same plant without it — standard methodology in battery-retrofit economics (NREL
solar-plus-storage analyses, Solcellskollen's Swedish calculations) and investment
appraisal (incremental cash-flow principle). If you want the big number, it is still
there: `sensor.wolta_plant_savings_per_year`, correctly labelled.

## Requirements

- Home Assistant 2025.12.0 or newer
- A battery storage system with energy sensors already configured in Home Assistant's Energy dashboard
- Grid import and export sensors (required); solar production sensor (optional)

## Installation via HACS

Wolta is in the HACS default store, so no custom repository is needed.

One-click (opens HACS on your instance with this repository preloaded):

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Seblin01&repository=wolta-homeassistant&category=integration)

Or from the HACS panel:

1. In Home Assistant, open **HACS** (install it first if needed).
2. Search for **Wolta** and select **Download**.
3. Restart Home Assistant.
4. Go to **Settings** → **Devices & Services** → **Add Integration** and search for **Wolta**.

## Manual installation

1. Copy `custom_components/wolta/` into your HA `config/custom_components/` directory.
2. Restart Home Assistant.
3. Add the integration via **Settings** → **Devices & Services**.

## Setup flow

No account or API token is required. Setup starts with a choice:

- **Create a new profile** — Home Assistant provisions a Wolta profile automatically (the flow below).
- **Link an existing wolta.se profile** — already used wolta.se? Paste your personal profile link (the one with `?profile=…`) or just the token. Your plant parameters are read from the server, the profile is adopted for integration use (backend v0.19.0+), and the plant step is skipped; Home Assistant takes over data uploads from there (overlapping periods are overwritten with sensor data). The profile must include a battery — solar-only profiles are rejected. (Pasting the read link from your device's Visit button here doesn't work — it can't authenticate a new setup; get a profile link or token from the plant page on wolta.se instead.)

  If the plant already streams its data to wolta.se through another connection (the Sonnen
  webhook or a Reduxi bridge), the flow offers **view-only mode** instead: you get all the
  sensors and the recompute button, but no sensor selection and no uploads — the plant's own
  connection keeps owning the data, and removing the entry never deletes anything on
  wolta.se. If the saved token ever stops working, re-authentication simply asks for a fresh
  token (create one on the plant page on wolta.se while signed in).

**Step 1 – Energy sensors**
- Map your HA energy sensors for battery charge, battery discharge, grid import and grid export (prefilled from the Energy dashboard). Solar is optional.

**Step 2 – Price zone and control system (create path)**
- Pick your price zone. Nothing is preselected: the zone suggested from your Home Assistant location is listed first and labelled, but you have to choose it — the zone cannot be changed after the plant is created.
- Confirm the control system (prefilled when Wolta recognises the integration behind your battery sensors — for a Riemann-sum helper, the integration behind its source sensor) and check the charge/discharge direction toggle (preselected when your history looks reversed). The form states plainly what setup means: your 15-minute energy data is stored at Wolta, your anonymised optimisation grade is included in the public comparison, and removing the integration deletes all of it — see [Privacy](#privacy). There is no separate checkbox for any of this; it isn't optional.
- Submit. **You are not asked for battery capacity, power or efficiency**: Wolta measures them from the data the integration uploads. The status sensor shows *Measuring battery* until then (typically 30 days with a few full charges). If the data cannot yield them — the battery never charges fully, or one stream is empty — a Repair asks for the nameplate values instead; you can also enter them any time under Configure → Settings.

## Shared profile with wolta.se

The integration and wolta.se show the **same profile**. The server is the source of truth: change a value in the Configure dialog and it shows up on wolta.se; change it on wolta.se and Home Assistant picks it up within about five minutes (a lightweight side-poll watches the profile and refreshes the results as soon as something changed) — the Configure form always opens with the current server values. Last write wins.

Because a linked profile belongs to your wolta.se usage, **removing the integration never deletes a linked profile** (profiles created by the integration keep the documented delete-on-remove behaviour, see Privacy). A purged/deleted profile triggers re-authentication, which creates a fresh HA-owned profile from the cached settings.

## Adjusting values afterwards

Open the integration's **Configure** dialog (Settings → Devices & Services → Wolta → Configure) and choose **Settings** to adjust values without removing the integration. (The other menu options — **Link to a wolta.se account** and **Correct the price zone** — are covered under [Account linking](#account-linking) and [Correcting the price zone](#correcting-the-price-zone) below.) The form is grouped into **Battery**, **Economy** and **Tariffs** sections:

- Battery capacity (kWh), power (kW) and round-trip efficiency — changing these triggers a server-side regrade of your optimisation score. While Wolta is still measuring a newly created battery (status sensor shows *Measuring battery* or *Needs battery values*), **capacity and power** are blank and optional rather than required, so saving other settings doesn't overwrite the pending measurement with a guess. (Efficiency stays a required field — it is never measured from scratch, so it is always prefilled.)
- Nameplate capacity (kWh) and nameplate power (kW) — optional manufacturer-rated figures. The grade itself always uses the usable/deliverable values above; the rated figures let wolta.se compare per-kWh prices fairly in the expansion calculator and explain measured-vs-rated differences. Filling in **Nameplate capacity (kWh)** and **Nameplate power (kW)** has the same effect as the Repair: Wolta derives the usable capacity from them right away, and switches to the measured value once the data allows. Clearing a field removes the value.
- What you paid for the battery, and the purchase date — used for IRR, payback and this year's actual savings. Enter the invoice total, after any green deduction; it is treated as the whole investment. If Home Assistant's sensor history suggests a purchase date, it's offered as a one-time suggestion here rather than being set automatically — accept it, type your own, or clear it if you don't know the date. Clearing a field removes the value.
- Your own tariff — grid fee, electricity supplier markup and an export premium/discount (öre/kWh for SEK zones, euro cents/kWh for other zones; the export figure may be negative). Clearing a field reverts it to your country's standard tariff.
- Reserve floor (%) — the share of usable capacity your control system never discharges below. Clearing the field removes the reserve.
- **Battery charge/discharge reversed** — a toggle that swaps the battery charge and discharge streams on upload. See Troubleshooting below.

Only changed fields are sent to Wolta. After saving, a recompute is triggered automatically. The optimisation grade updates first; the economy figures (IRR, payback, actual savings) are recomputed in the background and follow a few minutes later. Throughout, the sensors keep their previous values instead of dropping to `unavailable` (v0.7.1+).

### Correcting the price zone

The price zone is chosen when the plant is created and normally stays put — it decides which day-ahead price series your grade and your economics are measured against. If it was set wrong, open **Configure → Correct the price zone** (v0.32.0+, requires server api 0.80.0+).

Only zones in the same country are offered. The amounts you have entered — what you paid, your grid fee, your supplier markup — are expressed in the zone's currency and are **not** converted, so a move across a currency boundary would silently reinterpret e.g. 100,000 SEK as 100,000 EUR. For that case the answer is still to delete the plant and set it up again.

A correction recalculates the grade and the economics against the new price series on your whole stored history, so **your figures will change**. The integration updates its own stored zone at the same time; nothing else needs doing.

**Changing energy sensors:** use the **Reconfigure** option (Settings → Devices & Services → Wolta → three-dot menu → Reconfigure) to pick new sensors. The full history is re-uploaded from the new sensors and the grade recomputed — no need to remove the integration.

## External control (Grid Rewards and similar flex-market services)

Some services take temporary direct control of your battery in exchange for compensation — Tibber's Grid Rewards is one example, and other flex-market programs work the same way. While such a service is dispatching your battery, the household isn't making its own price decisions, so those intervals shouldn't be judged as good or bad price calls in the optimisation grade. They are **not** cut out of the calculation: they are bound to your measured operation, so they contribute equally to both sides of the grade's ratio and neither raise nor lower your score. Removing them outright would break the battery's state-of-charge chain between intervals, since what the battery holds carries over from one interval to the next.

**What the picker does.** Both the setup flow's entity step and the **Reconfigure** option afterwards (Settings → Devices & Services → Wolta → three-dot menu → Reconfigure) offer an optional **External control active** field: point it at a `binary_sensor` that is `on` for exactly as long as a flex service has control of the battery. The integration reads that sensor's Home Assistant state history and marks the corresponding 15-minute intervals so the backend can neutralise them. Leaving the field empty — the default — changes nothing: no extra sensor is read, and every interval is graded exactly as it was before this feature existed.

**Where to get the sensor (Tibber Grid Rewards).** Tibber's official APIs do not expose
Grid Rewards at all — neither the GraphQL API nor the newer Data API has a field for it, so
there is nothing for Wolta to read directly. The community integration
[`JohNan/homeassistant-tibber_grid_rewards`](https://github.com/JohNan/homeassistant-tibber_grid_rewards)
fills the gap: it creates a **Grid Reward Active** binary sensor that is `on` for exactly as
long as Tibber reports the `GridRewardDelivering` state. Point the picker straight at it — no
template sensor needed.

Two things worth knowing before you rely on it. It talks to Tibber's *app* API (the one behind
the phone app, using your email and password) rather than an official, documented one, so it
can stop working without warning if Tibber changes something. And it reports the present
moment, not the past: Wolta reads the sensor's recorder history, so the flag only covers the
period since you installed that integration — dispatch sessions from before it was running
stay unflagged.

For other flex services the picker is vendor-neutral: any `binary_sensor` that is `on` while
the service controls your battery works, including a
[template sensor](https://www.home-assistant.io/integrations/template/) derived from whatever
state sensor your provider's integration offers.

**How far back flagging reaches.** Your energy data (battery, grid, solar) is backfilled a full year regardless of this setting. The *flag* is different: it comes from Home Assistant's recorder **state history**, whose retention is governed by `recorder.purge_keep_days` (10 days by default) — far shorter than the long-term statistics your energy data is read from.

Beyond that retention window there are no recorded state changes, so Home Assistant answers with the last state it still knows about and holds it forward. Which way that falls depends on what that state was. If it was `off` — the usual case for a sensor that spends most of its time off — nothing older than the retention window is flagged, and those energy rows upload unflagged even if the flex service was in fact active back then. If it happened to be `on`, the flag is held forward from there until the next recorded change, which can reach much further back than `purge_keep_days`.

Either way, the flag only ever *marks* intervals that were going to be uploaded anyway — it never creates one. An interval is uploaded because your **battery** sensors have recorded statistics covering it (hourly for older data, which is split across that hour's four quarters; 5-minute for recent data), and the flag is then applied to it. So a stale `on` state cannot turn a stretch where the recorder has nothing — while Home Assistant was down, or before the battery was installed — into a wall of empty "externally controlled" intervals. From the first upload onwards, flagging keeps pace with every new upload as long as the sensor stays configured.

**Known limitation: standby/reserve periods.** Flex-market programs typically hold a slice of capacity in reserve between active dispatch sessions, ready to call on the battery again. If your binary sensor only reports `on` during an actual dispatch — not during the standing-by periods around it — those idle stretches are **not** flagged, even though the reserved capacity was unavailable for your own price optimisation and still affects the grade. There is no way to tell "resting because the price is bad" apart from "resting because a flex program has it reserved" unless the sensor itself reflects the reservation.

**A practical pattern:** most flex-market services expose their own status entity (a text/enum sensor whose value is something like `GridRewardDelivering` while dispatching). Rather than hunting for a ready-made `binary_sensor`, create a [template binary sensor](https://www.home-assistant.io/integrations/template/) derived from it and point the picker there, for example:

```yaml
template:
  - binary_sensor:
      - name: "Flex control active"
        state: "{{ states('sensor.your_control_status_sensor') == 'GridRewardDelivering' }}"
```

Adjust the source entity and state value to whatever your own integration exposes. If that same source entity also distinguishes standby/reserve from active dispatch, folding those states into the template narrows the limitation above.

### What the flex participation paid you

Excluding the flagged intervals keeps the grade fair, but it says nothing about the other half of the trade: the compensation the service pays you for that control. Wolta can show the two side by side — what the participation earned, next to what it cost you in foregone spot value — but only if it knows the amounts.

**The picker.** The same two places (setup flow's entity step and **Reconfigure**) offer an optional **Flex compensation, monthly** field. Point it at a sensor holding the compensation in SEK — CheckWatt, Tibber Grid Rewards and similar services usually expose one, and a [template sensor](https://www.home-assistant.io/integrations/template/) works just as well. Wolta reads the monthly total from long-term statistics rather than from the current state, so a sensor that resets at the start of each month is handled correctly.

**Which sensor to pick.** Two integrations already expose something suitable:

- **CheckWatt** — [`faanskit/ha-checkwatt`](https://github.com/faanskit/ha-checkwatt) publishes *Daily Net Income* and *Annual Net Income* (net after CheckWatt's and the installer's shares, in SEK). Either works: Wolta reads the *increase over the month*, so a daily sensor that resets every day and a yearly one that resets every year both add up to the same monthly figure.
- **Tibber Grid Rewards** — the reward for the current month comes from Tibber's app API; [`JohNan/homeassistant-tibber_grid_rewards`](https://github.com/JohNan/homeassistant-tibber_grid_rewards) exposes it as *Grid Reward Current Month*, which is exactly the figure Wolta wants.

Whichever you pick, open **Developer tools → Statistics** and confirm the entity is listed there with a *sum* — that is the check that matters, not what the integration is called. A sensor missing from that list, or listed without a sum, is the `state_class` problem below.

**The sensor must have `state_class: total` or `total_increasing`.** "Has long-term statistics" is not enough: a `state_class: measurement` sensor gets long-term statistics too, but only min/mean/max — there is no monthly sum to read, and Wolta would have nothing to send. If the sensor you pick yields nothing for about a day, a repair notice appears telling you so; it disappears on its own once a figure comes through or you clear the field. Wolta never invents a zero for a month it could not measure — showing "0 kr compensation" beside a real foregone-spot cost would make flex participation look like a pure loss on a number nobody measured. A month whose total comes out NEGATIVE is skipped for a different reason: Wolta stores compensation of zero or more and refuses the whole update otherwise, so sending one would stop every other month from getting through.

Once a cycle the integration reads the current month and the previous one and sends those two figures on. Last month is re-sent every cycle on purpose: aggregators often settle a month days after it ended, and an estimate that was frozen on first reading would stay wrong.

**Only the current and previous month are read.** Home Assistant keeps long-term statistics indefinitely — they survive `purge_keep_days`, which only trims state history and the short-term statistics — but they start the day your compensation sensor started recording them. Wolta therefore reads two months and no further back: everything before that (and anything from before you installed the sensor) has to be entered by hand in the compensation card on wolta.se, where it is kept as a `manual` amount.

**It never touches what you entered yourself — and the card never touches its months.** Amounts you type into the compensation card on wolta.se are kept separately from the ones the sensor reports, and the integration only ever writes its own. The separation runs both ways: the card lists the sensor's months but cannot change them, so those rows have both fields locked and no Remove button. Clearing the picker stops the reading; the months already recorded stay where they are, and deleting the plant on wolta.se is the only thing that removes them.

Leaving the field empty — the default — changes nothing: no extra sensor is read and nothing is sent.

### What the figures do, and what they never do

**The amounts never move your payback time or IRR.** They are shown next to the foregone spot value so you can see both halves of the flex trade, and that is all they do. Wolta's economy figures are deliberately computed without them: the compensation is paid outside the electricity bill, on a contract you can leave at any time, and reported per plant by the aggregator rather than measured by Wolta. Folding it into the return on the battery would make the investment case depend on a number nobody here can verify. The optimisation grade doesn't use them either — that side of the trade is handled by neutralising the flagged intervals, described above.

**On the grade sensor: the `flex_compensation` attribute (v0.34.0+).** When the backend has something to say about compensation, `sensor.wolta_optimisation_grade` carries it as a structured attribute so you can build your own cards and automations on the figures:

| Key | Meaning |
| --- | --- |
| `compensation_period_sek` | What the recorded amounts add up to over the grade's period. `null` when no amounts overlap it. |
| `compensation_coverage` | The share of the period those amounts actually cover — one entered CheckWatt month against a three-year window is `0.03`, not a small payout. |
| `estimate_period_sek` | `{low, high}`: what participation would plausibly have paid over the same period, from the aggregator's capacity prices and your bid size. A band, not a prediction. |
| `estimate_coverage` | The share of the period the estimate could be computed for (missing price hours lower it). |
| `source` | `manual` (you entered it on wolta.se), `sensor` (read from the picker above) or `estimate` (nothing recorded — the band is all there is). |
| `aggregator` | The flex provider the estimate assumes. |
| `bid_kw` | The bid size used, either yours or one derived from the battery's nameplate figures. |

**Read the two coverage ratios before comparing the two amounts.** They have different bases on purpose: the estimate spans the whole grade window, the recorded amount only the months that exist. Putting a one-month payout next to a three-year estimate as if they were the same quantity is the mistake the ratios are there to prevent. The whole block is absent — not `null` — when there is nothing to say: no amounts, no aggregator, no bid, or a grade computed before this feature shipped.

## Troubleshooting

**Optimisation grade is strongly negative or looks inverted.** This almost always means the battery charge and discharge streams are mapped the wrong way round — some battery integrations and energy meters report the two directions in a way Wolta reads reversed, which makes it look as though the battery charges when power is expensive and discharges when it is cheap. Open the **Configure** dialog and turn on **Battery charge/discharge reversed**. Wolta swaps the two streams, re-reads your history and recomputes the grade automatically — you don't need to change any of your Home Assistant sensors. If the grade still looks wrong afterwards, please [open an issue](https://github.com/Seblin01/wolta-homeassistant/issues).

**Payback time shows `unknown`.** The sensor has no payback year to report because the battery doesn't reach break-even — typically a small or expensive battery whose modelled internal rate of return (IRR) is negative. This is a real result, not an error; the IRR sensor shows the (negative) return. Increasing the battery capacity or lowering the entered purchase price moves it towards a payback.

**The grade sensor has a `capacity_hint` attribute (v0.8.0+).** The backend sets it when the battery capacity you entered is clearly higher than the usable capacity your measured data shows — a nameplate-vs-usable mix-up. The grade compares you against a perfect dispatch on the capacity you *entered*, so an oversized entry unfairly lowers the score. Open **Configure** and set the battery capacity to the attribute's `suggested_kwh` (or your own better estimate of usable capacity); the grade is recomputed automatically. No hint means no mismatch was detected.

**"Wolta needs your battery's nameplate values" repair appears (v0.36.0+).** On a newly created plant, Wolta measures battery capacity, power and efficiency from the data you upload instead of asking for them at setup — the status sensor shows *Measuring battery* while that's in progress. If the data can't yield a measurement after enough time has passed — the battery never charges fully, or one of the battery streams is empty — this repair fires and asks for the manufacturer's nameplate capacity and power instead. Wolta derives the usable capacity from the nameplate pair and switches to the measured value later if the data allows it. You can also enter the nameplate values proactively at any time under Configure → Settings, without waiting for the repair.

**Measured-parameter repairs appear (v0.12.0+).** Once there are a couple of months of history, Wolta measures the grade-affecting battery parameters at the meter and, if one clearly disagrees with what you configured, raises a fixable repair. They only appear when the measurement is mature and confident (≥60 days and a real gap), and the measured values are all-time figures, so they're stable across seasons. Each repair opens on a menu: **adopt the measured value** or **ignore and keep your value** (v0.26.0+) — ignoring is remembered so the repair stops reappearing, which is the right choice if you simply haven't fully cycled the battery in the period, or if a spiky sensor has biased the measurement.

- **Usable capacity** — the AC/meter-side usable capacity (not the DC/cell figure your battery app shows). Adopting sets the battery capacity and **clears any reserve floor**: the measurement already reflects only the window you actually cycle, so the reserve must not be subtracted again.
- **Peak power** — the most power the battery has charged/discharged at the meter. This is a *lower bound* (your control may never have demanded full power), so adopting opens an editable field pre-filled with the measured value — set the battery's real inverter limit. Too low a value flatters the grade; too high unfairly lowers it. If a spiky sensor reports a peak above what your hardware can physically deliver (more than 50 % over your declared/nameplate power), the repair is suppressed automatically rather than nagging you to raise a correct value.
- **Round-trip efficiency** — the measured AC energy-out ÷ energy-in, a wall-to-wall figure lower than the battery's DC/cell spec. One-click adopt; corrects a stale efficiency set from thin history at setup. Because a spurious sensor jump inflates the charged sum and biases this *low*, and there's no clean physical floor to auto-suppress that, the honest choice is to adopt it or ignore it.

**The grade sensor exposes the measured parameters as attributes (v0.23.0+).** The same three measurements are available directly on the grade sensor for dashboards and template sensors: `measured_capacity_kwh`, `measured_power_kw` and `measured_efficiency` (a fraction), each with a companion `measured_*_status` attribute — `ok`, `immature` (data hasn't reached the measurement's maturity threshold yet) or `unmeasurable` (impossible with your sensor setup, e.g. a net-metering sensor that collapses the battery flow). These are measured lower bounds, not nameplate specs. An absent value simply omits its attribute; the status says why.

## Full results on wolta.se

The Wolta device page has a **Visit** link that opens your plant on wolta.se, using a read-only link minted automatically for this installation (backend v0.79.0+; existing entries mint one at their next setup). It shows the full plant view — grade breakdown, economy drill-downs, history — but the link itself writes nothing: it cannot edit economy, tariff or grade-window fields, delete the plant, mint a new link, share the plant with anyone else, or edit technical fields (sensors, battery capacity/power/efficiency). The link sits in `configuration_url`, which travels in every device-registry export people attach to GitHub issues or forum posts, so it is deliberately unable to change anything, even the fields it lets you view. Anyone with access to your Home Assistant can follow the link and see everything it shows. For any editing on wolta.se, see [Account linking](#account-linking) below.

## Account linking

The read link above covers viewing your results — grade breakdown, economy drill-downs, history — but not changing them. Editing anything on wolta.se (economy, tariff, grade window, technical fields, deleting the plant, sharing, rotating the link) requires linking the plant to a wolta.se account — it is the only way to change a Home-Assistant-created plant from wolta.se.

Open the integration's **Configure** dialog (Settings → Devices & Services → Wolta → Configure) and choose **Link to a wolta.se account** instead of **Settings**. The flow mints a one-time linking code, shown on screen. Sign in or create an account at wolta.se, go to **Account → Link plant**, and enter the code there.

The code is valid for 10 minutes and can be used once. If it expires before you enter it, or you've already used it, open the menu again to mint a fresh one.

## Privacy

Your 15-minute energy data is stored on Wolta's servers to power the analysis. No personal data (name, address, account) is sent or required.

**Deleting the integration removes your data server-side** — for profiles the integration created. Removing the config entry in Home Assistant then triggers a right-to-erasure request to the Wolta backend. **Linked profiles are exempt:** removing the integration only disconnects Home Assistant; your wolta.se profile and history stay. Delete those from wolta.se itself.

**Every request identifies the integration and its version** (since v0.37.3), as
`wolta-hacs/0.37.4 HomeAssistant/2026.9.3 aiohttp/3.14.3 Python/3.14` in the User-Agent —
the second half is what Home Assistant already sends to every server it talks to. This is
what makes it possible to keep older installations working, or to tell you plainly when
yours has fallen too far behind, instead of leaving you with a bare `405`. It names
software versions and nothing else: no identifier for you, your plant, your tokens or
your data.

Your anonymised optimisation grade is always included in the public corpus comparison — there is no checkbox to opt out of it, and there never actually was one that did anything: the setting used to sit in the plant step, but it did not gate corpus membership or the raw-data retention described above, and it only had one real effect — disabling the expansion calculator (see [v0.37.0 release notes](docs/release-notes-v0.37.0.md)). See [wolta.se/om](https://wolta.se/om) for the full privacy policy.

## Links

- [wolta.se](https://wolta.se) – the service
- [wolta.se/om](https://wolta.se/om) – about & privacy
- [GitHub issues](https://github.com/Seblin01/wolta-homeassistant/issues) – bug reports & feature requests

## Example dashboard

A ready-made Lovelace view for the Wolta sensors is available in `dashboards/wolta.yaml` (English entity IDs and labels — the default for most instances). A Swedish variant with Swedish entity IDs and labels is available in `dashboards/wolta.sv.yaml`.

### Using the dashboard

1. Open `dashboards/wolta.yaml` and copy the entire contents.
2. In Home Assistant go to **Settings → Dashboards**.
3. Create a new dashboard (type: Lovelace) and choose **Edit manually**.
4. Paste the YAML content and save.

### Verify entity IDs

Entity names follow your Home Assistant language (English and Swedish translations are bundled; other languages fall back to English). HA generates entity IDs from the translated names at install time. The dashboard YAML uses the **English** defaults; on a **Swedish** HA instance, replace them as follows:

| English ID (used in the YAML) | Swedish instance ID |
|-------------------------------|---------------------|
| `sensor.wolta_optimisation_grade` | `sensor.wolta_optimeringsbetyg` |
| `sensor.wolta_battery_value_per_year` | `sensor.wolta_batterivarde_per_ar` |
| `sensor.wolta_internal_rate_of_return_irr` | `sensor.wolta_intern_avkastning_irr` |
| `sensor.wolta_payback_time` | `sensor.wolta_aterbetalningstid` |
| `sensor.wolta_actual_savings_this_year` | `sensor.wolta_facit_i_ar` |
| `sensor.wolta_data_status` | `sensor.wolta_datastatus` |
| `sensor.wolta_status` | `sensor.wolta_status` |
| `button.wolta_recompute` | `button.wolta_rakna_om` |

If your entities differ (other language, integrations installed before v0.4.3, or a `_2` suffix when installed more than once): go to **Settings → Devices & Services → Wolta** to see the exact IDs, and adjust the dashboard YAML accordingly.

### Notes

The dashboard ends with a markdown card linking to your full results on wolta.se. The link is resolved dynamically from the device's `configuration_url` (v0.4.1+; a read-scoped link since v0.30.0 — see [Full results on wolta.se](#full-results-on-woltase)), so no manual token pasting and no dashboard change are needed.

Economy sensors (battery value, IRR, payback, actual savings) are only available for Swedish price zones (SE1–SE4). They show `unavailable` for other zones.

Most sensors show `unavailable` until the first recompute run completes, which requires at least 7 days of uploaded data (the preliminary optimisation grade, server v0.43.0+). The economy sensors (battery value, IRR, payback, actual savings) need more: a mature grade (30 days) for IRR/payback/actual savings, and a 180-day window before the battery-value sensor shows a measured figure instead of its modelled fallback.

During a recompute (e.g. after changing values), sensors keep their last known values instead of flickering to `unavailable`; retained attributes carry a `computing: true` flag and `sensor.wolta_status` shows **Computing** (`computing`) until the new results land (v0.4.2+). The grade is recomputed first and the economy figures (IRR, payback, actual savings) follow a few minutes later; they hold their previous value in the meantime rather than going `unavailable` (v0.7.1+).

## License

MIT – see [LICENSE](LICENSE).
