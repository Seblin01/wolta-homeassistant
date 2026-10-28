# v0.20.0 — Bearer token transport

The integration now sends your profile token in the `Authorization: Bearer` header
instead of the URL path. Tokens no longer appear in server access logs, proxies or
browser history. No action needed: your existing token keeps working, nothing about
the connection changes. Requires wolta.se API 0.44.0 or later (already deployed).
