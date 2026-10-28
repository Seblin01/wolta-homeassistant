# v0.10.0 – One profile, two views

The integration and wolta.se now share **one profile**. The server is the source of truth; Home Assistant and the website are just two views of it.

## New

- **Link an existing wolta.se profile.** Setup now starts with a choice: create a new profile, or paste your personal wolta.se link (`?profile=…`) or token to adopt the profile you already built on the website — CSV history included. The profile is converted for integration use server-side (battery profiles only; solar-only profiles are rejected with a clear error) and Home Assistant takes over data uploads from there.
- **Continuous profile sync.** Values changed on wolta.se (capacity, efficiency, economy, tariffs, reserve floor, zone) show up in Home Assistant automatically; the Configure dialog always opens with the current server values and only genuinely changed fields are sent back. No more silent drift between the two.
- **Reconfigure energy sensors** without removing the integration (three-dot menu → Reconfigure). The full history is re-uploaded from the new sensors and the grade recomputed.
- **Smarter setup prefill:** the price zone is preselected from your Home Assistant country (and latitude within Sweden), the round-trip efficiency is prefilled with your plant's *measured* AC value when enough battery history exists, the purchase date is suggested from the first recorded data point, and swapped charge/discharge sensors are detected up front instead of surfacing later as an inverted grade.
- **Diagnostics support** (profile token redacted) to make GitHub issue reports actionable.
- **Repair issue when the profile hits its storage cap** (about 2.3 years of 15-minute data) instead of failing silently in the log.

## Changed

- The Configure dialog is grouped into **Battery**, **Economy** and **Tariffs** sections, and the setup flow now asks for energy sensors first.
- **Removing the integration never deletes a linked (web-created) profile** — it only disconnects Home Assistant. Profiles created by the integration keep the documented delete-on-remove behaviour.

## Requires

- Wolta backend v0.19.0+ (tariff fields and sharing flag exposed on the profile endpoint, plus the profile adopt endpoint used by the link flow).

## Upgrade notes

No action needed. Existing entries behave as before (they are HA-created, so delete-on-remove still applies). The first poll after upgrading records your current sensor selection and invert flag; syncing starts immediately.
