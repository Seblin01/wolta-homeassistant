# v0.11.0 – Web edits show up in Home Assistant within minutes

Follow-up to v0.10.0's shared profile: changes made on wolta.se previously reached
Home Assistant at the next main poll (up to 6 hours). The integration now runs a
lightweight profile side-poll every 5 minutes; when it detects a web-side change it
mirrors the new values immediately and refreshes the results, so the recomputed grade
lands in your sensors within minutes of editing on the website.

- The side-poll is a single-row `GET /profile` — negligible load, and it pauses while
  the integration is already fast-polling during an ongoing server computation.
- No configuration changes, no new entities. Requires backend v0.19.0+ (unchanged).
