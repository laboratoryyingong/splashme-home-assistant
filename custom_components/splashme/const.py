"""Constants for the SplashMe integration."""

from os import getenv

from homeassistant.const import Platform

DOMAIN = "splashme"
MANUFACTURER = "SplashMe"

DEFAULT_API_BASE_URL = "https://api.splashmepool.com.au"
API_BASE_URL = getenv("SPLASHME_API_BASE_URL", DEFAULT_API_BASE_URL).rstrip("/")

OAUTH2_AUTHORIZE = f"{API_BASE_URL}/api-gateway/v1/oauth/authorize"
OAUTH2_TOKEN = f"{API_BASE_URL}/api-gateway/v1/oauth/token"
# Built-in public OAuth client (no secret, PKCE): registered on the backend
# once per environment, so users never enter application credentials.
OAUTH2_CLIENT_ID = "splashme-home-assistant"
OAUTH2_USERINFO = f"{API_BASE_URL}/api-gateway/v1/oauth/userinfo"
USER_SITES = f"{API_BASE_URL}/api-gateway/v1/user/sites"
USER_DEVICE_ONLINE_STATUSES = f"{API_BASE_URL}/api-gateway/v1/user/deviceOnlineStatuses"
IOT_SHADOW_DEVICE_BASE = f"{API_BASE_URL}/api-gateway/v1/iot-shadow/device"
IOT_SHADOW_V2_DEVICE_BASE = f"{API_BASE_URL}/api-gateway/v2/iot-shadow/device"

OAUTH2_SCOPES = ["profile"]

COORDINATOR_NAME = "splashme"
SCAN_INTERVAL_SECONDS = 60
SITES_REFRESH_INTERVAL_SECONDS = 300
INSTALLER_ROLE = "installer"

# Config entry options persisting the site/device selection across restarts
OPT_SELECTED_SITE_ID = "selected_site_id"
OPT_SELECTED_DEVICE_ID = "selected_device_id"
# Per-device pump speed setpoints: applied when the pump switch turns on;
# changing the slider while the pump is stopped only updates this value.
OPT_PUMP_SPEED_SETPOINTS = "pump_speed_setpoints"

# LAN (local) connection: devices with firmware >= 2.5.41 serve HTTP on :8080
# and announce via mDNS (_splashme._tcp). Writes are signed with the device's
# protocol v2 envelope key, fetched once from the cloud during setup.
CONF_CONNECTION = "connection"
CONNECTION_LAN = "lan"
# Auth-only account entry: holds the OAuth token, creates no entities.
CONNECTION_ACCOUNT = "account"
CONF_MAC = "mac"
CONF_DEVICE_ID = "device_id"
CONF_ENVELOPE_KEY = "envelope_key"
LAN_PORT = 8080
# Pump speed applied when the LAN pump switch turns on (entry option).
OPT_LAN_PUMP_SPEED_SETPOINT = "pump_speed_setpoint"
# Options flow of a LAN entry (dashboard behaviour)
OPT_CREATE_DASHBOARD = "create_dashboard"
OPT_DASHBOARD_ADMIN_ONLY = "dashboard_admin_only"  # v0.1.11, replaced by OPT_DASHBOARD_MODE
OPT_DASHBOARD_MODE = "dashboard_mode"
# Observe only: no entity, service or action that writes to the controller (#15).
OPT_READ_ONLY = "read_only"
# Pool / spa target temperature range in whole °C: the SplashMe app's dial.
HEATING_TARGET_MIN = 0
HEATING_TARGET_MAX = 50
DASHBOARD_FULL = "full"
DASHBOARD_BASIC = "basic"
DASHBOARD_BASIC_USERS_FULL_ADMINS = "basic_users_full_admins"
DASHBOARD_FULL_ADMINS_ONLY = "full_admins_only"
DASHBOARD_MODES = (DASHBOARD_FULL, DASHBOARD_BASIC, DASHBOARD_BASIC_USERS_FULL_ADMINS, DASHBOARD_FULL_ADMINS_ONLY)
LAN_TIMEOUT_SECONDS = 10
LAN_SCAN_INTERVAL_SECONDS = 15
# After a write, poll faster for a while so flow, power and speed are seen
# ramping (a pump takes up to a minute to prime) instead of one sample later.
LAN_FAST_POLL_INTERVAL_SECONDS = 3
LAN_FAST_POLL_SECONDS = 60

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

PLATFORMS_LAN: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
]

# A read-only LAN entry: readings, plus the Refresh and Reset Pool Dashboard
# buttons, which never reach the controller.
PLATFORMS_LAN_READ_ONLY: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SENSOR,
]
