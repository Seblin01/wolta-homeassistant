# v0.48.0 – security update for reconnecting and the Visit link

Works together with the security update on wolta.se (17 October 2026).

- **Reconnecting a plant that belongs to a wolta.se account now asks for a token.** wolta.se no longer issues a new token for an existing plant from the plant id alone when a wolta.se account owns the plant. If Home Assistant needs to reconnect such a plant, the integration now asks you to paste a token. Create it on wolta.se: log in, open the plant and choose *Create new token*. The token is checked against this plant before it is saved.
- **Older entries get a new plant id.** Entries created before v0.16.0 used Home Assistant's entry id as the plant id, and that id is visible in the address bar. On setup they now switch to a random id. Nothing changes for you, and your history stays.
- **The Visit link no longer puts the link in the address.** The link opens `wolta.se/anlaggning#link=…`. The part after `#` is never sent to a server, and wolta.se swaps it for a secure cookie as soon as the page opens. Old links keep working.
