## New – optional state-of-charge (SoC) sensors for battery capacity measurement

Wolta measures your battery's real capacity from its charge and discharge streams, but the
accuracy improves when it can also see the battery's own state-of-charge readings. If your
battery or inverter exposes a SoC sensor (most modern systems do), Wolta can use that to
refine the measurement.

This release adds an optional **State of charge sensors** field in the setup flow (Step 1) and
in the **Reconfigure** menu. Point it at up to 16 sensors — any with `state_class: measurement`
and `unit_of_measurement: %` — and the integration will upload their statistics to Wolta along
with your energy data:

- **Backfilled data** (older than a few days): hourly min/max/average
- **New data** (current week onwards): 15-minute min/max/average

Wolta uses these to characterise your battery's real capacity versus the nameplate rating,
which helps refine the investment case on new plants and track degradation over time. **Your
optimisation grade is unaffected** — the SoC data is measurement input only, and goes nowhere
near the score itself.

Removing the integration deletes all recorded SoC data along with the rest.

**Important:** this feature requires **Wolta API 0.93.0+** (production deployment date TBD).
Older servers silently ignore the SoC data. When you upgrade and add a SoC sensor for the
first time, the integration re-uploads your full history (up to 365 days) on the next cycle
so Wolta has a complete picture from start to finish.
