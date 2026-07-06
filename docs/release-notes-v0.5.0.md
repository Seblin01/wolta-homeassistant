## Ändrat – batterivärdet visar nu batteriets värde (BRYTANDE semantik)

**Tre sensorer byter innebörd. Värdena blir lägre än tidigare – de gamla var för höga.**

- **Batterivärde per år** visade tidigare hela anläggningens årsbesparing (sol + batteri)
  under batterietikett. Nu visas batteriets **uppmätta** värde ur betygsberäkningen –
  samma siffra som wolta.se visar under "Du fångade". Saknas betyg används
  beslutsmotorns modellerade batteridel. Attributen `source` (`measured`/`modelled`)
  och `plant_total_sek` (anläggningstotalen) följer med.
  Bonus: sensorn fungerar nu även utanför Sverige (betyget är fleralands, ekonomin i
  profilens valuta).
- **IRR** och **Återbetalningstid** räknas nu på **enbart batteriets** besparingsström
  mot batteriets inköpspris (inkrementell investeringskalkyl). Tidigare tillgodoräknades
  batteriinvesteringen även solelens värde, vilket gav systematiskt för optimistiska
  nyckeltal för solägare. Kräver wolta.se-backend v0.15.3 (deployad).
- **Ny sensor: Anläggningens besparing per år** (`plant savings per year`) – totalen
  (sol + batteri) som tidigare felaktigt visades som batterivärde. Attributen
  `battery_sek`/`solar_sek` ger uppdelningen.

**Varför?** Batteriets värde definieras som det *inkrementella* värdet mot samma
anläggning utan batteri – solens värde hör till solinvesteringen. Detta följer
etablerad metodik (batteri-retrofitkalkyler, NREL:s solar-plus-storage-analyser,
Solcellskollens svenska beräkningar) och inkrementalkassaflödesprincipen i
investeringskalkyl. Kort sagt: de nya siffrorna är mindre smickrande men sanna.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
