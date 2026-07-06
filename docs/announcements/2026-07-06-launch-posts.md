# Lanseringsinlägg (utkast 2026-07-06)

Skrivna för Facebook-grupper (HA Sverige + internationell). Faktakollade mot koden
(30-dagarskrav, 12-mån backfill, HA-golv 2025.12.0, Energy-prefill, SE-only-ekonomi,
26 ENTSO-E-länder, opt-in-delning). Publiceras av Sebastian.

## Svenska (Home Assistant Sverige m.fl.)

Har byggt en HACS-integration som betygsätter hur bra hembatteriet styrs mot spotpriset, och tänkte att den kan vara intressant för fler här.

Bakgrund: jag driver wolta.se, en gratis elpristjänst där man kan ladda upp sin driftdata och få ett optimeringsbetyg på sin batteristyrning. Integrationen gör det automatiskt direkt från Home Assistant.

Vad den gör:
• Laddar upp 15-minutersdata (batteri, nätimport/export, ev. sol) från HA:s energistatistik
• Sensorer i HA: optimeringsbetyg 0–100 % (hur nära perfekt prisstyrning batteriet körs), batterivärde i kr/år, avkastning (IRR), återbetalningstid och "facit i år"
• Betyget jämförs anonymt med andra anläggningar (percentil)
• Länk från enhetssidan till fullständigt resultat på wolta.se med uppdelning per värdekälla

Krav: batteri + import/export-sensorer i HA (samma som Energy-dashboarden använder), HA 2025.12 eller nyare. Solproduktion är valfri. Betyget fungerar för de flesta europeiska elområden, ekonomisiffrorna för svenska (SE1–SE4).

Installation:
1. HACS → tre prickar uppe till höger → Anpassade repositorier
2. Klistra in github.com/Seblin01/wolta-homeassistant, kategori Integration
3. Ladda ner Wolta och starta om HA
4. Inställningar → Enheter & tjänster → Lägg till integration → Wolta. Sensorerna förifylls från Energy-dashboarden.

Första betyget kräver minst 30 dagars data, men upp till 12 månaders historik hämtas automatiskt ur HA:s statistik vid installationen, så för de flesta kommer betyget inom några minuter.

Om integritet: driftdatat lagras på wolta.se för analysen. Inget konto behövs och ingen persondata skickas. Tar man bort integrationen raderas datat på servern. Delning till den anonyma jämförelsestatistiken är opt-in.

Öppen källkod (MIT): github.com/Seblin01/wolta-homeassistant. Feedback och buggrapporter tas tacksamt emot.

## English (international HA groups)

I've built a HACS integration that grades how well your home battery is dispatched against day-ahead spot prices, and figured it might be useful for others here.

Background: I run wolta.se, a free electricity price service where you can upload your battery's operational data and get an optimisation grade. The integration does this automatically straight from Home Assistant.

What it does:
• Uploads 15-minute energy data (battery charge/discharge, grid import/export, optionally solar) from HA's long-term statistics
• Sensors in HA: optimisation grade 0–100 % (how close your battery runs to perfect price-driven dispatch), plus battery value per year, IRR, payback time and "actual savings this year"
• Your grade is compared anonymously against other installations (percentile)
• A link on the device page opens your full results on wolta.se with a breakdown per value source

Supported countries: the grading works for most European ENTSO-E bidding zones (26 countries at the moment, from the Nordics down to Iberia and the Balkans) using day-ahead prices from ENTSO-E. The economy sensors (value/IRR/payback) are currently Sweden-only, since they build on Swedish grid tariffs and tax rules. Grades for non-Swedish zones are calculated in EUR.

Requirements: a battery plus grid import/export sensors in HA (the same ones your Energy dashboard uses), HA 2025.12 or newer. Solar is optional.

Installation:
1. HACS → three-dot menu → Custom repositories
2. Paste github.com/Seblin01/wolta-homeassistant, category Integration
3. Download Wolta and restart HA
4. Settings → Devices & Services → Add integration → Wolta. Sensors are prefilled from your Energy dashboard.

The first grade needs at least 30 days of data, but up to 12 months of history is backfilled automatically from HA's statistics during setup, so most people get their grade within minutes.

Privacy: your energy data is stored on wolta.se to power the analysis. No account needed, no personal data sent. Removing the integration deletes your data server-side. Sharing to the anonymous benchmark is opt-in.

Open source (MIT): github.com/Seblin01/wolta-homeassistant. Feedback and bug reports are very welcome.
