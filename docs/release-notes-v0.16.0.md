# v0.16.0 – your plant keeps its identity when you set it up again

### What changes

The integration now sends wolta.se a stable, random identifier for the plant it manages. The
backend never sees it in the clear beyond the request itself: it is salted and hashed into a
"plant fingerprint" and only ever used to recognise a plant it has already seen.

Two things get better as a result:

- **Re-authentication keeps your history.** If your profile ever needs re-authenticating,
  wolta.se can now tell that it is the same plant instead of quietly starting an empty one.
- **The public statistics stop double-counting.** wolta.se compares your grade against the
  median of all shared plants. A plant that was set up twice used to appear as two plants —
  once with a full history, once nearly empty — which dragged that median around.

### Nothing to do

Existing installations are unaffected and need no action. The identifier is created when a new
entry is set up; entries created before this version derive theirs from their own config entry
id the first time they re-authenticate, and store it from then on.

### Note on the identifier

It is a freshly generated 128-bit random value, not your Home Assistant instance id and not
anything derived from your entity names. It identifies one Wolta config entry and nothing else,
and it is not reused across integrations.

Removing and re-adding the integration produces a new identifier. For an entry Home Assistant
created, that is the whole story — removing it also deletes the profile on wolta.se, so there is
nothing left to recognise. For a profile you linked from wolta.se the profile deliberately
survives removal (it is yours, not ours to delete); re-linking it sends the new identifier and
wolta.se rebinds the same plant to it, so your history stays with you either way.

Requires wolta.se API v0.38.0 or later.
