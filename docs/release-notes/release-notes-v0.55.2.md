# v0.55.2 – clearer errors when Wolta refuses a setting

When wolta.se refuses a value you save in Home Assistant, the form now says why. Before, some refusals showed "check your internet connection", even though the connection was fine.

- **The purchase date, for a plant built in stages:** if your battery came years after the solar panels, the purchase date is the first stage's date, and the later stages are set on wolta.se. The settings form now says so before saving, at the purchase date field, if you type a later date or clear it. The field's description says it too.
- **Any other refusal:** the settings form, Reconfigure, linking a profile and entering a new token after a reauth show Wolta's own reason instead of a connection error. The reason comes from wolta.se and is in Swedish.
- **A real connection problem** still shows the connection error.

You do not need to do anything. Update when it suits you.
