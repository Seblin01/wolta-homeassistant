# v0.49.0 – a warning when the grid fee looks too high

Works together with Wolta API 0.110.0.

- **New repair: "Wolta: your grid fee looks too high".** Several plants have entered a grid fee that includes VAT and energy tax. Wolta adds both itself, so the energy tax was counted twice, in the grade and in the economics. When the server finds your grid fee above its plausibility limit (60 öre or cents per kWh, higher than any variable grid fee we have verified), Home Assistant now raises a repair that explains this. Correct the figure under Configure → Tariffs, or leave it if your network operator really charges that much. The figure is used either way, and the repair is only a warning.
- **The grid fee field now says "excluding VAT and energy tax".** It used to say only "excluding VAT".
