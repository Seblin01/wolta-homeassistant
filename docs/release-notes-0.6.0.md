## New – configure your own grid fee, retailer markup and export premium

Triggered by [issue #1](https://github.com/Seblin01/wolta-homeassistant/issues/1): a plain HA
user had no way to tell the integration their actual grid fee or export deal, so every grade and
economy figure was computed against the 2026 Swedish default tariff.

- **Three new optional fields**, available both when you first set up the integration and later
  in its options (gear icon → Configure): grid fee (öre/kWh), retailer markup (öre/kWh) and
  export premium (öre/kWh, can be negative or positive on top of spot + grid benefit).
- **Leave them blank and nothing changes** — the standard Swedish tariff default is used, exactly
  as before.
- Set a field later and leave it blank again to clear it back to the default at any time.
- The **optimisation grade sensor** now shows which tariff was actually applied as an attribute
  (`applied_tariff`): the source (`user` or `country_default`) plus the grid fee, markup and
  export premium values used in the calculation — so you can confirm your own numbers took effect.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
