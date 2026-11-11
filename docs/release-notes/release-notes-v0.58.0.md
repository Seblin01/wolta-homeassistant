# v0.58.0 - Your own battery values

## What changed

- Own battery values: when Wolta uses its own-values rule, Configure → Settings has an optional section *Your own values* with capacity, power, round-trip efficiency (in %) and reserve. Leave a field empty and Wolta uses the measured value. Your capacity, power and efficiency affect only your own grade; your reserve also applies to the comparison. Clearing a field that has values for earlier stages asks for confirmation first.
- The grade sensor now follows wolta.se: the window grade if you set a window, otherwise your own grade, otherwise the comparison grade. New attributes: `basis` (`window`, `own` or `comparison`), `on_own_values`, `measured_score`, `in_comparison` and `comparison_reasons`. The reason codes are listed in the sensor documentation.
- The battery section of the form asks only for the nameplate values under the new rule. The long texts about AC and DC measuring, usable against nameplate capacity and the reserve counted twice are gone, because the own-values fields replace them.
- A recreated profile is sent without battery values under the new rule, so that Wolta measures them again instead of storing the measured figures as your own.

## What you need to do

Nothing.

## New diagnostic sensors

Four diagnostic sensors show the battery values Wolta calculates with: battery capacity, battery power, round-trip efficiency and reserve. Capacity and efficiency are enabled by default; power and reserve are not. Each has the attributes `state`, `source`, `period_start` and `period_end`, and shows Wolta's measured value when you have set your own.

## Repairs

- The offers to adopt the measured capacity, power or efficiency are not raised under the new rule, and open ones are removed.
- A new repair, *Wolta needs your battery's nameplate power*, asks for the nameplate power when Wolta lacks it.

Works with the current Wolta server and switches over by itself when Wolta changes its calculation rule.
