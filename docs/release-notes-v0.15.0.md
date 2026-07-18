# v0.15.0 – device link goes to the plant hub + nameplate power field

### "Visit" link now opens your plant hub

wolta.se reorganised its pages in July 2026: everything about *your stored plant* — the
grade, economy, expansion calculator and all editing — now lives in a dedicated hub at
`/anlaggning`, while `/optimeringsbetyg` is the public landing page. The integration's
device link ("Visit" on the Wolta device page) previously pointed to the old address and
relied on a server-side redirect; it now goes straight to the plant hub with your profile
token, landing you on the overview of your own plant.

### Nameplate power (kW) field

Parity with the nameplate capacity field from v0.13.0: the setup flow and the Configure
dialog (Battery section) now accept an optional **nameplate power** — the
manufacturer-rated kW figure. The grade itself is unchanged and always uses the
deliverable AC power you enter in the regular power field; the rated figure lets wolta.se
explain the difference when the power measured at your meter is lower than the brochure
number (inverter losses, derating). Optional and clearable, synced with your wolta.se
profile like every other field — set or clear it on either side and the other follows.

Requires nothing new on the backend side — the field has been live on wolta.se since
2026-07-15.
