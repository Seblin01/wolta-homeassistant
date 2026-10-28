# v0.11.1 – Respect Retry-After on rate-limited recomputes

Housekeeping patch, no functional changes for users.

When the 7-day data cadence suggested a recompute while the server-side 24h cooldown
was still active, the coordinator retried on every poll — harmless (the 429 was always
swallowed) but noisy, generating ~20 pointless requests per day per installation.

The coordinator now stores the server's `Retry-After` and stays quiet until it has
passed. A successful recompute — including user-initiated ones from the Configure
dialog or the Recompute button, which clear the server cooldown via PATCH — resets
the backoff immediately, so user-triggered recomputes behave exactly as before.
