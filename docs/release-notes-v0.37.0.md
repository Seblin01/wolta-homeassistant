# v0.37.0 – the sharing checkbox is gone; setup is now honest about what happens

### What changes

**Step 2 of setup no longer has a "contribute anonymised data" checkbox.** It looked
like a privacy control, but it wasn't one: your anonymised optimisation grade has
always counted in the public corpus comparison regardless of the checkbox (corpus
membership is gated by your plant's own accepted/rejected status, never by this
setting), and your raw 15-minute energy data was never deleted because of it either
— an integration-created plant streams live data, so the series has to be kept for
streaming to work. The checkbox's only real, working effect was on HA-created
plants: leaving it unticked silently disabled the expansion calculator, which then
422'd if you tried to use it. It protected nothing and cost a feature, so it's
removed.

**Every plant is now created — and every reauth recreated — with sharing on.**
There is nothing left to configure: the plant step asks for price zone, control
system and the charge/discharge direction toggle only. In its place, the step's
description now states plainly what setup means: your 15-minute energy data is
stored at Wolta, your anonymised grade is included in the public comparison, and
removing the integration deletes all of it (see [Privacy](../README.md#privacy) in
the README for the full picture, including the linked-profile exception).

**What's stored doesn't change.** The `share` field is still present in
`entry.data` for backward compatibility (reauth and diagnostics still read the
key), but it is always `True` from this version on and is never read to decide
anything any more.

**A plant created with the box left unticked keeps the old setting for now.**
Server-side the flag only changes when the plant's profile is recreated, which
happens during re-authentication — and Home Assistant prompts for that only if the
plant's token stops working. There is no way to flip it from the interface, and
removing and re-adding the integration would delete the plant's data and start a
new plant, so it is not worth doing for this alone. The expansion calculator is the
only thing affected; the grade, the economics and the sensors are unchanged.

### Why

The checkbox implied a trade-off that didn't exist in the code: ticking it bought
you nothing you didn't already have, and leaving it unticked cost you a feature
without protecting any data. That's a UI that lies to the person reading it, and
the fix is to remove the false choice and say what actually happens instead.

### Notes

- No grade change for existing plants — every plant's grade already counted in the
  corpus regardless of this setting, so nothing about the comparison changes.
- If your expansion calculator was disabled, it stays disabled until the profile is
  recreated at a re-authentication. Nothing else about that plant is affected.
- No breaking changes to the sensors or entry data shape. Update via HACS and
  restart Home Assistant.
