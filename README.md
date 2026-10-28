# Wolta for Home Assistant

[![HACS Default](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://hacs.xyz)
[![GitHub release](https://img.shields.io/github/v/release/Seblin01/wolta-homeassistant)](https://github.com/Seblin01/wolta-homeassistant/releases)
[![License: MIT](https://img.shields.io/github/license/Seblin01/wolta-homeassistant)](https://github.com/Seblin01/wolta-homeassistant/blob/main/LICENSE)
[![Validate](https://github.com/Seblin01/wolta-homeassistant/actions/workflows/validate.yml/badge.svg)](https://github.com/Seblin01/wolta-homeassistant/actions/workflows/validate.yml)

Grade how well your home battery is optimised against **day-ahead electricity prices** (Nord Pool / ENTSO-E), directly in Home Assistant. [Wolta](https://wolta.se) analyses your 15-minute battery and grid data and scores how much of your battery's achievable value your operation actually captured. The optimisation grade and measured battery value work across all supported European price zones. Full battery economics (IRR, payback and whole-plant savings) are currently available for Swedish price zones (SE1–SE4).

## What it does

After setup the integration automatically:

1. Uploads 15-minute energy statistics from your HA sensors to Wolta.
2. Triggers periodic recomputes on the Wolta backend.
3. Exposes the results as Home Assistant sensors.

Only complete quarters are uploaded: see [which quarters are uploaded](docs/configuration.md#which-quarters-are-uploaded).

## Sensors

- **Results:** optimisation grade, comparison grade, battery value per year, plant savings per year, IRR, payback time, actual savings this year, data status and computation status, and a **Recompute** button. The optimisation grade and the battery value work in all supported price zones; plant savings, IRR, payback time and actual savings are for SE1–SE4 only.
- **Prices (SE1–SE4, information only):** spot price (enabled by default), and spot price with forecast, purchase price, sale price, and purchase and sale price with forecast (disabled by default).

What each sensor and attribute means: [Sensors](docs/sensors.md).

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
- Confirm the control system (prefilled when Wolta recognises the integration behind your battery sensors — for a Riemann-sum helper, the integration behind its source sensor) and check the charge/discharge direction toggle (preselected when your history looks reversed). The form states plainly what setup means: your 15-minute energy data is stored at Wolta, your anonymised optimisation grade is included in the public comparison, and removing the integration deletes all of it — see [Privacy](docs/privacy.md). There is no separate checkbox for any of this; it isn't optional.
- Submit. **You are not asked for battery capacity, power or efficiency**: Wolta measures them from the data the integration uploads. The status sensor shows *Measuring battery* until then (typically 30 days with a few full charges). If the data cannot yield them — the battery never charges fully, or one stream is empty — a Repair asks for the nameplate values instead; you can also enter them any time under Configure → Settings.

## Documentation

- [Sensors](docs/sensors.md): result sensors, spot, purchase and sale price sensors, and when sensors are unavailable
- [Feeding the prices to EMHASS](docs/emhass.md): an example template sensor
- [Configuration](docs/configuration.md): uploads, the shared profile with wolta.se, adjusting values, correcting the price zone or stored history, account linking and state of charge
- [External control](docs/flex-markets.md): Grid Rewards and similar flex-market services, and flex compensation
- [Troubleshooting](docs/troubleshooting.md): repairs and what to do about them
- [Example dashboard](docs/dashboard.md): a Lovelace view and the Swedish entity IDs
- [Privacy](docs/privacy.md): what is stored and what is deleted
- [Release notes](https://github.com/Seblin01/wolta-homeassistant/releases)

## Links

- [wolta.se](https://wolta.se) – the service
- [wolta.se/om](https://wolta.se/om) – about & privacy
- [GitHub issues](https://github.com/Seblin01/wolta-homeassistant/issues) – bug reports & feature requests

## License

MIT – see [LICENSE](LICENSE).
