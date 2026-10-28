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
