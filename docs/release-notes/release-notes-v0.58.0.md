# v0.58.0 - Your own battery values

## What changed

- Own battery values: when Wolta uses its own-values rule, Configure → Settings has an optional section *Your own values* with capacity, power, round-trip efficiency (in %) and reserve. Leave a field empty and Wolta uses the measured value. Your capacity, power and efficiency affect only your own grade; your reserve also applies to the comparison. Clearing a field that has values for earlier stages asks for confirmation first.
- The grade sensor now follows wolta.se: the window grade if you set a window, otherwise your own grade, otherwise the comparison grade. The attribute `basis` gains the value `own` (it already had `window` and `comparison`). New attributes: `on_own_values`, `measured_score`, `in_comparison` and `comparison_reasons`. The reason codes are listed in the sensor documentation.
- When Wolta switches rule, the grade sensor's number can step, because it then shows your own grade instead of the comparison grade if you have own values. The step is visible in the history.
- When Wolta switches rule, the grade sensor no longer carries `measured_capacity_kwh`, `measured_power_kw`, `measured_efficiency` or their `_status` attributes.
- The battery section of the form asks only for the nameplate values under the new rule. The long texts about AC and DC measuring, usable against nameplate capacity and the reserve counted twice are gone, because the own-values fields replace them.
- A recreated profile is sent without battery values under the new rule, so that Wolta measures them again instead of storing the measured figures as your own.

## What you need to do

Nothing, unless a template or automation reads `measured_capacity_kwh`, `measured_power_kw`, `measured_efficiency` or their `_status` attributes from the grade sensor. After Wolta switches rule, read the new diagnostic sensors below instead.

## New diagnostic sensors

The sensors appear when Wolta switches rule. Four diagnostic sensors show the battery values Wolta calculates with: battery capacity, battery power, round-trip efficiency and reserve. Capacity and efficiency are enabled by default when first created; power and reserve are not. Each has the attributes `state`, `source`, `period_start` and `period_end`, and shows Wolta's measured value when you have set your own.

## Repairs

- The offers to adopt the measured capacity, power or efficiency are not raised under the new rule, and open ones are removed.
- A new repair, *Wolta needs your battery's nameplate power*, asks for the nameplate power when Wolta lacks it.

Works with the current Wolta server and switches over by itself when Wolta changes its calculation rule.
