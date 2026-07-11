# v0.11.2 – Hint that a future purchase date belongs in the decision calculator

Documentation-only patch, no functional changes.

The purchase-date field feeds the "actual savings this year" calculation, which is
measured against real historical electricity prices. A date in the future therefore has
no outcome to measure yet — the grade and economy sensors only reflect what has actually
happened so far.

The field help text now says so, in both the initial setup step and the Configure
dialog's Economy section: if the date is a planned purchase, use the decision calculator
on wolta.se instead, which projects profitability forward. English and Swedish.

(Home Assistant config forms can't show a live, value-dependent warning, so this is a
static hint on the field rather than a conditional one — the matching web form does the
conditional version.)
