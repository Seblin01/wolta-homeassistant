# v0.50.0 – the grade measured from your battery's data

Works together with wolta.se switching to the data-driven benchmark. The grade is now computed against the capacity and efficiency Wolta measures from your battery's state of charge, not against the battery details you entered.

- **The grade sensor tells you whether your grade is compared with others.** `sensor.wolta_optimisation_grade` has a new attribute `benchmark` with `comparable` (true or false) and `reasons`. A grade that is not comparable is a grade of your own: it is computed and shown, but it is left out of the comparison with other installations. The most common reason is `no_soc`, meaning no state-of-charge sensor is selected.
- **The battery value sensor stays on the same figure as the economics.** After the switch, the grade can cover a different period than payback and IRR. `sensor.wolta_battery_value_per_year` keeps following the figure the economics are anchored on, so it does not jump when wolta.se switches over. It can therefore differ from the "You captured" figure next to the grade on wolta.se.
- **New text for the state-of-charge field and repair.** They used to say that the state of charge does not change your grade. That is no longer true: without it, the grade is not compared with other installations. The economics still use the battery details you entered.
