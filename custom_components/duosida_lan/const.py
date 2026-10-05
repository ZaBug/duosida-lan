"""Constants for the Duosida LAN integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "duosida_lan"
INTEGRATION_VERSION: Final = "0.1.1"
MANUFACTURER: Final = "Duosida"

CONF_POLL_INTERVAL: Final = "poll_interval"
DEFAULT_POLL_INTERVAL: Final = 15
MIN_POLL_INTERVAL: Final = 10
MAX_POLL_INTERVAL: Final = 120

MIN_CURRENT: Final = 6
DEFAULT_MAX_CURRENT: Final = 32
