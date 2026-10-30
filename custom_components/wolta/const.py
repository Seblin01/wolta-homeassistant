"""Constants for the Wolta integration."""

DOMAIN = "wolta"

# Config entry data keys
CONF_TOKEN = "token"
CONF_ZONE = "zone"
CONF_BATT_IN = "batt_in"
CONF_BATT_OUT = "batt_out"
CONF_GRID_IN = "grid_in"
CONF_GRID_OUT = "grid_out"
CONF_SOLAR = "solar"
CONF_BATTERY_KWH = "battery_kwh"
# Nameplate (manufacturer-rated) capacity; battery_kwh above is the USABLE capacity.
# Backend spec 2026-07-14: the ratio usable/nameplate feeds the sizing sweep.
CONF_NAMEPLATE_KWH = "nameplate_kwh"
CONF_BATTERY_KW = "battery_kw"
# Nameplate (manufacturer-rated) power; battery_kw above is the DELIVERABLE AC power.
# Backend spec 2026-07-15: shown as a note on the grade ("we measured X kW usable").
CONF_NAMEPLATE_KW = "nameplate_kw"
CONF_EFF = "eff"
# Control system driving the battery (audit finding 2026-08-24: every plant onboarded
# through this integration became 'unknown' in the corpus statistics because the field
# could never be set). Mandatory in the plant step since v0.29.0, mirroring the web
# guide's rule; "other" additionally requires the free-text name below.
CONF_CONTROL_SYSTEM = "control_system"
CONF_CONTROL_SYSTEM_NAME = "control_system_name"
CONF_RESERVE_PCT = "reserve_pct"
CONF_SHARE = "share"
CONF_COST_SEK = "cost_sek"
CONF_PURCHASE_DATE = "purchase_date"
CONF_GRID_VAR_ORE = "grid_var_ore"
CONF_SURCHARGE_ORE = "surcharge_ore"
# Since api 0.119.0 export_extra_ore is the RETAILER's terms for sold electricity (premium +,
# deduction -), no longer a catch-all that also held the grid benefit. The owner's grid benefit
# (nätnytta, the network operator's compensation for feeding in; öre/kWh excl. VAT) has a field of
# its own. SE1-SE4 only.
CONF_EXPORT_EXTRA_ORE = "export_extra_ore"
CONF_GRID_BENEFIT_ORE = "grid_benefit_ore"
# The electricity contract that applies today (api 0.119.0, SE1-SE4 only; the server owns every
# rule and range): `quarter` (the default: the spot price per quarter-hour), `month` (the month's
# average price, set after the fact) or `fixed` (a fixed price, retail_fixed_ore, which carries no
# markup). The server keeps the contract history (retail_events) and mirrors today's entry into
# these two fields and surcharge_ore; a change to them adds an entry dated today on the server.
# The history itself, corrections back in time and the time periods below are edited on wolta.se.
CONF_RETAIL_PRICE_MODEL = "retail_price_model"
CONF_RETAIL_FIXED_ORE = "retail_fixed_ore"
RETAIL_PRICE_MODELS = ("quarter", "month", "fixed")
DEFAULT_RETAIL_PRICE_MODEL = "quarter"
# Read-only here (shown as a summary in the options form, never sent from Home Assistant): the
# time periods with a price of their own for the grid fee and the grid benefit, the contract
# history and the sale side's history (spec 2026-10-27-marknadsanpassning Tillägg 5: the
# retailer's terms per entry, export_events, and the grid benefit per entry, grid_benefit_events).
# The base price (grid_var_ore / grid_benefit_ore) applies outside the periods. The server mirrors
# today's sale entries into export_extra_ore and grid_benefit_ore / export_extra_pct, and a change
# to those adds an entry dated today (or applies from the start: the server's rulings 18 and 19).
KEY_GRID_FEE_PERIODS = "grid_fee_periods"
KEY_GRID_BENEFIT_PERIODS = "grid_benefit_periods"
KEY_RETAIL_EVENTS = "retail_events"
KEY_EXPORT_EVENTS = "export_events"
KEY_GRID_BENEFIT_EVENTS = "grid_benefit_events"
# The plant's stages (GET /profile): a battery or solar purchase each, with its date.
KEY_BATTERY_EVENTS = "battery_events"
KEY_SOLAR_EVENTS = "solar_events"
# Percent-of-spot tariff components (backend api 0.55.0): variable grid fee as % of
# spot (southern-Sweden DSO model) and export compensation as % of spot.
CONF_GRID_VAR_PCT = "grid_var_pct"
CONF_EXPORT_EXTRA_PCT = "export_extra_pct"
# Reduced energy tax (the "norrlandsavdraget"): the owner lives in a municipality where the
# grid operator deducts 9.6 öre/kWh excl. VAT (Skatteverket, see the options help text).
# Tri-state, mirrored from the server (_PROFILE_SYNC_KEYS): absent/None = never answered, False =
# answered no, True = answered yes. Only asked in SE1-SE3 (no affected municipality in SE4).
CONF_REDUCED_ENERGY_TAX = "reduced_energy_tax"
# Battery charge/discharge reversed (issue #1): some community integrations/meters
# (Emaldo, signed Shelly) map the directions the wrong way → inverted grade. The flag swaps
# batt_in/batt_out in the upload path so the user doesn't have to change their HA sensors.
CONF_INVERT_BATTERY = "invert_battery"
# The DESIRED battery inversion as half-open UTC intervals (intervals.py stored form:
# a list of [from_iso | None, to_iso | None]). CONF_INVERT_BATTERY is kept as a mirror,
# true exactly when the intervals are [[None, None]], so a downgrade reads the right
# global flag in the common case. What the server's stored rows were built with (the
# APPLIED intervals) lives in the coordinator's Store, not here.
CONF_INVERT_INTERVALS = "invert_intervals"
# False for profiles adopted via the link-existing flow: the web profile (incl. CSV
# history) is not ours to delete when the integration is removed. Missing key = True
# (every pre-v0.10.0 entry was created by HA → documented delete-on-remove kept).
CONF_CREATED_BY_HA = "created_by_ha"
# Stable per-plant identifier sent to the backend as `client_plant_id`, which salts and hashes
# it into a plant_fingerprint so a re-onboarded plant reuses its existing row instead of
# creating a duplicate (and orphaning its streamed history).
#
# A freshly minted 128-bit random value, NOT the config entry_id: `entry_id` does not exist
# yet while the user flow is running (HA core builds the ConfigEntry in async_finish_flow,
# after the step returns CREATE_ENTRY), and it is re-minted on remove+re-add. High entropy is
# also a security property here — the server hashes the plaintext we send, so the salt protects
# nothing against someone who can guess the value; only the value's own entropy does.
# Entries created before v0.16.0 have no stored value; the only path that needs one is reauth,
# which falls back to the entry_id (available there, since a reauth flow carries the entry) and
# persists it. No setup-time backfill — nothing else reads the key.
CONF_PLANT_ID = "plant_id"
# View-only entry (bound plants): the plant already streams via its own binding (Sonnen
# webhook / Reduxi), so this entry only POLLS results - the coordinator must never read HA
# statistics, PUT data or trigger the recompute cadence, and reauth must never create a new
# profile (it just asks for a fresh token). Missing key = False (every pre-v0.18.0 entry
# streams).
CONF_VIEW_ONLY = "view_only"
# Spec 2026-09-14: inköpsdatumet ur statistiken (första datapunkten) skickas INTE till
# servern vid skapandet – det är inte nödvändigtvis ett inköpsdatum. Sparas här och visas
# som suggested_value i options → Economy, där användaren bekräftar det.
CONF_PREFILL_PURCHASE_DATE = "prefill_purchase_date"
# Serverns battery_status (profil-GET, spec 2026-09-14 §4). Bara stämpeln renderas; regeln
# pending→needs_input bor i backend.
# Serverns batteristämpel i profil-GET:en (spec 2026-09-14 §4) – EN nyckel för alla lässiten.
KEY_BATTERY_STATUS = "battery_status"
BATTERY_STATUS_NONE = "none"
BATTERY_STATUS_PENDING = "pending"
BATTERY_STATUS_NEEDS_INPUT = "needs_input"
BATTERY_STATUS_MEASURED = "measured"
BATTERY_STATUS_NAMEPLATE = "nameplate"
# Set when the user dismisses the measured-power repair via its "ignore" option. observed_power
# is only a lower bound and can be inflated by chronic sensor jumps in the cumulative HA
# statistics, so a user who stands by their configured power must be able to silence the nudge
# permanently. The coordinator suppresses the repair while this is truthy; adopting a value via
# the repair's "set power" path clears it (the user re-engaged). Client-only, never sent to the
# server, so it is absent from _PROFILE_SYNC_KEYS.
CONF_POWER_ISSUE_IGNORED = "power_issue_ignored"
# Same dismissal mechanism for the capacity and efficiency repairs. observed_capacity and
# observed_eff are both derived from the same meter flows, so a plant with spurious sensor jumps
# (which inflate the charged/discharged sums) can push either measurement to an implausible value;
# the user must be able to keep their configured figure. Client-only, cleared on adopt.
CONF_CAPACITY_ISSUE_IGNORED = "capacity_issue_ignored"
CONF_EFFICIENCY_ISSUE_IGNORED = "efficiency_issue_ignored"
# Läslänken (spec 2026-08-24): en CACHE av senaste lyckade mint, aldrig sanningskällan.
# Servern äger länken; integrationen mintar vid varje setup (idempotent) och skriver över.
CONF_LINK_TOKEN = "link_token"
# Extern styrning (spec 2026-08-26): binary_sensor som är 'on' när batteriet flex-styrs
# externt (t.ex. härledd ur Tibber Grid Rewards-integrationens tillståndssensor).
# KLIENTLOKAL nyckel (samma art som CONF_INVERT_BATTERY): en uppladdningstransformation,
# inte ett profilfält – PATCH:as aldrig till servern, hålls utanför _PROFILE_SYNC_KEYS.
CONF_EXTERNAL_CONTROL = "external_control_entity"
# Ersättningssensor (spec 2026-08-28): läses månadsvis ur LTS och PATCH:as till servern
# som flex_compensation-poster med source='sensor'. Ingår MEDVETET INTE i
# applied_entities-fingerprintet - den påverkar inga energirader och får inte trigga
# en ersättning av lagrade flöden (B8). Speglas INTE i _PROFILE_SYNC_KEYS.
CONF_FLEX_COMPENSATION = "flex_compensation_entity"
# Optional battery state-of-charge sensors, one per battery unit (spec 2026-10-03). The
# entity ids are CLIENT-LOCAL configuration and never PATCHed to the server - only the
# statistics read from them are uploaded, as battery_state rows through put_data.
CONF_SOC = "soc_entities"
# Legacy: set when the user declined the old soc_missing repair (its "ignore" option).
# Nothing reads it any more (SoC is required, spec 2026-10-20-soc-krav); the constant
# survives only so the repair and reconfigure flows can drop the stale key from entry.data.
CONF_SOC_ISSUE_IGNORED = "soc_issue_ignored"
# The owner's declaration that the battery has no SoC sensor to report (spec
# 2026-10-20-soc-krav, 4.4). Unlike CONF_SOC (entity ids, client-local) this IS a profile
# field synced with the server: sent in POST /profile, PATCHed from the repair, and
# mirrored back into entry.data through _PROFILE_SYNC_KEYS, since it can also be flipped
# on wolta.se. True = no sensor exists, False = has one, absent = never answered.
CONF_SOC_ABSENT = "soc_absent"
# Användaren har avböjt att vända riktningen (repairen battery_direction, bara de fixbara
# formerna). Rensas när användaren vänder via repairen. Klientlokal nyckel, aldrig till
# servern, och därför utanför _PROFILE_SYNC_KEYS.
CONF_DIRECTION_ISSUE_IGNORED = "direction_issue_ignored"
# When the owner last flipped the direction (ISO UTC, direction.apply_direction_target). The
# fixable repair stays down until the server's direction data (battery_direction.as_of) is newer
# than this: the server recounts the direction days nightly, so a grade fetched right after the
# flip still says "reversed" and would otherwise re-raise the repair, inviting a second flip
# back. Client-only, never sent to the server, so it is absent from _PROFILE_SYNC_KEYS.
CONF_DIRECTION_FLIPPED_AT = "direction_flipped_at"
# When the owner last changed the desired direction, from the repair or by hand (ISO UTC).
# Unlike CONF_DIRECTION_FLIPPED_AT a healthy verdict never clears it (_clear_direction_flags):
# a SoC verdict counted before it describes the old direction (direction.read_verdict). It is
# dropped only when the server refuses the change it dates (409 direction_verified): nothing
# changed, and the verdict behind the refusal must count as current. Client-only.
CONF_DIRECTION_CHANGED_AT = "direction_changed_at"
# Varifrån den senaste riktningsändringen kom: "repair" (reparationen) eller "manual"
# (ägaren valde själv). Kopieras till riktningsjobbet; saknas den räknas den som "repair".
# Klientlokal nyckel.
CONF_DIRECTION_ORIGIN = "direction_origin"
# The owner confirmed a manual change against a current SoC verdict (bool). Sent as
# `override` with the direction job; the repair never sets it. Client-only.
CONF_DIRECTION_OVERRIDE = "direction_override"

# Defaults
DEFAULT_ZONE = "SE3"
DEFAULT_BATTERY_KWH = 10.0
DEFAULT_BATTERY_KW = 5.0
MIN_BATTERY_KWH = 0.1
MIN_BATTERY_KW = 0.1
DEFAULT_EFF = 0.9

# API – wolta.se serves the API under /api/v1 (no api. subdomain exists)
WOLTA_API_BASE = "https://wolta.se"


def link_url(link_token: str) -> str:
    """Landningssidan för anläggningen, med LÄSLÄNKEN i stället för ägar-tokenet.
    Ägar-tokenet skrivs aldrig in i configuration_url (Frencks HACS-review-notering,
    hacs/default#9039): URL:en syns på enhetssidan och följer med varje
    device-registry-export. WOLTA_API_BASE är ren sajt-bas – /api/v1 läggs på av
    API-klienten, inte här.

    Läslänken står i FRAGMENTET (#link=), inte i queryn (Woltas C-härdning 2026-10-16):
    fragmentet skickas aldrig till servern (RFC 3986 §3.5) och tas bort ur Referer, och webben
    växlar det vid inträdet mot en HttpOnly-kaka. Gamla ?link=-länkar fungerar fortfarande på
    wolta.se, så äldre versioner av integrationen påverkas inte."""
    from urllib.parse import quote

    return f"{WOLTA_API_BASE}/anlaggning#link={quote(link_token, safe='')}"

# Control systems for the plant-step selector. The picker takes the server's list
# (GET /control-systems, `label_en`, api >= 0.123.0) and falls back to this one when the
# request fails or an older server answers without English labels - see
# control_system_prefill.async_control_system_choices. Values MUST match the backend's
# CONTROL_SYSTEMS set (api/calibration.py) - the server 422s on unknown values - and the
# labels match its CONTROL_SYSTEM_LABELS_EN. Alphabetical by label with Other last
# (control_system_prefill.sorted_options, checked by tests/test_control_system_choices.py).
# Brand names are language-neutral; only the four modes carry language (inline English
# labels, same convention as SUPPORTED_ZONES below).
CONTROL_SYSTEMS: list[tuple[str, str]] = [
    ("1komma5", "1KOMMA5°"),
    ("aikion", "Aikion"),
    ("checkwatt", "CheckWatt"),
    ("custom", "Custom-built (Home Assistant, script)"),
    ("darkstar", "Darkstar"),
    ("emaldo", "Emaldo"),
    ("emhass", "EMHASS"),
    ("ferroamp", "Ferroamp"),
    ("greenely", "Greenely"),
    ("huawei", "Huawei"),
    ("keepai", "KeepAI"),
    ("manual", "Manual"),
    ("pixii", "Pixii"),
    ("reduxi", "Reduxi"),
    ("self_consumption", "Self-consumption (passive)"),
    ("sigenergy", "Sigenergy"),
    ("solis", "Solis"),
    ("sonnen", "Sonnen"),
    ("tibber", "Tibber"),
    ("other", "Other"),
]

# Zones that get the spot-price sensors. The price endpoint converts to SEK and öre with
# Swedish VAT (25 %), so the figures are only meaningful for Swedish zones; elsewhere the
# price coordinator is not created at all (no polling, no 404s). Decided at setup. If the
# zone is changed in the options flow to a non-Swedish one without a reload, the price
# coordinator makes no request and the sensors are unavailable until the entry is reloaded.
PRICE_SENSOR_ZONES: tuple[str, ...] = ("SE1", "SE2", "SE3", "SE4")

# Supported price zones for the zone selector (SP3 multi-land requirement).
# Covers all 26 countries in the Wolta backend; labels are shown in the UI.
# Server-side validates the zone; unknown zones get a 422 response.
# Keep this list in sync with the backend's countries register manually.
SUPPORTED_ZONES: list[tuple[str, str]] = [
    # Sweden
    ("SE1", "SE1 – Northern Sweden"),
    ("SE2", "SE2 – Central Sweden"),
    ("SE3", "SE3 – South-central Sweden"),
    ("SE4", "SE4 – Southern Sweden"),
    # Norway
    ("NO1", "NO1 – Eastern Norway"),
    ("NO2", "NO2 – Southern Norway"),
    ("NO3", "NO3 – Central Norway"),
    ("NO4", "NO4 – Northern Norway"),
    ("NO5", "NO5 – Western Norway"),
    # Denmark
    ("DK1", "DK1 – Western Denmark"),
    ("DK2", "DK2 – Eastern Denmark"),
    # Finland, Baltics
    ("FI", "FI – Finland"),
    ("EE", "EE – Estonia"),
    ("LV", "LV – Latvia"),
    ("LT", "LT – Lithuania"),
    # Central Europe
    ("NL", "NL – Netherlands"),
    ("DELU", "DE – Germany"),
    ("CZ", "CZ – Czechia"),
    ("AT", "AT – Austria"),
    ("BE", "BE – Belgium"),
    ("FR", "FR – France"),
    ("CH", "CH – Switzerland"),
    ("PL", "PL – Poland"),
    # Iberia
    ("ES", "ES – Spain"),
    ("PT", "PT – Portugal"),
    # South Europe
    ("IT_NORD", "IT – Italy"),
    ("GR", "GR – Greece"),
    ("RO", "RO – Romania"),
    ("HU", "HU – Hungary"),
    ("SK", "SK – Slovakia"),
    ("SI", "SI – Slovenia"),
    ("BG", "BG – Bulgaria"),
    ("HR", "HR – Croatia"),
    ("IE", "IE – Ireland"),
]
