# v0.23.0 – measured battery parameters as grade sensor attributes

### What changes

**New attributes on the optimisation grade sensor.** The backend measures three
battery parameters from your actual uploaded meter data (the same figures shown on
the "Measured" facet of the plant page on wolta.se). The grade sensor now exposes
them as attributes:

| Attribute | Meaning |
|---|---|
| `measured_capacity_kwh` | Dispatchable capacity the battery has demonstrably delivered (all-time lower bound, capped at nameplate) |
| `measured_power_kw` | Highest charge or discharge power seen at the meter |
| `measured_efficiency` | Round-trip efficiency, discharged ÷ charged at the meter (AC side), as a fraction |
| `measured_*_status` | One per parameter: `ok`, `immature`, or `unmeasurable` |

The status explains an absent value: `immature` means the data has not reached the
measurement's maturity threshold yet (capacity needs ≥30 days with a stable plateau,
efficiency ≥60 days), `unmeasurable` means the measurement is impossible with your
sensor setup (a net-metering sensor collapses the battery flow, so waiting will not
help). When a value is absent, its attribute is omitted rather than set to null.

These are measured lower bounds, not nameplate specs — a conservatively driven
battery may never have shown its full window. Useful in dashboards and template
sensors to compare what the data shows against what you configured, e.g.:

```yaml
{{ state_attr('sensor.my_plant_optimisation_grade', 'measured_capacity_kwh') }}
```

(The entity id depends on your plant name; the sensor is named "Optimisation grade".)

### Notes

- The value fields have been in the grade payload for a while (they already power the
  "adopt measured value" repair flows); the `measured_*_status` fields require wolta.se
  api 0.51.0 (live since 2026-07-28). A grade cached before that can therefore show
  value attributes without their status attributes until it is recomputed; integration
  profiles recompute automatically within a day. Absent fields simply omit their
  attribute — nothing is set to null.
- This is presentation only — nothing here affects the grade, and the existing
  "adopt measured value" repair flows are unchanged.
- No breaking changes. No action needed after upgrading.
