## Fixed – the linking-code dialog now describes a path that exists

If you set Wolta up from Home Assistant and did not already have a wolta.se account, the
linking code was a dead end. The dialog told you to "sign in or create an account at
wolta.se" — but an account could only be created from a browser that already held a plant's
owner token, which the Home Assistant setup path never shows you. People followed the
instructions, asked for a login link, and no email ever arrived.

- **The backend now accepts the linking code directly.** Go to wolta.se/konto, enter your
  email address and paste the code in the same form. If you do not have an account yet, one
  is created when you confirm the email, and the plant is linked at the same time.
- **The dialog text was rewritten** to describe that route, and to point people who are
  already signed in to Account → Link plant instead.
- **No code changes in the integration** beyond the translated strings — nothing about how
  the code is minted, how long it lives (10 minutes), or how it is redeemed has changed.

**Requires wolta.se api 0.90.0 or later**, which is live. Against an older backend the form
would reject the code, which is why this release comes after the server side.

**Why?** A dialog that instructs you to do something the product cannot do is worse than no
dialog at all: it makes the user think they typed something wrong. The fix was on the
server; this release stops the text from describing the old, impossible route.
