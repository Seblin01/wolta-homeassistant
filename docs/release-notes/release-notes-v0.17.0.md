# v0.17.0 – linking stops early when a plant already streams another way

### What changes

The "link an existing profile" flow now checks how the plant already gets its data. If the
plant streams to wolta.se through another connection — the Sonnen webhook or a Reduxi
bridge — the flow stops at the token step with a clear explanation, instead of asking you
to pick Home Assistant sensors.

### Why

Completing that flow used to be possible, and it would have made two sources write the same
hourly rows on wolta.se: the existing connection and Home Assistant, each with its own
meters. The last writer would win hour by hour, so the stored data — and the optimisation
grade computed from it — would silently become a mixture of both.

If you want Home Assistant to stream instead, disconnect the other connection on your plant
page on wolta.se first, then link the profile here.

The server refuses these cases too (wolta.se API v0.42.0), so older versions of the
integration are also stopped — this release simply gives you the real explanation instead
of a generic error.

### Nothing else changes

Creating new profiles, linking web-created or HA-streamed profiles, re-authentication and
all sensors behave exactly as in v0.16.0.
