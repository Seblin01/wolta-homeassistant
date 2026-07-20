# v0.19.0 – preliminary grades show up after a week

### What changes

wolta.se now computes a first, **preliminary** optimisation grade after about 7 days of
data instead of 30 (api v0.43.0). The integration surfaces it:

- **Optimisation grade** shows the preliminary score as soon as the backend has one. Two
  new attributes let dashboards and automations tell the difference: `preliminary`
  (true until the plant has 30 days of data) and `n_days` (how many days the grade is
  based on). A preliminary score can swing from day to day — treat it as an early
  indication, not a verdict.
- **Battery value per year** keeps requiring a *mature* grade for its measured figure —
  a preliminary grade has a real score, but its yearly battery value is still a short
  window multiplied up to a year (the v0.18.1 problem in new clothes). The modelled
  fallback is unaffected.
- **Measured-parameter repairs** (adopt measured capacity/power/efficiency) wait until
  the grade is mature: a week of data systematically underestimates what the battery can
  do, and acting on it would suggest wrong values.

The economy sensors (plant savings, IRR, payback, actual savings) are unchanged — they
still require 30 days, because they are built on annualised absolute figures that do not
tolerate short windows.

Requires wolta.se API v0.43.0 for preliminary grades; with an older server everything
behaves exactly as before.
