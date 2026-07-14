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
CONF_EFF = "eff"
CONF_RESERVE_PCT = "reserve_pct"
CONF_SHARE = "share"
CONF_COST_SEK = "cost_sek"
CONF_PURCHASE_DATE = "purchase_date"
CONF_GRID_VAR_ORE = "grid_var_ore"
CONF_SURCHARGE_ORE = "surcharge_ore"
CONF_EXPORT_EXTRA_ORE = "export_extra_ore"
# Battery charge/discharge reversed (issue #1): some community integrations/meters
# (Emaldo, signed Shelly) map the directions the wrong way → inverted grade. The flag swaps
# batt_in/batt_out in the upload path so the user doesn't have to change their HA sensors.
CONF_INVERT_BATTERY = "invert_battery"
# False for profiles adopted via the link-existing flow: the web profile (incl. CSV
# history) is not ours to delete when the integration is removed. Missing key = True
# (every pre-v0.10.0 entry was created by HA → documented delete-on-remove kept).
CONF_CREATED_BY_HA = "created_by_ha"

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


def profile_url(token: str) -> str:
    """Link to the profile's full results on wolta.se (token mode: ?profile=)."""
    from urllib.parse import quote
    return f"{WOLTA_API_BASE}/optimeringsbetyg?profile={quote(token, safe='')}"

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
