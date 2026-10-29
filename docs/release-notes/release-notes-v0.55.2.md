# v0.55.2 – clearer errors when Wolta refuses a setting

When wolta.se refuses a value you save in Home Assistant, the form now says why. Before, some refusals showed "check your internet connection", even though the connection was fine.

- **The purchase date, for a plant built in stages:** if your battery came years after the solar panels, the purchase date is the first stage's date, and the later stages are set on wolta.se. The settings form now says so before saving, at the purchase date field, if you type a later date or clear it. The field's description says it too.
- **Any other refusal:** the settings form, Reconfigure, linking a profile and entering a new token after a reauth show Wolta's own reason instead of a connection error. The reason comes from wolta.se and is in Swedish.
- **A real connection problem** still shows the connection error.
- **Help with the sensors:** each energy sensor picker now says what it wants: a total in kWh that only grows, the same sensor the energy dashboard uses. The text for bought electricity says it should rise every night, so a sensor that has stopped updating is easier to spot.
- **Where the sensors are changed:** the Configure menu now says that the sensors are changed with Reconfigure in the integration's ⋮ menu, not in Settings.

You do not need to do anything. Update when it suits you.
