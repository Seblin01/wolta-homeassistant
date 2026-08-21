# v0.28.0 – battery value per year now needs 180 days, not 30

### What changes

**The battery-value-per-year sensor's measured/modelled switch moved from 30 days to
180 days**, matching a backend change already live on wolta.se (server v0.71.1+,
audit "F12" 2026-08-20). Annualising a short window overstates or understates the
real figure — a 32-day summer window read 55 % high on one plant. The backend now
withholds the annualised figure entirely until a plant has 180 days of history;
before that, `sensor.wolta_battery_value_per_year` keeps showing the modelled
fallback (the `source` attribute still tells you which). Nothing to configure —
existing installs pick this up automatically, and a plant already past 180 days
sees no change at all.

**Hardened against a future factor-less payload.** The sensor already degrades to
"no value" when the backend omits the annual figure for an immature window; it now
also degrades cleanly if a future payload were to send an annual block *without* a
factor, instead of raising inside `native_value`/`available`. Defensive only — the
backend never sends that shape today, so this changes nothing on its own.

### Notes

- README corrected in two places that had drifted from actual backend behaviour:
  the battery-value sensor's maturity bar (was documented as 30 days, is 180), and
  the general "sensors need 30 days" note under Notes, which predated the
  preliminary-grade feature (v0.19.0) — most sensors now need only 7 days, with the
  economy sensors needing 30 (grade-dependent ones) or 180 (the annualised battery
  value) as documented per sensor.
- No breaking changes. Update via HACS and restart Home Assistant.
