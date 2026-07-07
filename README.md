# Wolta for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
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
| `sensor.wolta_optimisation_grade` | % | Holistic optimisation score (0–100): the share of the battery's theoretically perfect total value your actual operation captured. All price zones. |
| `sensor.wolta_battery_value_per_year` | SEK / EUR | The battery's **own** annual value — the incremental saving vs. the same plant without a battery. Measured from your actual flows (the same number wolta.se shows as "You captured"); falls back to the modelled battery share when no grade exists yet. Attribute `source` tells you which (`measured`/`modelled`); `plant_total_sek` carries the plant total. All price zones. Solar's value is **not** included — it belongs to the solar investment. |
| `sensor.wolta_plant_savings_per_year` | SEK / EUR | Total annual saving of the whole plant (solar + battery), i.e. what a combined solar-plus-battery investment earns. Attributes `battery_sek`/`solar_sek` give the split. SE zones only. |
| `sensor.wolta_internal_rate_of_return_irr` | % | IRR of the **battery investment**: the battery-only savings stream against the battery's purchase price (incremental cash-flow principle). Can be negative — that means the battery alone does not carry its cost. SE zones only. |
| `sensor.wolta_payback_time` | yr | Payback of the battery investment from the battery-only savings stream. `unknown` when the stream never repays the cost within the projection horizon. SE zones only. |
| `sensor.wolta_actual_savings_this_year` | SEK / EUR | Actual battery revenue this year. SE zones only. |
| `sensor.wolta_data_status` | timestamp | Last data point uploaded (diagnostic). Always available. |
| `sensor.wolta_status` | enum | Computation status: `done` / `computing` / `waiting_for_data` / `error` (displayed translated). Always available. |

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

One-click (opens HACS on your instance with this repository preloaded):

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Seblin01&repository=wolta-homeassistant&category=integration)

Or add it manually:

1. In Home Assistant, open **HACS** (install it first if needed).
2. Open the three-dot menu (top right) → **Custom repositories**.
3. Paste `https://github.com/Seblin01/wolta-homeassistant`, choose category **Integration**, and select **Add**.
4. Search for **Wolta** in HACS and select **Download**.
5. Restart Home Assistant.
6. Go to **Settings** → **Devices & Services** → **Add Integration** and search for **Wolta**.

> Once Wolta is accepted into the HACS default store, you can find it by searching **Wolta** in HACS directly, without adding a custom repository.

## Manual installation

1. Copy `custom_components/wolta/` into your HA `config/custom_components/` directory.
2. Restart Home Assistant.
3. Add the integration via **Settings** → **Devices & Services**.

## Setup flow

No account or API token is required. The integration provisions a profile automatically during setup.

**Step 1 – Zone & battery**
- Select your Nordpool/ENTSO-E price zone (e.g. SE3).
- Enter your battery capacity (kWh) and peak power (kW). Both are required and must be greater than zero.
- Set the round-trip efficiency (default 0.9).

**Step 2 – Energy sensors**
- Map your HA energy sensors for battery charge, battery discharge, grid import and grid export. The integration prefills these from the HA Energy dashboard if it is configured.
- Solar production is optional.

**Step 3 – Privacy**
- Opt in to anonymised data sharing (off by default) if you want to contribute to the Wolta benchmark.
- Confirm to create the profile and complete setup.

## Adjusting values afterwards

Open the integration's **Configure** dialog (Settings → Devices & Services → Wolta → Configure) to adjust values without removing the integration:

- Battery capacity (kWh), power (kW) and round-trip efficiency — changing these triggers a server-side regrade of your optimisation score.
- Battery purchase price and purchase date — used for IRR, payback and this year's actual savings. Clearing a field removes the value.

Only changed fields are sent to Wolta. After saving, a recompute is triggered automatically and the sensors update within minutes.

## Full results on wolta.se

The Wolta device page has a **Visit** link that opens your complete results on wolta.se (grade breakdown, economy drill-downs, history) using your profile token. Note: anyone with access to your Home Assistant can follow the link.

## Privacy

Your 15-minute energy data is stored on Wolta's servers to power the analysis. No personal data (name, address, account) is sent or required.

**Deleting the integration removes your data server-side.** Removing the config entry in Home Assistant triggers a right-to-erasure request to the Wolta backend.

Anonymised corpus sharing is opt-in and defaults to off. See [wolta.se/om](https://wolta.se/om) for the full privacy policy.

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

The dashboard ends with a markdown card linking to your full results on wolta.se. The link is resolved dynamically from the device's `configuration_url` (v0.4.1+), so no manual token pasting is needed.

Economy sensors (battery value, IRR, payback, actual savings) are only available for Swedish price zones (SE1–SE4). They show `unavailable` for other zones.

All sensors show `unavailable` until the first recompute run completes, which requires at least 30 days of uploaded data.

During a recompute (e.g. after changing values), sensors keep their last known values instead of flickering to `unavailable`; retained attributes carry a `computing: true` flag and `sensor.wolta_status` shows **Computing** (`computing`) until the new results land (v0.4.2+).

## License

MIT – see [LICENSE](LICENSE).
