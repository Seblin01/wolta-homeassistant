# v0.24.0 – percent-of-spot tariff components

### What changes

**Two new optional tariff fields.** Several Swedish grid operators (southern Sweden
in particular, after E.ON's regional-grid pricing change in 2026) now charge the
variable part of the grid fee as a *percent of the spot price* on top of a fixed
öre part — and some express export compensation the same way. The integration can
now pass both to wolta.se:

| Field | Meaning |
|---|---|
| Grid fee, percent of spot | Percent of spot added to your grid fee (import side, before VAT), 0–100 % |
| Export compensation, percent of spot | Percent of spot added to your export compensation, −100–100 % |

Both are available in the setup flow and under **Configure → Tariffs**, and both
are optional: leave them blank if your tariff is öre-only. Clearing a field
reverts to the country default, exactly like the existing öre fields.

A percent component scales the hourly price spread instead of shifting it, so it
changes *when* optimal control should act — with these fields set, the grade
benchmarks your control against the same rulebook your optimizer is playing by.
Example (Skånska Energi's published model): fixed part 20 öre/kWh + 5.61 % of spot.

### Notes

- Requires wolta.se backend api 0.55.0+ (live since 2026-07-31). Against an older
  backend the fields would be accepted but ignored, so the grade would silently keep
  using the country default — not a concern in practice, since wolta.se is the only
  deployment and is always current.
- Setting or clearing a field triggers a grade recompute, like the öre fields.
- The `applied_tariff` attribute on the grade sensor carries the new keys
  (`grid_var_pct`, `export_extra_pct`) automatically once the grade is recomputed.
- No breaking changes. No action needed after upgrading.
