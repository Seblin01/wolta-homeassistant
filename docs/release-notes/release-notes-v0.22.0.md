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

**The battery value stays gross, the grade goes net.** These two sensors deliberately use
different wear conventions, so it is worth being explicit. `batterivarde_ar` (battery value
per year) reports the measured battery value **before** the wear deduction, the same basis
it has always used; the wear is available separately as the `measured_wear_sek` attribute
on the grade sensor if you want to subtract it yourself. Keeping the sensor on gross
preserves the continuity of your long-term statistics for that entity and matches the
modelled fallback it drops back to, which is also a gross figure. The optimisation grade
itself, on the other hand, is now net on both sides as described above.

**Comparing the sensor with the website.** This needs a little care, because the two do not
always show the same *unit*. The sensor is always SEK/year: `measured_period_sek` ×
`annual.factor`. The "Du fångade" figure on wolta.se follows the length of your upload
instead — below 365 days (`annual.basis` = `"extrapolated"`) it is a **sum in kr for the
uploaded period**, not a yearly figure at all, and only from 365 days (`basis` =
`"measured"`) does it become a measured kr/year average. So the figure that is directly
comparable to the sensor is:

- **below 365 days:** the greyed-out line under the two cards, "Ditt fångade värde uppräknat
  till helår blir ungefär X kr/år";
- **from 365 days:** "Du fångade" itself.

That figure is net of wear while "Räkna med batterislitage" is ticked, which is the default,
so it sits `measured_wear_sek` × `annual.factor` below the sensor. Untick the box and it
matches the sensor exactly.

A worked example on a 120-day plant (`measured_period_sek` 1123, `measured_wear_sek` 210,
`annual.factor` 3.044): the sensor reads 1123 × 3.044 = **3418 kr/year**. The website shows
"Du fångade **913 kr**" — the period sum, net of wear — and the line below it puts the yearly
projection at **2779 kr/year**. The 639 kr between 2779 and 3418 is the wear; the rest of the
distance down to 913 kr is the unit, not a disagreement.

**Grades below zero are published as-is.** Because the grade now subtracts wear from both
sides, a plant that cycles harder than is profitable can score below 0 % — that means it
did worse than a fully passive battery. The sensor publishes the negative value unchanged
rather than flooring it at 0, so automations and statistics keep the sign and you can see
how far below the baseline it is. wolta.se takes the opposite approach and refuses to show
a negative grade as a number at all, displaying an "under baslinjen" state instead, so the
same plant looks different in the two places by design. Values slightly above 100 % are
also passed through (the backend caps the underlying ratio at 1.05).

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
