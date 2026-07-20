# v0.18.0 – view-only mode for plants that stream another way

### What changes

Pasting a token for a plant that already streams to wolta.se through its own connection —
the Sonnen webhook or a Reduxi bridge — no longer stops with an error. The flow now offers
to add the plant in **view-only mode**:

- You get all the Wolta sensors (optimisation grade, battery value, plant savings, IRR,
  payback, result status, data status) and the recompute button.
- Home Assistant never uploads anything to the plant: no sensor selection, no data writes.
  The plant's own connection keeps owning the data, exactly as before.
- Removing the entry never deletes anything on wolta.se.

If the saved token ever stops working (for example because you created a new one on
wolta.se), re-authentication simply asks for a fresh token instead of creating a new
profile.

### Background

v0.17.0 blocked these plants outright to prevent two sources writing the same hours. The
block was correct but blunt — there was no way to just *see* such a plant in Home
Assistant. View-only mode is that way.

Requires wolta.se API v0.42.0 or later (the `transport` field and the server-side write
guards it builds on).
