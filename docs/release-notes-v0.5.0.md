## Changed – battery value now shows the battery's value (BREAKING semantics)

**Three sensors change meaning. The values are lower than before; the old ones were too high.**

- **Battery value per year** previously showed the whole plant's annual saving (solar + battery)
  under a battery label. It now shows the battery's **measured** value from the grade calculation,
  the same figure wolta.se shows under "You captured". When no grade exists, the decision engine's
  modelled battery share is used. The attributes `source` (`measured`/`modelled`) and
  `plant_total_sek` (the plant total) are included.
  Bonus: the sensor now also works outside Sweden (the grade is multi-country, the economy in
  the profile's currency).
- **IRR** and **Payback time** are now calculated on the **battery-only** savings stream against
  the battery's purchase price (incremental investment appraisal). Previously the battery
  investment was also credited with the value of solar electricity, which gave systematically
  over-optimistic figures for solar owners. Requires wolta.se backend v0.15.3 (deployed).
- **New sensor: Plant savings per year** (`plant savings per year`), the total (solar + battery)
  that was previously shown incorrectly as the battery value. The attributes `battery_sek`/`solar_sek`
  give the split.

**Why?** A battery's value is defined as the *incremental* value versus the same plant without a
battery; the solar's value belongs to the solar investment. This follows established methodology
(battery-retrofit calculations, NREL's solar-plus-storage analyses, Solcellskollen's Swedish
calculations) and the incremental cash-flow principle in investment appraisal. In short: the new
numbers are less flattering but true.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
