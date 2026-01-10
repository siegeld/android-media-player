"""Constants for Android Media Player integration."""

DOMAIN = "android_media_player"

CONF_HOST = "host"
CONF_PORT = "port"
CONF_NAME = "name"

DEFAULT_PORT = 8765
DEFAULT_NAME = "Android Media Player"

# Connection settings
RECONNECT_INTERVAL_MIN = 1  # seconds (initial)
RECONNECT_INTERVAL_MAX = 30  # seconds (max backoff)
RECONNECT_INTERVAL = 1  # for backwards compatibility
CONNECTION_TIMEOUT = 10  # seconds
WS_HEARTBEAT = 30  # seconds
