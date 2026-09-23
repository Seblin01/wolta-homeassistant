## Fixed – the version stamp no longer hides Home Assistant's own

v0.37.3 added a User-Agent naming the integration and its version. It did so by
**replacing** the one Home Assistant's shared session already sends, and that turned out
to trade away a fact worth keeping.

Home Assistant identifies itself as `HomeAssistant/2026.9.3 aiohttp/3.14.3 Python/3.14`.
That is how the backend could see which Home Assistant releases are actually in use — the
same question `hacs.json`'s minimum supported version (2025.12.0) exists to answer. One
release of ours made that invisible for anyone who upgraded.

- **Both are now sent**, ours first:
  `wolta-hacs/0.37.4 HomeAssistant/2026.9.3 aiohttp/3.14.3 Python/3.14`.
- **Nothing else changed.** Same information about you as before: an integration name, a
  version number, and what Home Assistant already told every server it talks to.

**If you are on v0.37.3** there is nothing broken to repair on your side — the only thing
lost was a detail in our logs. Updating restores it.

**Why this reached you at all:** the stamp was added to answer whether anyone still runs a
version too old for the current backend. The first hours of real data answered a different
question instead — it showed that the stamp itself had removed something. Measuring is
worth doing precisely because it contradicts you.
