## New – optional state-of-charge (SoC) sensors

Wolta is preparing to measure your battery's real capacity from energy and change in state of charge (SoC). This release only collects the SoC readings. Nothing uses them yet, and they affect neither your grade nor the economics. If your battery or inverter exposes a SoC sensor, you can already pick it so the history is there when the measurement arrives.

This release adds an optional **Battery state of charge** field in the setup flow (Step 1) and
in the **Reconfigure** menu. Point it at up to 16 sensors — any with `state_class: measurement`
and `unit_of_measurement: %` — and the integration will upload their statistics to Wolta along
with your energy data:

- **History older than 9 days:** hourly min/max/average
- **The last 9 days and ongoing:** 15-minute min/max/average

The data is only stored for now: it is not used for the grade, the economics or any figure you see. It is deleted together with your energy data, which on removal of the integration happens only for profiles the integration created. Linked wolta.se profiles are kept (see Privacy in the README). Each sensor is labelled with its entity ID and integration; the server stores the sensor's identifier as a keyed hash and the label as text.

**Requires Wolta API 0.93.0 or newer.** Older servers silently ignore the SoC fields. Adding a SoC sensor uploads its history (up to 365 days) with the next regular upload; your energy data is not re-sent. Removing one just stops collecting it. If you add no SoC sensor, nothing changes: nothing is uploaded differently.
