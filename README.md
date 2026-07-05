# Wolta for Home Assistant

A Home Assistant custom integration that brings [Wolta](https://wolta.se) battery optimization grades and economy data directly into your Home Assistant instance.

## What it does

Wolta analyses day-ahead electricity prices and your battery/solar setup to calculate how well each day's charging and discharging was executed. This integration exposes that data as Home Assistant sensors so you can track performance over time, build dashboards, and trigger automations.

**Sensors provided:**

| Sensor | Unit | Description |
|--------|------|-------------|
| `sensor.wolta_grade` | A–F | Daily battery optimization grade |
| `sensor.wolta_score` | % | Numeric grade (0–100) |
| `sensor.wolta_savings` | SEK/EUR | Estimated savings vs no optimization |
| `sensor.wolta_next_grade` | A–F | Forecast grade for tomorrow |

*Sensor details are filled in as the integration develops (v0.2+).*

## Requirements

- Home Assistant 2025.12.0 or newer
- A free [wolta.se](https://wolta.se) account with an API token
- A battery, solar, or combined energy setup already configured in Home Assistant's Energy dashboard

## Installation via HACS

1. In Home Assistant, open **HACS** (install it first if needed).
2. Go to **Integrations** → three-dot menu → **Custom repositories**.
3. Paste `https://github.com/Seblin01/wolta-homeassistant`, category **Integration** → **Add**.
4. Search for **Wolta** and click **Download**.
5. Restart Home Assistant.
6. Go to **Settings** → **Devices & Services** → **Add Integration** → search **Wolta**.
7. Enter your API token and select your electricity price zone.

## Manual installation

1. Copy `custom_components/wolta/` into your HA `config/custom_components/` directory.
2. Restart Home Assistant.
3. Add the integration via **Settings** → **Devices & Services**.

## Configuration

The integration is configured via the UI config flow. You will need:

- **API Token** – generate one at [wolta.se](https://wolta.se) under your account settings.
- **Price zone** – your Nordpool/ENTSO-E bidding zone (e.g. SE3, SE4, DK1).
- **Battery size** (optional) – helps Wolta calculate accurate grades.
- **Sensor mappings** (optional) – map your existing HA energy sensors for detailed analysis.

## Privacy

Wolta stores anonymised energy and price data on its servers to calculate grades. Your personal data (name, address) is never sent. Data is deleted when you remove the integration entry from Home Assistant.

See [wolta.se/om](https://wolta.se/om) for the full privacy policy.

## Links

- [wolta.se](https://wolta.se) – the service
- [wolta.se/om](https://wolta.se/om) – about & privacy
- [GitHub issues](https://github.com/Seblin01/wolta-homeassistant/issues) – bug reports & feature requests

## License

MIT – see [LICENSE](LICENSE).
