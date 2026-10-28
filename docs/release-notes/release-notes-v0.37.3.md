## Changed – requests now say which version made them

Nothing user-visible changes in this release. It exists so a question can be answered
that previously could not be: **which versions of this integration are still in use?**

Until now, nothing in a request identified the client. The backend saw an anonymous HTTP
call and could not tell a build from July from today's. That matters because the API does
move: `GET /grade/check-plant`, for example, answers **405 Method Not Allowed** today —
an old enough client would fail against it. Whether anyone actually runs such a version
was unknowable.

- **Every request now carries a User-Agent** of the form
  `wolta-hacs/0.37.3 (+https://github.com/Seblin01/wolta-homeassistant)`.
- **The version is read from `manifest.json`**, not duplicated in the code. A second copy
  drifts the first time someone bumps only one of them, and a User-Agent that misreports
  its version is worse than one that says nothing at all.
- **Your own request headers are untouched** — authentication works exactly as before.

**What is sent:** the integration name and its version number. Nothing about you, your
plant, your tokens or your data. The same string every installation on this version sends.

**Why it matters to you:** it is what makes it possible to keep older installations
working, or to tell you plainly when yours has fallen too far behind — instead of leaving
you with an error that says only `405`.
