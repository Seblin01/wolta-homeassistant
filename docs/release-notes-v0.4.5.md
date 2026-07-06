## Fixat

- **Statistik normaliseras till kWh.** Integrationen hämtar nu recorder-statistik med `units={"energy": "kWh"}`. Tidigare laddades värden upp i sensorns egen enhet – en sensor som statistikförs i Wh gav 1000× för stora värden, som avvisades av wolta.se (422) och gjorde att driftdata aldrig kom fram. Har du en ansluten sensor men aldrig fått något betyg: uppdatera till denna version.
- **Enstaka spikrader stoppar inte längre hela uppladdningen.** Rader där något värde överstiger 500 kWh/kvart (t.ex. en mätarreset-spik) droppas klientsidigt med en varning i HA-loggen, istället för att hela batchen avvisas av servern.
- **Null-säkra exempeldashboards.** Markdown-kortet i `dashboards/wolta.yaml` och `dashboards/wolta.sv.yaml` spammar inte längre HA-loggen med template-fel om man valt fel språkvariant – det visar en hänvisning till rätt fil istället.
