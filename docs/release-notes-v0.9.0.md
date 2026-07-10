## New – reserve floor field

The grade benchmark can now be told about a SoC reserve your control system keeps, so it
compares you against the same window you actually operate in.

- **New `reserve_pct` field** in the setup flow and in Configure (Settings → Devices &
  services → Wolta → Configure): "Reserve floor (%)" — the share of usable capacity your
  control system never discharges below (e.g. a backup reserve). Optional; leave it blank
  if you don't reserve any capacity.
- **Why**: without this, a deliberate reserve looked like wasted capacity to the grade
  benchmark and quietly dragged the score down, even though never touching that last slice
  of charge was the correct, intentional behaviour.
- **How to set it**: enter your battery's *usable* capacity in the capacity field as before,
  then the reserve percentage separately in the new field — the reserve is subtracted from
  that usable figure on the backend, so don't subtract it yourself in the capacity field too.
- **New `applied_reserve` attribute** on the optimisation grade sensor. Shape:
  `{reserve_pct, effective_kwh}` — the reserve percentage actually applied and the resulting
  effective (post-reserve) capacity used in the calculation. Set only when a reserve is
  configured and a grade has been computed with it; otherwise the attribute is omitted
  entirely (not set to `null`), so existing automations checking for its presence keep
  working unchanged.
- **Clearable**: clearing the field in Configure removes the reserve — the grade goes back
  to treating the full usable capacity as available.
- **Requires the plan-38 backend.** Older wolta.se backend responses simply don't include
  the field — the attribute is absent, no error, no broken sensor.

**Why?** The optimisation grade is a benchmark against a perfect dispatch on the capacity
window you actually use. A backup reserve is a deliberate, permanent constraint, not a
missed opportunity — the grade should be measured against the same window you're actually
allowed to use, not the full nameplate-minus-usable figure.
