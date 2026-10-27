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

**Only complete quarters are uploaded.** A quarter is sent only when every selected
sensor has its own value for it in Home Assistant's statistics, and never the first
period after a gap: when a sensor has been unavailable, Home Assistant puts the whole
gap's energy into the first period after it, which is not a measurement of that
period. A missing period counts as a zero only when that is known: for a solar sensor
while the sun is down at your Home Assistant location (many inverters report nothing at
night), and for any sensor whose counter has not moved across the gap. If a selected
sensor stops producing statistics altogether, uploading pauses
and a repair names the sensor. Periods when Home Assistant itself was not running are
still sent (Home Assistant records them as zero), and Wolta's server sets them aside.
A reconfiguration does not upload your whole history again: the integration sends only
the changed sensors' values for the affected range, and the server corrects what it
already stores (see [Correcting stored history](#correcting-stored-history)). Inside that
range, gaps of up to 6 hours where the new sensor has no complete statistics are left
empty; longer silences, and everything outside the range, keep their earlier values.

## Sensors

Entity names are translated (English and Swedish bundled; other languages fall back to English), and HA generates entity IDs from the translated names at install time. Default IDs on an English-language instance:

| Entity | Unit | Description |
|--------|------|-------------|
| `sensor.wolta_optimisation_grade` | % | Holistic optimisation score (0–100): the share of the battery's theoretically perfect total value your actual operation captured. Appears as a **preliminary** score after about 7 days of data (server v0.43.0+) and matures at 30 days — the attributes `preliminary` and `n_days` tell you which; a preliminary score can swing from day to day. If you have set a grade window on wolta.se, the sensor shows the grade for that window instead of the comparison grade. The attribute `basis` says which one (`window` or `comparison`), and `period_start` and `period_end` are the dates of the grade shown. Details that describe the comparison grade (`peer_percentile`, `peer_n`, `benchmark`, `annual_basis`, `measured_period_sek`, `measured_wear_sek`, `price_skill`, `components`, `external_control`) are on this sensor only while it shows the comparison grade; with a window set they are on `sensor.wolta_comparison_grade`. The attribute `benchmark` (`method`, `comparable`, `reasons`) says whether the comparison grade is compared with other installations or is a grade of your own, and why (for example `no_soc`: no state-of-charge sensor; see *State of charge (SoC) monitoring*). It is present once the server computes grades against the data-driven benchmark. All price zones. |
| `sensor.wolta_comparison_grade` | % | The grade over the last 12 full months, the one that is compared with other installations, always the same grade whether or not you have set a grade window. Attributes `peer_percentile` and `peer_n` give your percentile among comparable installations and how many there are; `period_start` and `period_end` are the dates it covers; `benchmark` says whether the grade is compared at all (and why not), next to `annual_basis`, `measured_period_sek`, `measured_wear_sek`, `price_skill`, `components` and `external_control`. |
| `sensor.wolta_battery_value_per_year` | SEK / EUR | The battery's **own** annual value — the incremental saving vs. the same plant without a battery. Measured from your actual flows once the window is **180 days** old (server v0.71.1+); before that it falls back to the modelled battery share — a short window multiplied up to a year is not an honest measured figure (a 32-day summer window has read 55 % high). Attribute `source` tells you which (`measured`/`modelled`); `plant_total_sek` carries the plant total. It is the same measured figure Wolta's economics (payback, IRR) are anchored on. Once the grade is computed against the data-driven benchmark, the grade can cover a different period than the economics, so this figure can differ from what wolta.se shows as "You captured" next to the grade. All price zones. Solar's value is **not** included — it belongs to the solar investment. |
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

## Spot price sensors

Two extra sensors show the day-ahead spot price (a third one, [*Purchase price*](#purchase-price-sensor), shows what you actually pay, including VAT, and [*Sale price*](#sale-price-and-forecast-price-sensors) what you are paid for sold electricity). They exist for the **Swedish price zones SE1–SE4 only**, because the prices are converted to SEK with Swedish VAT; for any other zone they are not created and the integration makes no price requests. They are information only: they have no connection to the optimisation grade, the economics or the data the integration uploads, and a problem with the price endpoint only makes the spot price sensors (and the purchase and sale price sensors, which share their poll) unavailable. The forecast never feeds the grade or any of the economy figures.

| Entity | Enabled by default | Description |
|--------|--------------------|-------------|
| `sensor.wolta_spot_price` | Yes | The spot price of the current quarter-hour, in SEK/kWh **excluding VAT**. The state is worked out locally from the `prices` list and flips on the quarter-hour without waiting for the next poll; it is `unavailable` when the data holds no price for the current quarter. The attribute `prices` is a list of `{start, price}` points (UTC start time, SEK/kWh excluding VAT) for today and, once it is published, tomorrow. `vat_factor` is the Swedish VAT multiplier (1.25, i.e. 25 %), so multiply a price by it for a VAT-inclusive figure. `tomorrow_complete` says whether tomorrow's prices are all published. |
| `sensor.wolta_spot_price_with_forecast` | **No** | The same state, but `prices` also carries Wolta's forecast for up to three days ahead: the quarters of tomorrow that have no published price yet, and the two days after tomorrow. Each point has `forecast: true` or `false`, and forecast points also have `horizon` (1, 2 or 3). The extra attributes `forecast_issued_at` and `forecast_accuracy` describe tomorrow's forecast, and `forecast_days` lists the issue time and accuracy of days 2 and 3. |

**Enable the forecast sensor** under *Settings → Devices & services → Wolta → entities*: open the disabled *Spot price with forecast* entity and switch it on. The two sensors poll the public Wolta price endpoint every 15 minutes and need no account or token. They are unavailable until the first data has arrived, and while Wolta has no exchange rate to convert the prices to SEK. The zone is read when the integration is set up: if you change it with *Correct the price zone* between a Swedish and a non-Swedish zone, the price sensors become unavailable until the integration is reloaded (or Home Assistant restarted); after a reload they are created, or not, to match the new zone.

**The forecast covers up to three days ahead.** `horizon` says how far ahead a forecast point is: `1` is tomorrow, `2` the day after tomorrow and `3` the day after that. Once the day-ahead prices for tomorrow are out (normally around 13:00), the published prices replace tomorrow's forecast, so forecast points never overlap real prices; from then on the forecast starts at the following day. The list may hold fewer than three forecast days, and when no forecast is available the forecast sensor has no forecast points and behaves like the plain spot price sensor. The points are in time order and no timestamp appears twice. The `horizon` values follow the server (currently 1 to 3). If a template or automation treats every `forecast: true` point as tomorrow, as was true before v0.52.0, filter on `horizon == 1`.

**The further ahead, the less certain the forecast is.** A forecast two or three days out depends on weather forecasts that are further out, so expect it to be worse than tomorrow's; use it to see the rough shape of the coming days (a windy, cheap day or a still, expensive one), not to plan around single quarters. Each day has its own issue time and accuracy. `forecast_issued_at` and `forecast_accuracy` (explained below) describe tomorrow's forecast, and the attribute `forecast_days` holds the same for the later days, without the points (those are in `prices`):

```yaml
forecast_days:
  - date: "2026-10-28"
    horizon: 2
    issued_at: "2026-10-26T09:05:00Z"
    accuracy: { basis: upstream, days: 0, window_days: 30, upstream_mae_ore_kwh: 25 }
  - date: "2026-10-29"
    horizon: 3
    issued_at: "2026-10-26T09:10:00Z"
    accuracy: { basis: upstream, days: 0, window_days: 30, upstream_mae_ore_kwh: 25 }
```

The forecast sensor makes one extra request to the public Wolta price endpoint per poll to get days 2 and 3; the plain spot price sensor makes none, so with the forecast sensor disabled nothing changes. If that extra request fails, the sensor keeps today, tomorrow and tomorrow's forecast, and `forecast_days` is an empty list until the next successful poll. `prices` holds up to roughly 400 points, which is why it is not stored by the recorder (see below).

**What the forecast is based on, and how good it is.** The forecast comes from the open-source [EpexPredictor](https://github.com/b3nn0/EpexPredictor), which predicts day-ahead prices from weather forecasts (Open-Meteo), expected generation and load (energy-charts) and recent price history. Wolta measures how good the forecast was: it compares the forecast that was actually shown before 13:00 with the prices that were then published. The attribute `forecast_accuracy` carries the result:

| Key | Meaning |
| --- | --- |
| `basis` | `wolta` when at least 14 days have been measured for that horizon, otherwise `upstream`. Days 2 and 3 are measured separately from tomorrow, so they show `upstream` for a while. |
| `days`, `window_days` | How many days Wolta has measured, and the window it looks at (30 days). |
| `mae_ore_kwh` | With `basis: wolta`: Wolta's own mean absolute error per quarter, in öre/kWh excluding VAT, over the window. `null` otherwise. |
| `peak_mae_ore_kwh` | With `basis: wolta`: the same error over the day's 8 most expensive quarters, the ones that matter most for charging and discharging decisions. `null` otherwise. |
| `upstream_mae_ore_kwh` | The predictor project's own backtest error for your zone, per horizon: in `forecast_accuracy` it is the figure for tomorrow, and inside each `forecast_days` entry it is the figure for that day's horizon. At current rates it is roughly 18–26 öre/kWh for tomorrow and about 21–29 öre/kWh for days 2 and 3 across SE1–SE4. The developers measured it with actual weather, so the real-world error is larger, and more so for days 2 and 3 than for tomorrow. |

Until Wolta has measured 14 days for a horizon, treat the upstream figure as the best available estimate for it. A forecast with this error is good for judging which hours of tomorrow are likely to be cheap or expensive; it is not a price to rely on to the öre. The `accuracy` of a later day in `forecast_days` has the same keys.

**The `prices` attribute is not stored by the recorder.** With three forecast days it holds up to roughly 400 points (about 100 per day at quarter-hour resolution) and would otherwise be written to the database on every change. Read it live from the state, for example in a template or an automation; it does not appear in the history graph.

**Source.** The `attribution` attribute comes from the server and is in Swedish. On the plain spot price sensor it names the source of the prices only. On the forecast sensor, while a forecast is present, it also names the forecast sources and links to EpexPredictor, energy-charts and Open-Meteo. The integration shows the price data as information and makes no claim about a licence for it.

### Example: feeding the prices to EMHASS

A template sensor can turn the `prices` attribute into a plain list, for example for EMHASS's `load_cost_forecast`. This is an illustration, not a tested configuration: adapt the horizon (here 96 quarters, 24 hours) and the time step to your EMHASS setup. The prices in the attribute are excluding VAT; the template already multiplies by `vat_factor`, so the list is VAT-inclusive (drop that factor for the raw prices). Grid tariffs and other taxes are not included (the [*Purchase price* sensor](#purchase-price-sensor) has them, including VAT). Points without a price (no exchange rate) are left out, which can shorten or shift the list, and EMHASS needs a list that matches its horizon: check the length before relying on it. Since v0.52.0 the `prices` list can run well past 96 quarters (up to about three days of forecast), so keep the slice at the end of the template, or the list becomes longer than the EMHASS horizon; the days beyond tomorrow are the least certain part of it.

```yaml
template:
  - sensor:
      - name: "Spot price forecast list"
        unique_id: wolta_spot_price_forecast_list
        state: "{{ (state_attr('sensor.wolta_spot_price_with_forecast', 'prices') or []) | length }}"
        attributes:
          load_cost_forecast: >-
            {% set vat = state_attr('sensor.wolta_spot_price_with_forecast', 'vat_factor') | float(1.25) %}
            {% set points = state_attr('sensor.wolta_spot_price_with_forecast', 'prices') or [] %}
            {% set from_ts = now() - timedelta(minutes=15) %}
            {% set ns = namespace(prices=[]) %}
            {% for p in points %}
              {% if p.price is not none and as_datetime(p.start) >= from_ts %}
                {% set ns.prices = ns.prices + [(p.price * vat) | round(4)] %}
              {% endif %}
            {% endfor %}
            {{ ns.prices[:96] }}
```

Replace `sensor.wolta_spot_price_with_forecast` with your entity ID (see [Verify entity IDs](#verify-entity-ids)). The last quarters of the list are forecast values; check `forecast` on the points if you want to treat them differently. The sensor's state is the number of points (a stable value on purpose; a timestamp would be recorded every minute). The list changes every quarter-hour, so consider excluding the sensor from the recorder:

```yaml
recorder:
  exclude:
    entities:
      - sensor.spot_price_forecast_list
```

## Purchase price sensor

`sensor.wolta_purchase_price` (v0.53.0+) shows what a kilowatt-hour costs you to buy **right now**, in SEK/kWh **including VAT**, per quarter-hour. It is for controlling consumption and storage against the price you really pay (for example as the import price in EMHASS), not the market price. It is **disabled by default**; enable it like the forecast sensor, under *Settings → Devices & services → Wolta → entities* (open the disabled *Purchase price* entity and switch it on). It exists for the Swedish price zones SE1–SE4, like the spot price sensors, and is information only: it is not part of the optimisation grade, the economics or anything the integration uploads. What you are paid for sold electricity is on its own [Sale price sensor](#sale-price-and-forecast-price-sensors) (v0.54.0+), which is available independently of this one.

**What is in the price:** the spot price, plus your electricity supplier's markup, plus the energy tax (after the reduced energy tax, if you have it), plus your variable grid fee (the price of your time period in a quarter that lies in one, if your tariff has time periods), with 25 % VAT on all of it. The markup, the grid fee and the electricity contract are the ones you saved under *Configure → Settings → Tariffs* (and the time periods set on wolta.se), the same ones the grade and the economy figures use; the server works the price out from them, the integration only reads the result.

**Time periods with a price of their own (v0.54.0+, set on wolta.se).** If your network operator charges a different grid fee at some times (a time-of-use tariff: for example weekdays 06–22 from November to March, or day and night every day of the year), the grid fee has time periods with a price of their own. They are set on wolta.se, not in Home Assistant (*Configure → Link to a wolta.se account* if your plant is not linked yet): each period is a set of months, all days or weekdays (with a holiday rule), and a start and an end hour in Swedish local time. *Grid fee (transfer)* is then the price outside the periods and is required. The options form shows how many periods your grid fee and grid benefit have and never changes them. Each point in `prices` has `grid_fee`: the whole variable grid fee of that quarter in SEK/kWh excluding VAT (the period's price or the base price, plus the percentage of spot), so you can see why the price differs between quarters. The percentage of spot in the grid fee is always counted on the market's spot price, whichever contract you have.

**The electricity contract (v0.54.0+).** *Electricity contract* is the contract you have today: *Quarter-hour price* (the standard: the spot price of each quarter-hour plus your markup), *Monthly price* or *Fixed price*. While the contract that applies today has no type (no type stored, or a contract history whose today's contract has none), the form starts on *Not stated*: Wolta counts quarter-hour price until you choose. If none of your contracts has a type yet, your first choice, *Quarter-hour price* included, applies to all of your history. Saving with *Not stated* sends nothing. A markup change keeps the type not stated, in the contract history too, and your first choice then gives every contract in the history that type, each with its own markup. *Fixed price* is the exception: if any of those contracts has a markup, it was a spot contract, so the fixed price applies from today and the earlier contracts stay not stated. With *Monthly price* the price is the average of the spot price over the calendar month (the plain average of the month's spot prices: quarter-hour from October 2025, hourly before) and it is set after the month is over, so the sensor has no quarter-hour price (`reason: monthly_price_contract`); the grade and the economy figures still use the monthly average. Your retailer's cost for volume weighting is not part of that average: put it in the markup. With *Fixed price* (*Fixed price*, excluding VAT, öre/kWh, including the retailer's own markup) the price is built on that price instead of spot plus markup (the energy tax, the grid fee and VAT are added as usual). A fixed price carries no markup: Wolta removes the markup when you choose it, and the form refuses a markup typed next to a fixed price. **After your first answer, a change applies from today.** Wolta keeps a contract history: a change of the contract or the markup is added as a new contract from today, and the grade and the economy figures keep counting earlier days with the contract you had then, while forward-looking figures (payback, IRR) and the sensor use today's contract. Two first answers apply to all of your history instead, because there was no earlier answer of yours to keep: the first contract type you choose, while no contract in your history has one, and a markup you enter for the first time without changing the contract type. A contract that changed on another day, and a mistake to correct back in time, are fixed in the contract list on wolta.se, which also shows the whole history. Wolta counts per quarter-hour: a contract priced per hour is best entered as *Quarter-hour price*, and the price can then differ within the hour from what your retailer charges. Contracts that mix a fixed price and a spot price are not supported.

**What is not in the price:** fixed charges that cannot be expressed as a price per kWh: the subscription fee to your supplier and to your grid operator, the fuse fee and the power tariff (effektavgift). Contracts that mix a fixed price and a spot price are not supported (see above). If your supplier charges elcertifikat or similar per kWh, include it in your markup.

**The spot price sensors stay excluding VAT.** The Purchase price sensor is the only one that includes VAT. In its `prices` attribute `purchase` is including VAT and `spot` is the spot price **excluding VAT**, the same figure as `sensor.wolta_spot_price`. Both are SEK/kWh.

| Part | Description |
|------|-------------|
| State | The purchase price of the current quarter-hour, SEK/kWh including VAT. Worked out locally from `prices` and flips on the quarter-hour without waiting for the next poll. |
| `prices` | A list of `{start, purchase, spot, grid_fee}` points (UTC start time, SEK/kWh) for today and, once it is published, tomorrow. `purchase` is `null` for a quarter that has no price (see below). `grid_fee` is the quarter's whole variable grid fee excluding VAT (see above); it is `null` when you have not entered *Grid fee (transfer)* (the country's standard is then used, which is not your figure), when the percentage of spot cannot be counted, and from a server that does not send it. Not stored by the recorder, like the spot price lists. |
| `components` | What the price is built from: `surcharge_ore`, `grid_var_ore` and `grid_var_pct` (your tariff), the grid fee's time periods as they are applied (`grid_fee_periods` and its holiday rule `grid_fee_holidays`, both `null` without periods), today's contract (`retail_price_model`, `retail_fixed_ore`; `surcharge_ore` is `null` with a fixed price), `reduced_energy_tax` (your answer: `true`, `false` or `null` for not answered), `energy_tax_ore` (the energy tax in öre/kWh excluding VAT, after any reduction, per calendar year as `{"2026": ...}`) and `vat_factor`. |
| `reason` | `null` while the sensor has a price; otherwise why it has none (the table below). |
| `tomorrow_complete` | Whether all of tomorrow's quarters are published. |

**When the sensor has no price, `reason` says what to do.** The state is then `unknown` (not `unavailable`, because Home Assistant hides every attribute of an unavailable entity and `reason` is the point). Most fixes below are in *Configure → Settings → Tariffs* (for a view-only plant the tariff is set on wolta.se instead); the last three rows (`fx_missing`, `zone_not_supported`, `monthly_price_contract`) say what to do instead:

| `reason` | What it means and what you do |
|----------|-------------------------------|
| `tariff_not_set` | You have not entered both the grid fee and the supplier markup. Wolta does not guess them: set both under *Configure → Settings → Tariffs*. `0` counts as set. |
| `grid_fixed_missing` | Your grid fee is a percentage of spot without a fixed part. Fill in the fixed part of the grid fee (öre/kWh excluding VAT, `0` if you only have a percentage) under *Configure → Settings → Tariffs*. The *Wolta: your grid fee needs its fixed part* repair says the same. |
| `energy_tax_unconfirmed` | In SE1–SE3 Wolta needs to know whether your municipality has the reduced energy tax: answer *Reduced energy tax* under *Configure → Settings → Tariffs* (*Yes* or *No*). SE4 is not asked. |
| `fx_missing` | Wolta has no EUR/SEK exchange rate at the moment, so neither the spot price nor the purchase price can be converted. Nothing to do on your side: it comes back when the rate does. |
| `zone_not_supported` | The plant's price zone is not SE1–SE4, where Wolta prices electricity. Correct the zone under *Configure → Correct the price zone* if it is wrong. |
| `monthly_price_contract` | Your *Electricity contract* is *Monthly price*: the price is set after the month is over, so there is no quarter-hour price to show. Nothing is wrong. The sale sensors are not affected. Choose *Quarter-hour price* or *Fixed price* under *Configure → Settings → Tariffs* if that describes your contract better. |

A single quarter can also have no price (`purchase` is `null` in `prices`, and the state is `unknown` while it lasts) when Wolta does not have the energy-tax rate for that calendar year yet. It never reuses last year's tax.

The state is `unavailable` when the data is missing altogether: the Wolta server could not be reached and there is no earlier answer to fall back on, or the data is out of date. A single failed poll does not do it while the integration holds an earlier answer with prices for your zone: it keeps that answer and the sensor keeps showing the price of the current quarter-hour from it for as long as it covers that quarter-hour; when it no longer does, the state is `unavailable`. An earlier answer in which neither the purchase price nor the sale price was available (both sides have a `reason`), a changed price zone and a saved tariff change all discard the kept answer, so after those a failed fetch makes the sensor `unavailable` straight away. A Wolta server older than **API 0.118.0** does not have the endpoint and answers 404, which also shows as `unavailable`. The integration logs one line when the fetch starts failing and one when it works again, and the spot price sensors are not affected.

**One extra request, only when you use it.** While the sensor is enabled the integration asks the Wolta server for the prices once per 15-minute poll, alongside the spot price request, with your plant's token in the `Authorization` header. With the sensor disabled nothing extra is requested. Saving a changed tariff under *Configure* refreshes it straight away.

## Sale price and forecast price sensors

Three more sensors (v0.54.0+, **disabled by default**, Swedish zones SE1–SE4) read the same answer from the Wolta server as the [Purchase price sensor](#purchase-price-sensor). Enable them the same way, under *Settings → Devices & services → Wolta → entities*. They are information only: not part of the optimisation grade, the economics or anything the integration uploads. **They need a Wolta server with API 0.119.0 or later** (an older server has no sale price: the sale sensors then sit at `unknown`, and the purchase sensors work as before).

| Entity (English / Swedish instances) | Description |
|--------|-------------|
| `sensor.wolta_sale_price` / `sensor.wolta_forsaljningspris` | What you are paid for a sold kWh in the current quarter-hour, SEK/kWh **excluding VAT**. |
| `sensor.wolta_purchase_price_with_forecast` / `sensor.wolta_inkopspris_med_prognos` | The purchase price, with Wolta's forecast days added to `prices`. |
| `sensor.wolta_sale_price_with_forecast` / `sensor.wolta_forsaljningspris_med_prognos` | The sale price, with Wolta's forecast days added to `prices`. |

**What is in the sale price.** The spot price, plus the grid benefit (*nätnytta*) your network operator pays for the quarter (the fixed part, plus the percentage of spot if you entered one), plus your electricity retailer's terms (a premium or a deduction on the spot price), plus the tax reduction for micro-production that applies in the quarter's calendar year (none from 1 January 2026, when it was abolished; `components.export_credit_ore` shows the figure per year). The server works it out from the tariff you saved under *Configure → Settings → Tariffs*, with the same function the grade uses for the export price, so the sensor and the grade cannot disagree about what a sold kWh earns. The integration only reads the result.

**Excluding VAT, for a private person.** A private person who sells at most SEK 120,000 of electricity per calendar year does not charge VAT, and the network operator does not add VAT to the grid benefit for someone who is not liable for VAT (source: Skatteverket, *Mikroproduktion av förnybar el*). The sale price is therefore without VAT, unlike the purchase price. If you are VAT-registered, the sensor is not meant for you. The `spot` in the sale price's `prices` is, as everywhere, the spot price excluding VAT.

**The grid benefit can have time periods too.** If your network operator pays a different grid benefit at some times, its time periods are set on wolta.se in the same way as the grid fee's. They are separate from the grid fee's, because an operator can use other hours and days for what it pays than for what it charges, so a quarter can lie in a period on one side only. *Grid benefit* is then the amount outside the periods and is required. Each point in `prices` has `grid_benefit` (SEK/kWh) so you can see which grid benefit a quarter used; it is the whole grid-benefit part of the sale price, its percentage of spot (*Sold electricity, percent of spot*) included, so the sale price is spot + `grid_benefit` + the retailer's terms + the tax reduction. A period covers chosen months, all days or weekdays (minus the days of a holiday rule) and an hour range, so operators with other months, weekends or several periods a day can be described too (at most six periods per list).

| Part | Description |
|------|-------------|
| State | The price of the current quarter-hour, SEK/kWh. Worked out locally from `prices` and flips on the quarter-hour without a poll. |
| `prices` | A list of points in time order, UTC `start`, SEK/kWh. Sale sensors: `{start, sale, spot, grid_benefit}` (`sale` is `null` for a quarter without a sale price). Purchase sensors: `{start, purchase, spot, grid_fee}` as above. `grid_fee` and `grid_benefit` are excluding VAT, and `null` when you have not entered the base price, when a percentage of spot cannot be counted because the spot price is missing, and from a server that does not send them. `grid_fee` includes the grid fee's percentage of spot and `grid_benefit` the grid benefit's: each is the whole grid part of its price, exactly as the server counts it. Not stored by the recorder. |
| `components` | What the prices are built from: for the sale price `grid_benefit_ore`, the grid benefit's time periods (`grid_benefit_periods` and `grid_benefit_holidays`, `null` without periods), `export_extra_ore` (the retailer's terms), `export_extra_pct` and `export_credit_ore` (per calendar year), next to the purchase price's parts. |
| `reason` | `null` while the sensor has a price; otherwise why not. On the sale sensors it is the server's `sale_reason`, on the purchase sensors the `reason` described [above](#purchase-price-sensor). |
| `tomorrow_complete` | Whether all of tomorrow's quarters are published. |

**When the sale sensor has no price, `reason` says what to do.** The state is `unknown` (not `unavailable`, for the same reason as on the purchase sensor), and it is independent of the purchase side: either price can be missing while the other works.

| `reason` | What it means and what you do |
|----------|-------------------------------|
| `grid_benefit_not_set` | You have not entered your grid benefit. Wolta does not guess it: enter it under *Configure → Settings → Tariffs* (*Grid benefit*; `0` counts as set). It is on your network operator's statement. |
| `fx_missing` | Wolta has no EUR/SEK exchange rate at the moment, so the spot price cannot be converted. It comes back when the rate does. |
| `zone_not_supported` | The plant's price zone is not SE1–SE4. |

**The two forecast sensors.** The state is the same as on the plain sensors. `prices` also carries Wolta's forecast for up to three days ahead (the days that have no published price yet), and each point has `forecast: true` or `false`; forecast points also have `horizon` (1 = tomorrow, 2 or 3). The points are in time order and no timestamp appears twice (a published price always wins over a forecast for the same quarter). They are separate sensors, as the spot price with forecast is, so that whoever reads `prices` on the plain sensors does not suddenly get forecast points. The attribute `forecast_days` lists the date, `horizon` and issue time of each forecast day, and `forecast_source` and `attribution` credit the forecast sources (EpexPredictor, energy-charts and Open-Meteo); `forecast_source` is `null` while there are no forecast days. The same uncertainty applies as for the [spot price forecast](#spot-price-sensors): the further ahead, the less certain, and a forecast price is not a price to rely on to the öre. The forecast never feeds the grade or the economy figures. `prices` holds up to roughly 400 points and is not stored by the recorder.

**One request serves all four.** The purchase price, the sale price and the two forecast sensors come from one read of the Wolta server per 15-minute poll, however many of them are enabled; with all of them disabled (the default) nothing is requested. If a poll fails, the sensors keep the last answer while it covers the current quarter-hour and then become `unavailable`, as the purchase sensor does; an earlier answer is kept when either the purchase price or the sale price in it was available. The side of such an answer that had no price (it shows `unknown` with its `reason`) is bounded the same way: once the kept answer no longer covers the current quarter-hour, that sensor becomes `unavailable` too, instead of showing an old `reason` for good.

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

**Re-authenticating a plant that belongs to a wolta.se account needs a token from the account.** Wolta does not hand out a new token for an existing plant on the strength of this installation's plant id alone when an account owns the plant (Wolta security review 2026-10-16), and the owner gets an email about the refused attempt. Re-authentication then asks you to paste a token instead: log in on wolta.se, open the plant, choose *Create new token* and paste it. The integration checks that the token belongs to this plant before it uses it, and the plant's history stays where it is. Entries created before v0.16.0 also get a fresh random plant id on their next start, replacing Home Assistant's entry id, which Home Assistant shows in its own address bar.

## Adjusting values afterwards

Open the integration's **Configure** dialog (Settings → Devices & Services → Wolta → Configure) and choose **Settings** to adjust values without removing the integration. (The other menu options — **Link to a wolta.se account** and **Correct the price zone** — are covered under [Account linking](#account-linking) and [Correcting the price zone](#correcting-the-price-zone) below.) The form is grouped into **Battery**, **Economy** and **Tariffs** sections:

- Battery capacity (kWh), power (kW) and round-trip efficiency — changing these triggers a server-side regrade of your optimisation score. While Wolta is still measuring a newly created battery (status sensor shows *Measuring battery* or *Needs battery values*), **capacity and power** are blank and optional rather than required, so saving other settings doesn't overwrite the pending measurement with a guess. (Efficiency stays a required field — it is never measured from scratch, so it is always prefilled.)
- Nameplate capacity (kWh) and nameplate power (kW) — optional manufacturer-rated figures. The grade itself always uses the usable/deliverable values above; the rated figures let wolta.se compare per-kWh prices fairly in the expansion calculator and explain measured-vs-rated differences. Filling in **Nameplate capacity (kWh)** and **Nameplate power (kW)** has the same effect as the Repair: Wolta derives the usable capacity from them right away, and switches to the measured value once the data allows. Clearing a field removes the value.
- What you paid for the battery, and the purchase date — used for IRR, payback and this year's actual savings. Enter the invoice total, after any green deduction; it is treated as the whole investment. If Home Assistant's sensor history suggests a purchase date, it's offered as a one-time suggestion here rather than being set automatically — accept it, type your own, or clear it if you don't know the date. Clearing a field removes the value.
- Your own tariff — grid fee, electricity supplier markup, your electricity contract, and for sold electricity the grid benefit and your retailer's terms (öre/kWh for SEK zones, euro cents/kWh for other zones). The fields are named as on wolta.se: *Grid fee (transfer)*, *Grid benefit* and *Electricity contract*. The grid fee is the transfer fee **excluding VAT and energy tax**: Wolta adds both itself. Clearing a field reverts it to your country's standard tariff. If the grid fee also has a percentage of spot (*Grid fee, percent of spot*), the fixed grid fee is required (v0.53.0+): enter `0` if you only have a percentage. Wolta cannot work out a purchase price from a percentage alone and refuses the save otherwise.
- **Electricity contract; time periods and history on wolta.se** (v0.54.0+, SE1–SE4 only, needs Wolta API 0.119.0 or newer). The form edits today's simple values: *Electricity contract* (*Quarter-hour price*, the standard, *Monthly price* or *Fixed price*; *Not stated* while today's contract has no type), *Fixed price* (the retailer's price excluding VAT, öre/kWh; required with *Fixed price* and only allowed with it) and the markup. **After your first answer, a change applies from today**: Wolta adds it to your contract history as a new contract and keeps the earlier contracts as they were. The exceptions are your first answers: the first contract type you choose (while no contract in your history has one) and a markup entered for the first time without changing the contract type apply to all of your history. Choosing *Fixed price* removes the markup (a fixed price is the retailer's whole price), and leaving it removes the fixed price. Everything more advanced is done on wolta.se (*Link to a wolta.se account* first if your plant is not linked yet): time periods with their own grid fee or grid benefit, their holiday rules, the contract history, the history of the grid benefit and of the retailer's terms (see below), a contract or terms that changed on another day, and corrections back in time. The form shows a short summary of them (how many time periods each side has, how many entries each history holds and since when today's applies) and never sends or changes them. While a side has time periods, its base price (*Grid fee (transfer)* or *Grid benefit*) is required. The form says what is missing before it sends, and shows the server's refusal at the field and at the top of the form. See the [Purchase price sensor](#purchase-price-sensor) for what each choice means; saving any of these refreshes the price sensors straight away.
- **Sold electricity: grid benefit and retailer's terms are two different things** (v0.54.0+). *Retailer's terms for sold electricity* (`export_extra_ore`) is what your **electricity retailer** pays (+) or deducts (−) per sold kWh on top of the spot price; it may be negative, and `0` means your retailer pays plain spot. Before v0.54.0 this field was called *Export premium/discount* and many people entered their grid benefit in it. The grid benefit (*nätnytta*) is paid by your **network operator** and has its own fields: *Grid benefit*, an amount excluding VAT in öre/kWh between −20 and 50 (`0` is a valid answer), and a percentage, *Sold electricity, percent of spot* (`export_extra_pct`, between −100 and 100, shown in every zone; in SE1–SE4 it is the percentage part of the grid benefit, for operators that pay a fixed part plus a share of spot; like the grid fee percentage it needs the fixed part, *Grid benefit*, `0` if you only have a percentage). A grid benefit that differs at some times is described with [time periods on wolta.se](#sale-price-and-forecast-price-sensors); *Grid benefit* is then the amount outside them. The form shows the grid benefit and the retailer's terms side by side, and saving a grid benefit also saves the retailer's terms (`0` if you left them empty), so you have seen and confirmed both. The grid benefit field is shown in SE1–SE4 only. Without an answer Wolta uses a standard 5 öre as grid benefit, which is a guess, and the [Sale price sensor](#sale-price-and-forecast-price-sensors) has no price (`grid_benefit_not_set`). Leaving the new fields empty keeps the grade exactly as before; setting them recomputes it.
- **The sale side has a history too** (v0.54.0+, SE1–SE4, Wolta API 0.119.0 or newer). The network operator's price list for the grid benefit normally changes at the turn of the year, and your retailer's terms change when you switch contract, so Wolta keeps a history of each. The form edits today's values with the same fields: after your first answer, a change applies from today, and the grade and the economy figures keep counting earlier days with the grid benefit and the terms that applied then, while the sale price sensor, payback and IRR use today's. Your first answer applies to all of your history instead. For the retailer's terms that is the first value other than 0, as long as the terms have no history on wolta.se: an empty field is stored as 0, so a 0 is not counted as an answer, and if you really had 0 until now you correct the date of the change in the history on wolta.se; with a history, every change of the terms applies from today, also from a 0. For the grid benefit it is the very first amount; after it, every change of the grid benefit (the amount, the percentage or the time periods) applies from today. A change on another day and corrections back in time are made in the two lists on wolta.se. The form shows how many entries each history has and since when today's applies, and never sends or changes them. While the grid benefit has a history, *Grid benefit* cannot be cleared (today's entry needs a base price): the form says so before it sends. Cleared retailer's terms are plain spot from today.
- **Reduced energy tax** (v0.53.0+, Swedish zones SE1–SE3) — *Not set*, *No* or *Yes*. Some municipalities in northern Sweden have a reduced energy tax: the grid operator deducts 9.6 öre/kWh excluding VAT. It applies in all municipalities in Norrbotten, Västerbotten and Jämtland, and in Sollefteå, Ånge, Örnsköldsvik, Ljusdal, Malung-Sälen, Mora, Orsa, Älvdalen and Torsby (source: Skatteverket). The municipalities do not follow the price zones, so Wolta cannot work it out from your zone; choose *Yes* if you live in one of them and *No* if you do not. The answer lowers the import price in the grade and the economics by that amount, and is part of the [Purchase price sensor](#purchase-price-sensor); until you answer, that sensor stays without a price (`energy_tax_unconfirmed`). SE4 has no affected municipality and is not asked. Leaving it *Not set* keeps the grade exactly as before.
- Reserve floor (%) — the share of usable capacity your control system never discharges below. Clearing the field removes the reserve.
- **Battery charge/discharge reversed** — a toggle that swaps the battery charge and discharge streams. It applies to the history Wolta already stores as well as to new uploads, without re-sending anything. See Troubleshooting below.

Only changed fields are sent to Wolta. After saving, a recompute is triggered automatically. The optimisation grade updates first; the economy figures (IRR, payback, actual savings) are recomputed in the background and follow a few minutes later. Throughout, the sensors keep their previous values instead of dropping to `unavailable` (v0.7.1+).

### Correcting the price zone

The price zone is chosen when the plant is created and normally stays put — it decides which day-ahead price series your grade and your economics are measured against. If it was set wrong, open **Configure → Correct the price zone** (v0.32.0+, requires server api 0.80.0+).

Only zones in the same country are offered. The amounts you have entered — what you paid, your grid fee, your supplier markup — are expressed in the zone's currency and are **not** converted, so a move across a currency boundary would silently reinterpret e.g. 100,000 SEK as 100,000 EUR. For that case the answer is still to delete the plant and set it up again.

A correction recalculates the grade and the economics against the new price series on your whole stored history, so **your figures will change**. The integration updates its own stored zone at the same time; nothing else needs doing.

**Changing energy sensors:** use the **Reconfigure** option (Settings → Devices & Services → Wolta → three-dot menu → Reconfigure) to pick new sensors. The integration reads the changed sensors' statistics and sends their values for the affected range, starting from the new sensor's first full hour of statistics; Wolta replaces only those values and recomputes the grade — no need to remove the integration. Removing a sensor never rewrites history. See [Correcting stored history](#correcting-stored-history).

### Correcting stored history

Changing the battery direction or the energy sensors does not make the integration upload your whole history again. It asks the server to correct what it already stores:

- **Battery direction.** The server swaps the battery charge and discharge values for exactly the period that was affected, losslessly, and nothing is sent again. Turning the toggle on or off, or confirming the direction repair, covers the stored history that the setting changes.
- **Changed sensors.** Only the values that come from a sensor you changed are replaced: solar, grid import, grid export, the two battery streams together, or the external control flag. The integration reads the changed sensors' statistics and sends their values for the affected range; the regular upload then sends the last few hours again. Other values stay as they are. The replacement starts from the new sensor's first full hour of statistics; earlier hours keep their old values. Inside the replaced range, gaps of up to 6 hours where the new sensor has no complete statistics are left empty, while longer silences keep the old values. The external control flag is replaced only over the period the binary sensor's recorded history covers. If the new sensors have no statistics yet, the integration waits and tries again on each update.
- **Removing a sensor** never rewrites history.
- **Older history.** A correction never touches stored history older than one year before the integration was set up. History from before that, for example a file uploaded on wolta.se, stays as it is.
- **Replaced values are kept.** The server keeps the replaced values for 90 days.

While a correction is running, regular uploads and the history check wait until it finishes. The progress is shown in the integration's diagnostics (**desired** against **applied** direction, and the running job). If the server has temporarily stopped accepting corrections for your plant, a repair says which correction is waiting and when it is retried automatically; nothing needs to be done.

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

**Optimisation grade is strongly negative or looks inverted.** This almost always means the battery charge and discharge streams are mapped the wrong way round — some battery integrations and energy meters report the two directions in a way Wolta reads reversed, which makes it look as though the battery charges when power is expensive and discharges when it is cheap. Open the **Configure** dialog and turn on **Battery charge/discharge reversed**. Wolta swaps the two streams for the affected period on the server, without re-sending your history, and recomputes the grade automatically — you don't need to change any of your Home Assistant sensors. If you have picked a state-of-charge sensor, Wolta's own direction repair settles the question: if no repair appears and the server confirms the direction, do not turn this setting on. If the grade still looks wrong afterwards, please [open an issue](https://github.com/Seblin01/wolta-homeassistant/issues).

**"Wolta: charging and discharging look swapped" repair appears (v0.43.0+).** Wolta compares the battery's state of charge with your charge and discharge data. If the state of charge falls while your data says the battery is charging, and rises while it says discharging, the two streams are reversed and Home Assistant raises a repair. The grade is then kept out of the comparison with other installations, the economics use rule-of-thumb figures instead of your measured data, and the battery value sensor falls back to the modelled value until the direction is right. What the repair offers depends on how far back the problem goes:

- **All the days Wolta can check are reversed** (reported as `all`): the repair offers to flip the direction. Confirming toggles **Battery charge/discharge reversed** (if it is off it is switched on, and the other way round), and the server swaps the stored battery values for exactly the affected period, without re-sending your history. The server keeps the replaced values for 90 days. The repair stays away until Wolta has recalculated after the flip, at the latest the next day, so it does not come back in the meantime.
- **Reversed for all the days since the state of charge became available, with no state of charge before that** (reported as `partial`): the flip is offered too, with a caveat. It is only right if your sensors have been the same the whole time. If you changed a sensor or a setting along the way, correct what changed under **Reconfigure** instead.
- **The direction became reversed at a certain date**: flipping everything would make the right part of the history wrong. When the server gives the reversed period, the repair offers to flip the direction for that period only; the time before stays as it is. If you would rather correct what changed on that date (a sensor, a setting or an update), do that instead of flipping, since doing both makes the direction wrong again. Without a period from the server the repair only explains: correct what changed, and once it is right again, let the grading window on wolta.se start after the correction.
- **The direction was reversed for a period that has ended**: the direction is right again. When the server gives the period, the repair offers to flip the direction for that period only; the time before and after stays as it is. Otherwise the repair only explains; let the grading window on wolta.se start after the last reversed day so the grade is only based on correct data.

While a direction change is still being applied to the stored history, the flip is refused, so a repair computed from the old direction cannot flip the same period back.

For the flip repair you can also choose **Ignore**, which is remembered until the server reports the direction as fine. The check needs a battery state-of-charge sensor (see below) and a recent enough Wolta server; it is not raised in view-only mode. The repair clears itself once the server no longer finds the direction reversed.

**"The same metering data exists in other plants at Wolta" repair (v0.46.0+).** If uploads at Wolta carry the same metering data as this plant and were uploaded after it (for example a CSV export of the same meter), Home Assistant offers to remove them. They are already counted only once in the comparison with other houses; removing them takes them out at once, and they are deleted after 30 days unless whoever holds their personal link chooses to keep them. Until then a removal can be undone on wolta.se once the plant is linked to an account (Configure → Link to a wolta.se account). Only uploads that are not on any wolta.se account are shown (copies on your own account are managed under My account), and uploads made before this plant are never shown. Ignoring the repair keeps it hidden until a new copy appears. The repair is not raised in view-only mode.

**"Wolta has stopped uploading: no selected sensor gives new statistics" repair (v0.47.0+).** Raised when none of the sensors you picked has given new statistics in Home Assistant for 12 hours – typically because the integration that provides them (an inverter or battery integration) has stopped working, or the sensors are unavailable. Wolta only uploads quarters in which every selected sensor has a value, so nothing is sent until they deliver again. The repair names the sensors and the time of the last statistics. Check the integration under Settings → Devices & services, or pick replacement sensors under Reconfigure. The hours without statistics cannot be measured afterwards and are not sent. The repair clears itself as soon as the sensors give statistics again. If only some of the sensors stopped, the "a selected sensor has no statistics" repair names them instead.

**Payback time shows `unknown`.** The sensor has no payback year to report because the battery doesn't reach break-even — typically a small or expensive battery whose modelled internal rate of return (IRR) is negative. This is a real result, not an error; the IRR sensor shows the (negative) return. Increasing the battery capacity or lowering the entered purchase price moves it towards a payback.

**The grade sensor has a `capacity_hint` attribute (v0.8.0+).** The backend sets it when the battery capacity you entered is clearly higher than the usable capacity your measured data shows — a nameplate-vs-usable mix-up. The grade compares you against a perfect dispatch on the capacity you *entered*, so an oversized entry unfairly lowers the score. Open **Configure** and set the battery capacity to the attribute's `suggested_kwh` (or your own better estimate of usable capacity); the grade is recomputed automatically. No hint means no mismatch was detected.

**"Wolta: your grid fee looks too high" repair (v0.49.0+).** Raised when the grid fee you entered is above the limit the Wolta server sets (60 öre or cents per kWh, higher than any variable grid fee Wolta has verified). Such a figure most likely includes VAT and energy tax. Wolta adds both itself, so the energy tax is then counted twice, in the grade and in the economics. Enter only the transfer fee, excluding VAT and energy tax, under Configure → Settings → Tariffs. If your network operator really charges that much, leave it: the figure is still used, and the repair is only a warning. It clears itself once the server finds the fee plausible. Not raised in view-only mode, where the tariff is set on wolta.se. Needs Wolta API 0.110.0 or newer.

**"Wolta: a time period of your grid fee looks too high" repair (v0.54.0+).** The same warning for the prices of your grid fee's time periods, with a limit of its own that the Wolta server sets (currently 80 öre per kWh): a time-of-use tariff really reaches 61 öre excluding VAT, so the base limit would flag a correct value. A figure above the limit most likely includes VAT and energy tax, which Wolta adds itself. Enter only the transfer fee, excluding VAT and energy tax, in the time periods on wolta.se (they are not in the options form); if your network operator really charges that much, leave it. It clears itself once the server finds the prices plausible. Not raised in view-only mode. Needs Wolta API 0.119.0 or newer.

**"Wolta: your grid fee needs its fixed part" repair (v0.53.0+).** Raised when the grid fee you entered is a percentage of the spot price without a fixed part. Wolta cannot work out what you pay for electricity from a percentage alone, so the [Purchase price sensor](#purchase-price-sensor) has no price (`reason: grid_fixed_missing`). Enter the fixed part of the grid fee (the transfer fee, excluding VAT and energy tax; `0` if you only have a percentage) under Configure → Settings → Tariffs. It clears itself once the server has the fixed part. Not raised in view-only mode, where the tariff is set on wolta.se. Needs Wolta API 0.118.0 or newer. If you are asked to re-authenticate while the fixed part is missing, the re-authentication form asks for it too.

**"Wolta: separate your grid benefit from your retailer's terms" repair (v0.54.0+).** Raised when you have entered a retailer's-terms (export premium) amount other than 0 but no grid benefit, in zones SE1–SE4. Wolta then adds a standard 5 öre of grid benefit on top of your figure. If your figure was really your grid benefit (the field used to be called *Export premium/discount*), it is counted twice. Open Configure → Settings → Tariffs and enter the grid benefit from your network operator and your retailer's terms separately (`0` as the retailer's terms if your retailer pays plain spot). **Nothing changes until you save**: the grade is calculated as before until then, and the repair does not move your figure for you. While your retailer's terms have no history on wolta.se, your answer corrects all of your history: saved together, the grid benefit and the corrected terms apply from the start, so the amount is counted once. With a history, correct it on wolta.se. It clears itself once the grid benefit is set. Not raised in view-only mode, where the tariff is set on wolta.se. Needs Wolta API 0.119.0 or newer.

**"Wolta: “Sold electricity, percent of spot” needs a fixed grid benefit" repair (v0.54.0+).** Raised when you have entered *Sold electricity, percent of spot* without the fixed part. Wolta then adds a standard 5 öre as the fixed part, which looks like your figure but is a guess. Enter *Grid benefit* (excluding VAT; `0` if you only have a percentage). It clears itself once the server has the fixed part. Not raised in view-only mode. Needs Wolta API 0.119.0 or newer. The options form also refuses to save the percentage without the fixed part, and says so at the field.

**"Wolta needs your battery's nameplate values" repair appears (v0.36.0+).** On a newly created plant, Wolta measures battery capacity, power and efficiency from the data you upload instead of asking for them at setup — the status sensor shows *Measuring battery* while that's in progress. If the data can't yield a measurement after enough time has passed — the battery never charges fully, or one of the battery streams is empty — this repair fires and asks for the manufacturer's nameplate capacity and power instead. Wolta derives the usable capacity from the nameplate pair and switches to the measured value later if the data allows it. You can also enter the nameplate values proactively at any time under Configure → Settings, without waiting for the repair.

**"Your energy data doesn't add up" repair appears (v0.38.0+).** Wolta derives your household consumption as solar + grid import + battery discharge − export − battery charge. If, for a large part of the grading window, the battery takes in more than solar and grid deliver in the same interval, the sensors don't describe the whole house and the grade would measure the sensor mapping rather than your battery's control. The backend then keeps your grade out of the comparison with other installations (you still see it yourself), and this repair tells you which quantity doesn't add up — solar reading too low (typically solar measured after a DC-coupled battery, or a missing inverter/string), grid import or charge not adding up, or a missing sensor — and names the sensors you picked. The decision is the backend's; the integration never applies its own threshold. It is not raised in view-only mode, where the data doesn't come from your Home Assistant sensors. It clears itself once the grading window no longer contains the mismatched data.

**Measured-parameter repairs appear (v0.12.0+).** Once there are a couple of months of history, Wolta measures the grade-affecting battery parameters from your battery's own sensors (not the electricity meter) and, if one clearly disagrees with what you configured, raises a fixable repair. They only appear when the measurement is mature and confident (≥60 days and a real gap), and the measured values are all-time figures, so they're stable across seasons. Each repair opens on a menu: **adopt the measured value** or **ignore and keep your value** (v0.26.0+) — ignoring is remembered so the repair stops reappearing, which is the right choice if you simply haven't fully cycled the battery in the period, or if a spiky sensor has biased the measurement.

- **Usable capacity** — the usable capacity as your battery sensors see it (not the nameplate figure; on an AC-coupled battery also below the DC/cell figure your battery app shows). Adopting sets the battery capacity and **clears any reserve floor**: the measurement already reflects only the window you actually cycle, so the reserve must not be subtracted again.
- **Peak power** — the most power the battery sensors have seen it charge/discharge. This is a *lower bound* (your control may never have demanded full power), so adopting opens an editable field pre-filled with the measured value — set the battery's real inverter limit. Too low a value flatters the grade; too high unfairly lowers it. If a spiky sensor reports a peak above what your hardware can physically deliver (more than 50 % over your declared/nameplate power), the repair is suppressed automatically rather than nagging you to raise a correct value.
- **Round-trip efficiency** — the measured energy-out ÷ energy-in where your battery sensors measure: on a hybrid inverter often the battery's DC side, on an AC-coupled battery the AC side (including inverter losses). A ratio outside 0.50–0.98 is treated as a broken series and never offered (v0.46.1+). One-click adopt; corrects a stale efficiency set from thin history at setup. Because a spurious sensor jump inflates the charged sum and biases this *low*, and there's no clean physical floor to auto-suppress that, the honest choice is to adopt it or ignore it.

**The grade sensor exposes the measured parameters as attributes (v0.23.0+).** The same three measurements are available directly on the grade sensor for dashboards and template sensors: `measured_capacity_kwh`, `measured_power_kw` and `measured_efficiency` (a fraction), each with a companion `measured_*_status` attribute — `ok`, `immature` (data hasn't reached the measurement's maturity threshold yet) or `unmeasurable` (impossible with your sensor setup, e.g. a net-metering sensor that collapses the battery flow). These are measured lower bounds, not nameplate specs. An absent value simply omits its attribute; the status says why.

**A re-created plant gets its history back (v0.42.0+).** If your plant is deleted on wolta.se and you confirm the re-authentication prompt in Home Assistant, the integration creates a new plant. Earlier versions only sent data from that point on. From v0.42.0 the integration compares its history with what the server holds and sends what is missing before the server's earliest stored data (up to 365 days back, the same window as the first setup). Data the server already has is never re-sent or overwritten, so a plant whose data was kept (for example after rotating the access token on wolta.se) only gets history older than what the server already holds, which normally means nothing. The check runs once after updating and after each re-authentication, and again only if the server later reports less history than before. The history is sent newest first, so an interrupted fill continues on the next update. This needs Wolta API 0.96.0 or newer; older servers make the integration skip the check. A re-created plant gets back today's tariff and contract from Home Assistant, but not the time periods, the contract history or the history of the grid benefit and the retailer's terms (v0.54.0+), which only the deleted plant held: enter them again on wolta.se.

## Full results on wolta.se

The Wolta device page has a **Visit** link that opens your plant on wolta.se, using a read-only link minted automatically for this installation (backend v0.79.0+; existing entries mint one at their next setup). It shows the full plant view — grade breakdown, economy drill-downs, history — but the link itself writes nothing: it cannot edit economy, tariff or grade-window fields, delete the plant, mint a new link, share the plant with anyone else, or edit technical fields (sensors, battery capacity/power/efficiency). The link sits in `configuration_url`, which travels in every device-registry export people attach to GitHub issues or forum posts, so it is deliberately unable to change anything, even the fields it lets you view. Anyone with access to your Home Assistant can follow the link and see everything it shows. For any editing on wolta.se, see [Account linking](#account-linking) below.

## Account linking

The read link above covers viewing your results — grade breakdown, economy drill-downs, history — but not changing them. Editing anything on wolta.se (economy, tariff, grade window, technical fields, deleting the plant, sharing, rotating the link) requires linking the plant to a wolta.se account — it is the only way to change a Home-Assistant-created plant from wolta.se.

Open the integration's **Configure** dialog (Settings → Devices & Services → Wolta → Configure) and choose **Link to a wolta.se account** instead of **Settings**. The flow mints a one-time linking code, shown on screen. Sign in or create an account at wolta.se, go to **Account → Link plant**, and enter the code there.

The code is valid for 10 minutes and can be used once. If it expires before you enter it, or you've already used it, open the menu again to mint a fresh one.

## State of charge (SoC) monitoring

Wolta measures your battery's real capacity and round-trip efficiency from its state of charge (SoC): how much energy it actually stores and returns, compared with the manufacturer's figure. The grade is computed against those measured figures, so the details you entered cannot move it. A plant whose SoC data does not pass Wolta's quality checks, or that has no SoC sensor, gets a grade computed on the details you entered; that is a grade of your own and is not compared with other installations (the grade sensor's `benchmark` attribute lists the reason). For this reason a SoC sensor is required (see below). The economics keep using the details you entered either way. Wolta also checks with the SoC whether your battery's charge and discharge streams are reversed (see the "charging and discharging look swapped" repair under Troubleshooting); if they are, the grade is kept out of the comparison with other installations and the economics use rule-of-thumb figures until the direction is right. This needs a Wolta server that computes grades against the data-driven benchmark; before that the SoC data is collected and used for the direction check only.

A SoC sensor is required. Both the setup flow's entity step and the **Reconfigure** option (Settings → Devices & Services → Wolta → three-dot menu → Reconfigure) ask for it: point the field at up to 16 `sensor` entities with `state_class: measurement` and `unit_of_measurement: %` (a state-of-charge reading from your battery or inverter's own monitoring). The integration reads their Home Assistant statistics and sends them to Wolta. Each sensor is labelled with its entity ID and integration, so each sensor can be told apart. Adding a SoC sensor uploads its history (up to 365 days) right away; your energy data is not re-sent. Removing a sensor stops collecting it; the last one can only be removed by ticking "My battery has no SoC sensor".

If your battery has no SoC sensor, tick **My battery has no SoC sensor** instead. The box and a picked sensor cannot be combined, and you can change the answer at any time under Reconfigure. Without a sensor the grade is not compared with other installations.

**"Wolta needs your battery's state-of-charge sensor" repair (v0.50.0+).** Raised once a plant with a battery has a settled grade and has neither a SoC sensor picked nor ticked **My battery has no SoC sensor**. It replaces the earlier optional "add your battery's state-of-charge sensor" repair (v0.41.0+); plants that had ignored the old one are asked again. It offers two choices and has no ignore option of its own: **pick a state-of-charge sensor** (the picker lists sensors in % with `state_class: measurement`; saving reloads the integration and up to 365 days of history is uploaded right away) or **my battery has no SoC sensor**. Picking a sensor changes nothing but the SoC choice; the energy sensors and their uploaded history are left alone. If Wolta cannot be reached when you answer, the repair stays open; try again later. It is not raised in view-only mode, for plants without a battery, or while the battery is still being measured.

**"Wolta needs your battery's control system" repair (v0.50.0+).** Raised when the profile at Wolta has no control system, or Other without a name; pick one in the repair form. Not raised in view-only mode. You can change the choice later on wolta.se.

**What to expect:** plants that have neither picked a SoC sensor nor said that the battery has none will stop being able to upload data in a coming version. No date is set yet. Until then uploads continue as before.

The SoC data is used for the grade's capacity and efficiency and for the direction check described above, not for the economics. It is deleted together with the energy data. As described under Privacy, that happens on removal only for profiles the integration created, not for linked wolta.se profiles. This needs Wolta API 0.93.0 or newer; older servers silently ignore the SoC data.

## Privacy

Your 15-minute energy data is stored on Wolta's servers to power the analysis. No personal data (name, address, account) is sent or required.

If you select state-of-charge sensors, their min/max/average values are sent to Wolta along with the energy data: per hour for backfilled history, per 15 minutes for recent data. Adding a SoC sensor uploads its history (up to 365 days) right away; your energy data is not re-sent. Removing a sensor stops collecting it; the last one can only be removed by ticking "My battery has no SoC sensor". Each sensor is labelled with its entity ID and integration (for example "ha:<integration>:sensor.battery_soc"). The server stores the sensor identifier only as a keyed hash, but the label with the entity ID is stored as text. The data is used to measure the battery's capacity and efficiency for the grade and for the direction check described above. It is deleted together with the energy data (see below for linked profiles).

**Deleting the integration removes your data server-side** — for profiles the integration created. Removing the config entry in Home Assistant then triggers a right-to-erasure request to the Wolta backend. **Linked profiles are exempt:** removing the integration only disconnects Home Assistant; your wolta.se profile and history stay. Delete those from wolta.se itself.

While one of the *Purchase price*, *Sale price* or forecast price sensors is enabled (they are disabled by default), the integration also reads your plant's computed purchase and sale prices from Wolta every 15 minutes, with the plant token in the `Authorization` header. The request only reads; it changes nothing on the server.

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
| `sensor.wolta_comparison_grade` | `sensor.wolta_jamforelsebetyg` |
| `sensor.wolta_battery_value_per_year` | `sensor.wolta_batterivarde_per_ar` |
| `sensor.wolta_internal_rate_of_return_irr` | `sensor.wolta_intern_avkastning_irr` |
| `sensor.wolta_payback_time` | `sensor.wolta_aterbetalningstid` |
| `sensor.wolta_actual_savings_this_year` | `sensor.wolta_facit_i_ar` |
| `sensor.wolta_data_status` | `sensor.wolta_datastatus` |
| `sensor.wolta_spot_price` | `sensor.wolta_spotpris` |
| `sensor.wolta_spot_price_with_forecast` | `sensor.wolta_spotpris_med_prognos` |
| `sensor.wolta_purchase_price` | `sensor.wolta_inkopspris` |
| `sensor.wolta_sale_price` | `sensor.wolta_forsaljningspris` |
| `sensor.wolta_purchase_price_with_forecast` | `sensor.wolta_inkopspris_med_prognos` |
| `sensor.wolta_sale_price_with_forecast` | `sensor.wolta_forsaljningspris_med_prognos` |
| `sensor.wolta_status` | `sensor.wolta_status` |
| `button.wolta_recompute` | `button.wolta_rakna_om` |

If your entities differ (other language, integrations installed before v0.4.3, or a `_2` suffix when installed more than once): go to **Settings → Devices & Services → Wolta** to see the exact IDs, and adjust the dashboard YAML accordingly.

### Notes

The dashboard ends with a markdown card linking to your full results on wolta.se. The link is resolved dynamically from the device's `configuration_url` (v0.4.1+; a read-scoped link since v0.30.0 — see [Full results on wolta.se](#full-results-on-woltase)), so no manual token pasting and no dashboard change are needed.

Economy sensors (battery value, IRR, payback, actual savings) are only available for Swedish price zones (SE1–SE4). They show `unavailable` for other zones.

Most sensors show `unavailable` until the first recompute run completes, which requires at least 7 days of uploaded data (the preliminary optimisation grade, server v0.43.0+). The economy sensors (battery value, IRR, payback, actual savings) need more: a mature grade (30 days) for IRR/payback/actual savings, and a 180-day window before the battery-value sensor shows a measured figure instead of its modelled fallback. The battery-value sensor also shows the modelled fallback while Wolta reports the charge/discharge direction as reversed (see Troubleshooting).

During a recompute (e.g. after changing values), sensors keep their last known values instead of flickering to `unavailable`; retained attributes carry a `computing: true` flag and `sensor.wolta_status` shows **Computing** (`computing`) until the new results land (v0.4.2+). The grade is recomputed first and the economy figures (IRR, payback, actual savings) follow a few minutes later; they hold their previous value in the meantime rather than going `unavailable` (v0.7.1+).

## License

MIT – see [LICENSE](LICENSE).
