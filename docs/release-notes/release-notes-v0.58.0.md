# v0.58.0 - Your own battery values

## What changed

- Own battery values: when Wolta uses its own-values rule, Configure → Settings has an optional section *Your own values* with capacity, power, round-trip efficiency (in %) and reserve. Leave a field empty and Wolta uses the measured value. Your capacity, power and efficiency affect only your own grade; your reserve also applies to the comparison. Clearing a field that has values for earlier stages asks for confirmation first. A value equal to Wolta's own is not stored by Wolta; the form leaves it out and tells you after saving, naming what Wolta's value is (measured, the measurement from before the battery's latest change, the datasheet's or assumed). A reserve equal to the floor Wolta has worked out from the state of charge is sent and stored, because your reserve says the level is a setting in the battery.
- The grade sensor now follows wolta.se: the window grade if you set a window, otherwise your own grade, otherwise the comparison grade. The attribute `basis` gains the value `own` (it already had `window` and `comparison`). New attributes: `on_own_values`, `measured_score`, `in_comparison` and `comparison_reasons`. Of these, `on_own_values`, `in_comparison` and `comparison_reasons` appear on the grade sensor right away, before Wolta switches rule (`in_comparison` and `comparison_reasons` also on the comparison grade sensor); `measured_score` and `basis: own` come with the switch. The reason codes are listed in the sensor documentation.
- Under the new rule the attribute `preliminary` follows wolta.se: your own grade and the comparison grade are also preliminary while the battery's capacity is not measured yet, or not since the battery's latest change (an expansion or a replacement).
- When Wolta switches rule, the grade sensor's number can step, because it then shows your own grade instead of the comparison grade if you have own values. The step is visible in the history.
- When Wolta switches rule, the grade sensor no longer carries `measured_capacity_kwh`, `measured_power_kw`, `measured_efficiency` or their `_status` attributes.
- The battery section of the form asks only for the nameplate values under the new rule. The long texts about AC and DC measuring, usable against nameplate capacity and the reserve counted twice are gone, because the own-values fields replace them.
- A recreated profile is sent without battery values under the new rule, so that Wolta measures them again instead of storing the measured figures as your own.

## What you need to do

Nothing, unless a template or automation reads `measured_capacity_kwh`, `measured_power_kw`, `measured_efficiency` or their `_status` attributes from the grade sensor. After Wolta switches rule, read the new diagnostic sensors below instead.

## New diagnostic sensors

The sensors appear when Wolta switches rule. Four diagnostic sensors show the battery values Wolta calculates with: battery capacity, battery power, round-trip efficiency and reserve. Capacity and efficiency are enabled by default when first created; power and reserve are not. Each has the attributes `state`, `source`, `period_start` and `period_end`, and shows Wolta's own value (measured, the measurement from before the battery's latest change, from the datasheet or assumed; `measured_state` and `measured_source` say which) when you have set your own. The codes are listed in the sensor documentation.

## Repairs

- The offers to adopt the measured capacity, power or efficiency are not raised under the new rule, and open ones are removed.
- Already before the switch: the offer to adopt the measured round-trip efficiency is not raised, and an open one is removed, when Wolta reports the efficiency as a balance between two full charges (`basis: full_to_full`). That figure is measured on the battery side and is not the round-trip the grade uses.
- A new repair, *Wolta needs your battery's nameplate power*, asks for the nameplate power when Wolta lacks it.

Works with the current Wolta server and switches over by itself when Wolta changes its calculation rule.
