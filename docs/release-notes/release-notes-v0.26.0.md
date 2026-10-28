# v0.26.0 – ignore option on all measured-parameter repairs

### What changes

**The capacity and efficiency repairs now have the same "ignore and keep my value"
option that the power repair got in v0.25.0.** All three measured-parameter repairs
(usable capacity, peak power, round-trip efficiency) are derived from your meter
flows, so a battery with the occasional sensor jump in Home Assistant's cumulative
statistics can have any of the three measurements biased to an implausible value —
for example a round-trip efficiency measured at 0.76 for a battery whose real
AC round-trip is nearer 0.90 (spurious charge energy inflates the denominator).

Each repair now opens on a small menu:

- **Adopt the measured value** — the same one-click adopt (capacity, efficiency) or
  editable field (power) as before.
- **Ignore and keep my value** — remembered per parameter, so the repair stops
  reappearing. Adopting a value later clears the ignore automatically.

The repair dialogs also now correctly show your configured value and the history
length alongside the measured figure (previously those placeholders could render
literally in the capacity and efficiency dialogs).

### Notes

- No backend change and no grade change — this only affects when the Home Assistant
  repairs are shown, and lets you dismiss one you don't want to act on.
- Efficiency has no automatic suppression like power's physical-ceiling gate: there
  is no clean physical floor that separates a genuinely low AC round-trip from an
  artefact, so the honest options are to adopt it or ignore it.
- No breaking changes. Update via HACS and restart Home Assistant.
