## New – capacity hint when your entered battery capacity looks too high

Triggered by [issue #1](https://github.com/Seblin01/wolta-homeassistant/issues/1): the grade
sensor can now flag a mismatch between the battery capacity you entered and what the measured
data actually shows.

- **New `capacity_hint` attribute** on the optimisation grade sensor. The backend sets it when
  the capacity you entered is clearly higher than the usable capacity the measured data supports
  — a classic nameplate-vs-usable mix-up (e.g. entering the rated capacity instead of the usable
  one, or not accounting for a SoC floor/reserve).
- **Shape**: `{entered_kwh, observed_usable_kwh, suggested_kwh}` — what you entered, what the
  data shows is actually usable, and a suggested value to enter instead.
- **Only appears when there's a real mismatch.** No hint, no attribute — the key is omitted
  entirely (not set to `null`), so existing automations and dashboards checking for its presence
  keep working unchanged.
- **How to act on it**: go to Settings → Devices & services → Wolta → Configure, and enter the
  *usable* capacity (`suggested_kwh` or your own better estimate) in the battery capacity field.
  This triggers a server-side regrade with the corrected value.
- **Requires the plan-37 backend.** Older wolta.se backend responses simply don't include the
  field — the attribute is absent, no error, no broken sensor.

## Changed – clarified capacity and efficiency field help texts

- **Usable battery capacity**: the field description now explicitly calls out that batteries with
  a SoC floor or reserve (e.g. Emaldo) have less usable capacity than their rated/nameplate figure
  — a common source of the mismatch the new `capacity_hint` attribute detects.
- **Round-trip efficiency**: clarified that the value should be measured on the AC side (energy
  out ÷ energy in, at the meter), not the DC/cell figure some manufacturer apps report.
- Both texts are updated in the setup flow and the options flow (gear icon → Configure), in
  English and Swedish.

**Why?** Entering nameplate capacity instead of usable capacity is one of the most common setup
mistakes, and it quietly distorts the optimisation grade and economy figures without any error
being raised. The new hint catches it after the fact from measured behaviour; the clearer field
texts aim to prevent it in the first place.
