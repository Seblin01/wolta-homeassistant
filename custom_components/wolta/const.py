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
DEFAULT_SHARE = 1.0

# API
WOLTA_API_BASE = "https://api.wolta.se"
