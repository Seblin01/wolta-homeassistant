# v0.12.0 – Wolta measures your usable battery capacity and offers to adopt it

The optimisation grade compares your operation against a perfect controller **on your own
hardware**, so the battery capacity you enter has to match what the battery really does.
The single most common mistake is entering the nameplate/rated figure — or the energy the
battery app shows at 100 % state of charge — instead of the *usable* capacity delivered at
the meter. Both are higher than the truth, because the inverter loses energy converting to
and from the battery, and that inflated capacity unfairly lowers the grade.

This release stops that from being something you have to know in advance.

**Wolta now measures it for you.** After a couple of months of history, the backend
reconstructs your battery's usable, dispatchable window from the energy that actually flows
in and out at the meter (the AC side). When that measured value clearly disagrees with what
you configured, a fixable **repair** appears — "Wolta measured a different battery
capacity" — showing the measured figure and offering to adopt it in one click.

Confirming it sets the battery capacity to the measured value and **clears any reserve
floor**: the measurement already reflects only the window you actually cycle, so re-applying
a reserve on top would reduce it twice. The grade and economy are then recomputed on the
corrected, fair basis.

The repair is deliberately conservative — it only appears once the measurement is mature
and confident (at least 60 days of history, a ceiling that recurs across many days, and a
gap larger than ~15 %). The measured value is an all-time figure, so it's stable across
seasons and won't flip-flop as more data arrives. If you simply haven't fully charged or
discharged in the period, ignore and dismiss it.

**Clearer field help.** The battery-capacity field now explains, in both English and
Swedish, exactly what to enter and why: the usable capacity measured at your meter — not
the nameplate rating and not the DC/cell figure your battery app shows — so the comparison
stays fair. This mirrors the existing round-trip-efficiency guidance (AC side, not the
DC/cell number).

No action needed. If a repair appears, it's optional; your existing configuration keeps
working exactly as before until you choose to adopt the measured value.
