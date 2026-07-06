# Options-flow v0.4.0 – justera anläggning + ekonomi i efterhand

**Datum:** 2026-07-06 · **Godkänd av:** Sebastian · **Scope-beslut:** anläggning + ekonomi (INTE sensorval, INTE zon, INTE pv_kwp)

## Mål

Alla profilvärden som backendens `PATCH /api/v1/profile/{token}` stödjer ska gå att ändra
från HA:s options-flow utan att ta bort och lägga till integrationen: **batterikapacitet
(kWh), effekt (kW), verkningsgrad** + befintliga **inköpspris, inköpsdatum**. Dessutom ska
pris/datum gå att **rensa** (v0.3.0-wart: tomt fält sväljs tyst).

## Delar

### 1. Backend (sibe/wolta, egen MR, v0.15.2)

`patch_profile` (api/calibration.py) kör i dag `_battery_metrics` **synkront** vid
anläggningsändring (battery_kwh/kw/eff/pv_kwp/zone) – full grade med perfekt-benchmark.
För en integrationsprofil (Bronäs: 367 dygn 15-min) tar det 30–90 s → NPM-proxyns
60 s-timeout 504:ar medan servern jobbar vidare.

**Ändring:** hoppa över den synkrona omräkningen när `profile_kind == 'integration'`.
Motivering: options-flödet triggar alltid recompute efter PATCH (PATCH nollar redan
`integration.last_recompute`-cooldownen) → warm-jobbets betygssteg kör `fill_metrics`
med exakt samma metrik asynkront. Den synkrona vägen är redundant för integrationsprofiler.
Upload-profiler (webbens "uppdatera profil") oförändrade – deras UX förväntar sig
uppdaterat betyg i svaret.

Explicit `null` i PATCH-body rensar redan fält (`exclude_unset` + `validate_purchase_date(None)
→ None`) – ingen backendändring behövs för rensning, men det låses med test.

### 2. HA-integrationen (Seblin01/wolta-homeassistant, v0.4.0)

`WoltaOptionsFlow.async_step_init` utökas:

- **Nya fält:** `battery_kwh`, `battery_kw`, `eff` – `vol.Required` med default från
  `entry.data` (alltid ifyllda). Samma selectors/gränser som setup-steget.
- **Diff-logik:** bara fält vars värde ändrats mot `entry.data` läggs i PATCH-payloaden.
  Oförändrad anläggning ⇒ ingen anläggnings-PATCH (ingen onödig server-omräkning).
  Tom diff ⇒ ingen PATCH alls.
- **Rensning:** `cost_sek`/`purchase_date` är `vol.Optional` med prefill; saknad nyckel i
  `user_input` när `entry.data` har ett värde ⇒ tolkas som rensning ⇒ `PATCH {fält: null}`
  + nyckeln tas bort ur `entry.data`. (Prefill gör att "orört formulär" skickar nuvarande
  värde ⇒ ingen falsk rensning.)
- **Efter lyckad PATCH:** uppdatera `entry.data`, trigga recompute (202 förväntas –
  cooldown nollad; 429 sväljs som i dag), `async_request_refresh` → fast-poll tills
  ekonomisensorerna uppdaterats. Befintligt mönster återanvänds oförändrat.
- **Fel:** `WoltaApiError` ⇒ `errors["base"] = "cannot_connect"` som i dag.

### 3. Release/deploy-ordning

1. Backend-MR → Sebastian mergar → CI deployar (annars riskerar plant-PATCH timeout).
2. Integration: commit på main → tag `v0.4.0` + GitHub-release (etablerad praxis för repot).
3. HACS-uppdatering på Bronäs + verifiera att integrationen laddar och sensorerna står kvar.

## Tester

- **Backend:** integrationsprofil + PATCH `battery_kwh` ⇒ 200 snabbt UTAN synkron
  metrik-omräkning (holistic_score orörd av PATCH:en själv), cooldown-nollning kvar;
  upload-profil ⇒ synkron omräkning kvar (befintligt beteende). PATCH `{"cost_sek": null}`
  ⇒ NULL i DB.
- **Integration:** options-flow med ändrad kWh ⇒ PATCH bara med `battery_kwh`; orört
  formulär ⇒ ingen PATCH; rensat prisfält ⇒ PATCH `{"cost_sek": None}` + nyckel borta ur
  entry.data; API-fel ⇒ formulärfel. Befintliga options-tester uppdateras för nya fält.

## Utanför scope

Sensorval (reconfigure-flöde), zonbyte (backend tillåter bara SE1–SE4; egentligen
ta-bort-och-lägg-till), pv_kwp (sol härleds ur uppladdat data), share_profile.
