# v0.18.1 – no annualised battery value from immature data

### What changes

**Battery value per year** no longer shows a measured figure until the plant's
optimisation grade exists (at least 30 days of data — the same maturity bar as the grade
sensor).

Before this fix, a plant with only a few days of history could show a "measured" yearly
battery value that was really those few days multiplied up to a year — three summer days
× 122 presented as an annual figure, with the `source` attribute claiming `measured`.
Found on a 3-day-old plant the day after v0.18.0 shipped.

The modelled fallback (the decision engine's battery share) is unaffected and still shows
when available; the `source` attribute now tells the truth in that state (`modelled`).
With neither a mature grade nor a modelled value, the sensor is unavailable — consistent
with every other Wolta sensor on a young plant.
