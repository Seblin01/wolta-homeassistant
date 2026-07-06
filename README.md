# Wolta for Home Assistant

A Home Assistant custom integration that connects [Wolta](https://wolta.se) to your Home Assistant instance. Wolta analyses your 15-minute battery and grid data against day-ahead electricity prices to grade how well your battery is optimised — and, for Swedish zones, calculates the battery's actual economic value.

## What it does

After setup the integration automatically:

1. Uploads 15-minute energy statistics from your HA sensors to Wolta.
2. Triggers periodic recomputes on the Wolta backend.
3. Exposes the results as Home Assistant sensors.

## Sensors

Entity names are translated (English and Swedish bundled; other languages fall back to English), and HA generates entity IDs from the translated names at install time. Default IDs on an English-language instance:

| Entity | Unit | Description |
|--------|------|-------------|
| `sensor.wolta_optimisation_grade` | % | Holistic optimisation score (0–100). Available for all price zones. |
| `sensor.wolta_battery_value_per_year` | SEK / EUR | Average annual battery value. SE zones only. |
| `sensor.wolta_internal_rate_of_return_irr` | % | Internal rate of return on the battery investment. SE zones only. |
| `sensor.wolta_payback_time` | år | Estimated payback period. SE zones only. |
| `sensor.wolta_actual_savings_this_year` | SEK / EUR | Actual battery revenue this year. SE zones only. |
| `sensor.wolta_data_status` | timestamp | Last data point uploaded (diagnostic). Always available. |
| `sensor.wolta_status` | enum | Computation status: `done` / `computing` / `waiting_for_data` / `error` (displayed translated). Always available. |

A **Räkna om** button lets you trigger an immediate recompute outside the automatic schedule.

Economy sensors (battery value, IRR, payback, actual savings) are only available for Swedish price zones (SE1–SE4). For other zones the grade, status and data-status sensors still work.

## Requirements

- Home Assistant 2025.12.0 or newer
- A battery storage system with energy sensors already configured in Home Assistant's Energy dashboard
- Grid import and export sensors (required); solar production sensor (optional)

## Installation via HACS

1. In Home Assistant, open **HACS** (install it first if needed).
2. Go to **Integrations** → three-dot menu → **Custom repositories**.
3. Paste `https://github.com/Seblin01/wolta-homeassistant`, category **Integration** → **Add**.
4. Search for **Wolta** and click **Download**.
5. Restart Home Assistant.
6. Go to **Settings** → **Devices & Services** → **Add Integration** → search **Wolta**.

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
- Battery purchase price and purchase date — used for IRR, payback and "facit i år". Clearing a field removes the value.

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

A ready-made Lovelace view for the Wolta sensors is available in `dashboards/wolta.yaml`.

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

During a recompute (e.g. after changing values), sensors keep their last known values instead of flickering to `unavailable`; retained attributes carry a `beraknar: true` flag and `sensor.wolta_status` shows **Computing** (`computing`) until the new results land (v0.4.2+).

## License

MIT – see [LICENSE](LICENSE).
