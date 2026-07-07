## Fixed

- **Statistics are normalised to kWh.** The integration now pulls recorder statistics with `units={"energy": "kWh"}`. Previously values were uploaded in the sensor's own unit, so a sensor recorded in Wh produced values 1000× too large, which wolta.se rejected (422) and meant operating data never got through. If you have a connected sensor but never received a grade, update to this version.
- **A single spike row no longer stops the whole upload.** Rows where a value exceeds 500 kWh per quarter-hour (for example a meter-reset spike) are dropped client-side with a warning in the HA log, instead of the whole batch being rejected by the server.
- **Null-safe example dashboards.** The markdown card in `dashboards/wolta.yaml` and `dashboards/wolta.sv.yaml` no longer spams the HA log with template errors if you picked the wrong language variant; it shows a pointer to the correct file instead.
