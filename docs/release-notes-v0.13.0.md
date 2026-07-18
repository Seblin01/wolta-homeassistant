# v0.13.0 – Optional nameplate capacity field

Adds an optional **Nameplate capacity (kWh)** field to the plant setup step and the battery
section of the options form.

**Why.** Battery capacity means two different things: what the manufacturer prints on the
datasheet (nameplate) and what the battery actually delivers to your house on a full discharge
(usable). Some brands market the nameplate figure (a sonnen 10p/22 is "22 kWh" nameplate,
20 kWh usable), others market usable (a Tesla Powerwall 3 is "13.5 kWh" usable). The
integration's capacity field has always meant *usable* — that's what the grade benchmark
plays with, and that's what the measured-capacity Repair adopts.

Wolta's battery expansion calculator ("Is expanding worth it?" on wolta.se), however, prices
capacity the way the market does: per **nameplate** kWh. Without knowing your nameplate figure
it had to assume nameplate = usable, which understates the cost of real deliverable capacity
for batteries like sonnen's. With this release you can record both, and the expansion sweep
compares candidate sizes fairly: prices per nameplate kWh, value from the usable window.

**What to do.** If your battery's rated capacity differs from what it delivers (check your
datasheet), open the integration's options (or your wolta.se profile page) and fill in the new
field. If your battery is marketed by usable capacity, leave it empty — nothing changes.

**Compatibility.** Requires the wolta.se backend from 2026-07-14 or later (API 0.24.0); on
older backends the field is ignored. The usable-capacity field, the grade, and all Repairs
behave exactly as before. The field is optional and cleared the same way as the reserve floor:
empty the field and save.

## Also in this release

### `capex_scope` attribute on the IRR and payback sensors

The backend (2026-07-18) now tells clients which capital base the decision block's IRR and
payback figures are attributed to: `battery` (the battery alone – the norm for HA-created
profiles, whose cost field is battery-only) or `plant` (the whole plant, solar + battery –
profiles created in the wolta.se guide with a combined purchase price and later adopted into
HA). The sensors expose this as a `capex_scope` attribute so automations and dashboards can
label the figure correctly. On older backends the attribute is simply omitted.

### Battery cost field hidden for whole-plant-priced profiles

For adopted wolta.se profiles whose stored purchase price covers the **whole plant**, the
options form's "Battery purchase price – battery only" field no longer applies: editing it
would silently reinterpret a whole-plant figure as battery-only. The field is now hidden for
those profiles (the economy section explains why) – the price is edited on your wolta.se
profile page, where the labels match the stored semantics. Profiles with a battery-only
price (all profiles created in HA) are unaffected.
