# v0.25.0 – don't nudge you to raise power to an impossible value

### What changes

**The "check your battery's maximum power" repair no longer keeps coming back with
an impossible figure.** The measured peak power comes from your meter flows, and a
sensor that occasionally jumps in Home Assistant's cumulative statistics can push
that figure far above what the hardware can physically deliver — e.g. a 9.9 kW
battery "measured" at 27.5 kW. The old repair took that at face value and asked you
to raise your (correct) value to it, then reappeared every recompute when you set
the real number back.

Two fixes:

- **Physical ceiling.** A battery can't sustain charge/discharge much above its
  inverter power, so the repair now suppresses an *increase* suggestion when the
  measured peak exceeds your declared power (nameplate if set, otherwise your
  configured power) by more than 50 % — that's a data artefact, not a real
  under-declaration. A genuine, physically plausible mismatch still surfaces, and a
  suggestion to *lower* the value is unaffected.

- **An ignore option.** The repair now opens on a small menu: **Set the maximum
  power** (the same editable field as before) or **Ignore and keep my value**.
  Ignoring is remembered, so the nudge stops for good. If you later adopt a value
  through the repair, the ignore is cleared automatically so a future genuine
  mismatch can still reach you.

### Notes

- No backend change and no configuration change — this only affects when the
  Home Assistant repair is shown. Your grade is unaffected (it already used your
  configured power, never the inflated measurement).
- The repair dialog now also restates your configured value and the history length,
  not just the measured peak.
- No breaking changes. No action needed after upgrading beyond the usual HACS update
  and Home Assistant restart.
