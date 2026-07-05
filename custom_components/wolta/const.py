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
CONF_BATTERY_KW = "battery_kw"
CONF_EFF = "eff"
CONF_SHARE = "share"

# Defaults
DEFAULT_ZONE = "SE3"
DEFAULT_BATTERY_KWH = 0.0
DEFAULT_BATTERY_KW = 0.0
DEFAULT_EFF = 0.9
# Privacy opt-in – must default to False (users must explicitly opt in to sharing)
DEFAULT_SHARE = False

# API – wolta.se serverar API:t under /api/v1 (ingen api.-subdomän finns)
WOLTA_API_BASE = "https://wolta.se"

# Supported price zones for the zone selector (SP3 multi-land requirement).
# Covers all 26 countries in the Wolta backend; labels are shown in the UI.
# Server-side validates the zone; unknown zones get a 422 response.
# Keep this list in sync with the backend's countries register manually.
SUPPORTED_ZONES: list[tuple[str, str]] = [
    # Sweden
    ("SE1", "SE1 – Norra Sverige"),
    ("SE2", "SE2 – Mellersta Sverige"),
    ("SE3", "SE3 – Södra mellansverige"),
    ("SE4", "SE4 – Södra Sverige"),
    # Norway
    ("NO1", "NO1 – Östra Norge"),
    ("NO2", "NO2 – Södra Norge"),
    ("NO3", "NO3 – Mellersta Norge"),
    ("NO4", "NO4 – Norra Norge"),
    ("NO5", "NO5 – Västra Norge"),
    # Denmark
    ("DK1", "DK1 – Västra Danmark"),
    ("DK2", "DK2 – Östra Danmark"),
    # Finland, Baltics
    ("FI", "FI – Finland"),
    ("EE", "EE – Estland"),
    ("LV", "LV – Lettland"),
    ("LT", "LT – Litauen"),
    # Central Europe
    ("NL", "NL – Nederländerna"),
    ("DELU", "DE – Tyskland"),
    ("CZ", "CZ – Tjeckien"),
    ("AT", "AT – Österrike"),
    ("BE", "BE – Belgien"),
    ("FR", "FR – Frankrike"),
    ("CH", "CH – Schweiz"),
    ("PL", "PL – Polen"),
    # Iberia
    ("ES", "ES – Spanien"),
    ("PT", "PT – Portugal"),
    # South Europe
    ("IT_NORD", "IT – Italien"),
    ("GR", "GR – Grekland"),
    ("RO", "RO – Rumänien"),
    ("HU", "HU – Ungern"),
    ("SK", "SK – Slovakien"),
    ("SI", "SI – Slovenien"),
    ("BG", "BG – Bulgarien"),
    ("HR", "HR – Kroatien"),
    ("IE", "IE – Irland"),
]
