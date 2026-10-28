# Privacy

Your 15-minute energy data is stored on Wolta's servers to power the analysis. No personal data (name, address, account) is sent or required.

If you select state-of-charge sensors, their min/max/average values are sent to Wolta along with the energy data: per hour for backfilled history, per 15 minutes for recent data. Adding a SoC sensor uploads its history (up to 365 days) right away; your energy data is not re-sent. Removing a sensor stops collecting it; the last one can only be removed by ticking "My battery has no SoC sensor". Each sensor is labelled with its entity ID and integration (for example "ha:<integration>:sensor.battery_soc"). The server stores the sensor identifier only as a keyed hash, but the label with the entity ID is stored as text. The data is used to measure the battery's capacity and efficiency for the grade and for the direction check described under [State of charge (SoC) monitoring](configuration.md#state-of-charge-soc-monitoring). It is deleted together with the energy data (see below for linked profiles).

**Deleting the integration removes your data server-side** — for profiles the integration created. Removing the config entry in Home Assistant then triggers a right-to-erasure request to the Wolta backend. **Linked profiles are exempt:** removing the integration only disconnects Home Assistant; your wolta.se profile and history stay. Delete those from wolta.se itself.

While one of the *Purchase price*, *Sale price* or forecast price sensors is enabled (they are disabled by default), the integration also reads your plant's computed purchase and sale prices from Wolta every 15 minutes, with the plant token in the `Authorization` header. The request only reads; it changes nothing on the server.

**Every request identifies the integration and its version** (since v0.37.3), as
`wolta-hacs/0.37.4 HomeAssistant/2026.9.3 aiohttp/3.14.3 Python/3.14` in the User-Agent —
the second half is what Home Assistant already sends to every server it talks to. This is
what makes it possible to keep older installations working, or to tell you plainly when
yours has fallen too far behind, instead of leaving you with a bare `405`. It names
software versions and nothing else: no identifier for you, your plant, your tokens or
your data.

Your anonymised optimisation grade is always included in the public corpus comparison — there is no checkbox to opt out of it, and there never actually was one that did anything: the setting used to sit in the plant step, but it did not gate corpus membership or the raw-data retention described above, and it only had one real effect — disabling the expansion calculator (see [v0.37.0 release notes](release-notes/release-notes-v0.37.0.md)). See [wolta.se/om](https://wolta.se/om) for the full privacy policy.
