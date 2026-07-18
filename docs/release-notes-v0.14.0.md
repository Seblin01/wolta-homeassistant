# v0.14.0 – capex_scope attribute + whole-plant price safety in the options form

Companion release to the wolta.se backend's `cost_scope` work (2026-07-18): the backend now
records whether a profile's scalar purchase price covers the battery alone or the whole
plant (solar + battery), instead of every client having to guess.

### `capex_scope` attribute on the IRR and payback sensors

The backend (2026-07-18) now tells clients which capital base the decision block's IRR and
payback figures are attributed to: `battery` (the battery alone – the norm for HA-created
profiles, whose cost field is battery-only) or `plant` (the whole plant, solar + battery –
profiles created in the wolta.se guide with a combined purchase price and later adopted into
HA). The sensors expose this as a `capex_scope` attribute so automations and dashboards can
label the figure correctly. On older backends the attribute is simply omitted.

### Battery cost field hidden for whole-plant-priced profiles

For adopted wolta.se profiles whose stored purchase price covers the **whole plant**, the
options form's "Battery purchase price – battery only" field no longer applies: editing it
would silently reinterpret a whole-plant figure as battery-only. The field is now hidden for
those profiles (the economy section explains why) – the price is edited on your wolta.se
profile page, where the labels match the stored semantics. Profiles with a battery-only
price (all profiles created in HA) are unaffected.
