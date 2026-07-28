# v0.22.0 – grade nets battery wear, payload goes period-first

### What changes

**Optimisation grade.** The benchmark wolta.se compares your operation against, a
perfectly foresighted controller, priced in a wear cost while deciding how to run the
battery, but reported its gross result, same as your side. A plant that cycled harder
than was actually profitable could "beat" the ideal controller without being better
optimised. The grade now subtracts wear on both sides. Scores can drop a little as a
result, most noticeably for plants that cycle a lot.

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

This is a breaking backend change. Run this integration version against an older
backend and it behaves as before. Run an older integration version against the new
backend and the payload it expects is missing the renamed fields, so the battery value
per year sensor goes to `unknown` until you update. That is intentional: an unknown
sensor is easier to notice than a quietly wrong number.
