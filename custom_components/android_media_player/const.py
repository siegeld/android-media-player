"""Constants for Android Media Player integration."""

DOMAIN = "android_media_player"

CONF_HOST = "host"
CONF_PORT = "port"
CONF_NAME = "name"

DEFAULT_PORT = 8765
DEFAULT_NAME = "Android Media Player"

# Connection settings
RECONNECT_INTERVAL_MIN = 5  # seconds (initial)
RECONNECT_INTERVAL_MAX = 300  # seconds (max backoff - 5 minutes)
RECONNECT_INTERVAL = 5  # for backwards compatibility
CONNECTION_TIMEOUT = 10  # seconds
WS_HEARTBEAT = 30  # seconds
