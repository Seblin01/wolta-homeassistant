# v0.12.1 – Field help points to staged battery and solar cost editing on wolta.se

The battery purchase-price field asks for the battery only, and that hasn't changed — the
integration keeps a single battery cost so the grade and the battery IRR stay incremental
(the battery's own return, not the whole plant's). What's new is that wolta.se now lets you
record two things this form deliberately doesn't, and the field help now says so.

**Staged battery purchases.** If you bought the battery in steps — installed one size, then
expanded later — you can enter each install and expansion (date, resulting capacity, cost) on
your wolta.se profile. Wolta then values each period at the capacity you actually had then,
instead of applying today's size to your whole history.

**Solar cost and the whole-plant view.** Enter your solar system's purchase price separately
on your profile and Wolta shows the combined investment for the whole plant (battery + solar)
alongside the battery-only figure.

Both live on the web because they need per-row and combined views that don't fit a simple
options form — the integration stays a single battery cost plus a pointer to wolta.se, exactly
as before. The battery purchase-price help text (English and Swedish) now mentions both.

No action needed. Nothing in Home Assistant changes; this release only updates the on-form
help text.
