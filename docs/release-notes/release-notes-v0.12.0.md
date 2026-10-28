# v0.12.0 – Wolta measures your grade-affecting battery parameters and offers to adopt them

The optimisation grade compares your operation against a perfect controller **on your own
hardware**, so the battery parameters you enter have to match what the battery really does.
Getting them wrong — usually by entering nameplate figures instead of what the battery
actually delivers at the meter — quietly makes the grade unfair. This release measures those
parameters from your real data and offers to fix them, so you don't have to know
nameplate-vs-usable or AC-vs-DC in advance.

Three grade-affecting inputs are now measured from the meter flows and surfaced as optional,
fixable **repairs** when they clearly disagree with what you configured:

**Usable capacity.** The all-time dispatchable window measured at the meter (what the battery
has demonstrably moved). Adopting it sets the battery capacity and **clears any reserve
floor** — the measurement already reflects only the window you actually cycle, so re-applying
a reserve would reduce it twice. One-click adopt.

**Peak power.** The most power the battery has charged or discharged at the meter. This one is
a *lower bound* — if your control never demanded full power, the real maximum may be higher —
so unlike the others it opens an **editable field pre-filled with the measured value** and
asks you to set the battery's true maximum (its inverter limit). A perfect controller would
use all of it, so a value set too low would flatter your grade and one set too high would
unfairly lower it: you'll probably want to confirm or raise the suggested figure.

**Round-trip efficiency.** The lifetime energy-out ÷ energy-in at the meter — a true
measurement, not a bound — so it's a one-click adopt. This corrects a stale default that could
otherwise linger if the integration was set up before there was enough history to measure the
efficiency.

All three are deliberately conservative: they only appear once there's enough history (at
least 60 days) and the gap is real (capacity/power over ~15 %, efficiency over ~0.08). The
measured values are all-time figures, so they're stable across seasons and won't flip-flop as
more data arrives.

**Clearer field help.** The battery-capacity field now explains, in English and Swedish,
exactly what to enter and why: the usable capacity measured at your meter — not the nameplate
rating and not the DC/cell figure your battery app shows — so the comparison stays fair. This
matches the existing round-trip-efficiency guidance (AC side, not the DC/cell number).

No action needed. Any repair that appears is optional; your existing configuration keeps
working exactly as before until you choose to adopt a measured value.
