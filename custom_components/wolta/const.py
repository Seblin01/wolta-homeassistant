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
CONF_EXPORT_EXTRA_ORE = "export_extra_ore"
# Percent-of-spot tariff components (backend api 0.55.0): variable grid fee as % of
# spot (southern-Sweden DSO model) and export compensation as % of spot.
CONF_GRID_VAR_PCT = "grid_var_pct"
CONF_EXPORT_EXTRA_PCT = "export_extra_pct"
# Battery charge/discharge reversed (issue #1): some community integrations/meters
# (Emaldo, signed Shelly) map the directions the wrong way → inverted grade. The flag swaps
# batt_in/batt_out in the upload path so the user doesn't have to change their HA sensors.
CONF_INVERT_BATTERY = "invert_battery"
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
BATTERY_STATUS_NONE = "none"
BATTERY_STATUS_PENDING = "pending"
BATTERY_STATUS_NEEDS_INPUT = "needs_input"
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
# re-backfill (B8). Speglas INTE i _PROFILE_SYNC_KEYS.
CONF_FLEX_COMPENSATION = "flex_compensation_entity"

# Defaults
DEFAULT_ZONE = "SE3"
DEFAULT_BATTERY_KWH = 10.0
DEFAULT_BATTERY_KW = 5.0
MIN_BATTERY_KWH = 0.1
MIN_BATTERY_KW = 0.1
DEFAULT_EFF = 0.9
# Privacy opt-in – must default to False (users must explicitly opt in to sharing)
DEFAULT_SHARE = False

# API – wolta.se serves the API under /api/v1 (no api. subdomain exists)
WOLTA_API_BASE = "https://wolta.se"


def link_url(link_token: str) -> str:
    """Landningssidan för anläggningen, med LÄSLÄNKEN i stället för ägar-tokenet.
    Ägar-tokenet skrivs aldrig in i configuration_url (Frencks HACS-review-notering,
    hacs/default#9039): URL:en syns på enhetssidan och följer med varje
    device-registry-export. WOLTA_API_BASE är ren sajt-bas – /api/v1 läggs på av
    API-klienten, inte här."""
    from urllib.parse import quote

    return f"{WOLTA_API_BASE}/anlaggning?link={quote(link_token, safe='')}"

# Control systems for the plant-step selector. Values MUST match the backend's
# CONTROL_SYSTEMS set (api/calibration.py) and the web's control-systems.ts - the
# server 422s on unknown values. Order mirrors the web picker. Brand names are
# language-neutral; only the last three carry language (inline English labels,
# same convention as SUPPORTED_ZONES below).
CONTROL_SYSTEMS: list[tuple[str, str]] = [
    ("tibber", "Tibber"),
    ("checkwatt", "CheckWatt"),
    ("greenely", "Greenely"),
    ("aikion", "Aikion"),
    ("sigenergy", "Sigenergy"),
    ("emaldo", "Emaldo"),
    ("ferroamp", "Ferroamp"),
    ("sonnen", "Sonnen"),
    ("reduxi", "Reduxi"),
    ("huawei", "Huawei"),
    ("pixii", "Pixii"),
    ("emhass", "EMHASS"),
    ("self_consumption", "Self-consumption (passive)"),
    ("manual", "Manual"),
    ("other", "Other"),
]

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
