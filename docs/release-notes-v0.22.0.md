# v0.22.0 – grade nets battery wear, payload goes period-first

### What changes

**Optimisation grade.** The benchmark wolta.se compares your operation against is a
perfectly foresighted controller. It priced in a wear cost while deciding how to run
the battery, but reported its gross result, the same as your side. A plant that
cycled harder than was actually profitable could therefore "beat" the ideal
controller without being better optimised. The grade now subtracts wear on both
sides. Scores can drop a little as a result, most noticeably for plants that cycle a
lot.

**Battery value per year, payload format.** The grade payload is now period-first: kr
amounts come as window sums under new field names (`measured_period_sek`,
`measured_wear_sek`) plus a top-level `annual: {basis, factor}` block, instead of one
pre-annualised total. Eight July days scaled by 365/8 to a yearly figure
systematically overstated it; the new block makes the annualisation basis and factor
explicit instead of hiding them in the number. The integration multiplies the window
sum by `annual.factor` itself, so the sensor keeps reporting SEK/year and your
statistics history stays valid. Nothing to do there.

The `gap_sek` attribute on the optimisation grade sensor is removed (no longer sent by
the backend). New attributes: `annual_basis`, `measured_period_sek`,
`measured_wear_sek`.

### Requires wolta.se API 0.52.0 or later

This is a breaking backend change, but it does not show up as a hard failure in
either direction. If the backend updates before you update the integration, the old
code cannot find the fields it expects in the new payload and quietly falls back to
the modelled battery-value estimate; the entity stays available, it just stops being
measured. If you update the integration before the backend does, the same thing
happens for the same reason in reverse: the new code requires a top-level `annual`
block that backends older than 0.52.0 never send, so it falls back to modelled too,
even for a plant with a mature grade that used to show a real measured figure.

Either way there is no `unknown` state to warn you. The tell is the `source`
attribute on the battery value sensor, `"measured"` versus `"modelled"`. If it reads
`"modelled"` and you expect a measured figure, check that both sides, the integration
and the backend, are updated.
