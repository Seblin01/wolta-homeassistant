# v0.37.1 – control-system suggestion now sees Riemann-sum helpers

### What changes

**The control-system suggestion in setup works for the most common sensor setup.**
Many Energy-dashboard battery sensors are Home Assistant *Riemann sum integral*
helpers (kWh integrated from a power sensor). Their integration domain is
`integration`, which no control system maps to, so the suggestion almost never
lit up for exactly the setup most people have. The integration now follows such a
helper one hop to its source sensor and reads *that* sensor's integration instead.
One hop only — a helper built on another helper still gives no suggestion. The
field remains a suggestion you confirm; nothing is chosen for you.

**Three more integrations are recognised on the Wolta side:** CheckWatt
(`checkwatt`), Greenely (`greenely`) and Emaldo (`emaldo`), read from the
respective integrations' manifests. Pixii and Reduxi have no Home Assistant
integration (Reduxi reaches HA over MQTT, and the generic `mqtt` platform is never
mapped), so they stay unsuggested.

**A malformed battery stamp no longer interrupts the main upload cycle.** The
5-minute side-poll already ignored a malformed `battery_status` row from the
server; the 6-hour main cycle did not, so a display field could abort a whole
upload cycle. Both paths now read the stamp atomically and ignore a bad one.

**The efficiency field's description says it is prefilled.** In *Configure →
Battery*, the round-trip efficiency is rendered with the value stored at Wolta.
The description now says so, so the number is not mistaken for a suggestion to
fill in.

### Notes

- No grade change, no change to sensors or entry data. Update via HACS and restart
  Home Assistant.
