"""Coordinator for Android Media Player integration."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    DOMAIN,
    RECONNECT_INTERVAL_MIN,
    RECONNECT_INTERVAL_MAX,
    CONNECTION_TIMEOUT,
    WS_HEARTBEAT,
)

_LOGGER = logging.getLogger(__name__)


class AndroidMediaPlayerCoordinator:
    """Coordinator for managing connection to Android Media Player."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        name: str,
    ) -> None:
        """Initialize the coordinator."""
        self.hass = hass
        self.host = host
        self.port = port
        self.name = name

        self._ws: websockets.WebSocketClientProtocol | None = None
        self._ws_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._connected = False
        self._state: dict[str, Any] = {}
        self._listeners: list[Callable[[], None]] = []
        self._should_reconnect = True
        self._reconnect_count = 0
        self._current_reconnect_interval = RECONNECT_INTERVAL_MIN

        _LOGGER.debug(
            "Coordinator initialized for %s at %s:%s",
            name, host, port
        )

    @property
    def base_url(self) -> str:
        """Return the base URL for the Android device."""
        return f"http://{self.host}:{self.port}"

    @property
    def ws_url(self) -> str:
        """Return the WebSocket URL for the Android device."""
        return f"ws://{self.host}:{self.port}/ws"

    @property
    def available(self) -> bool:
        """Return True if the device is available."""
        return self._connected

    @property
    def state(self) -> dict[str, Any]:
        """Return the current player state."""
        return self._state

    def add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Add a listener for state updates."""
        self._listeners.append(callback)
        _LOGGER.debug("Added state listener, total listeners: %d", len(self._listeners))

        def remove_listener():
            self._listeners.remove(callback)
            _LOGGER.debug("Removed state listener, total listeners: %d", len(self._listeners))

        return remove_listener

    @callback
    def _notify_listeners(self) -> None:
        """Notify all listeners of state update."""
        _LOGGER.debug("Notifying %d listeners of state update", len(self._listeners))
        for listener in self._listeners:
            listener()

    async def async_connect(self) -> None:
        """Connect to the Android device."""
        _LOGGER.info(
            "Initiating connection to Android Media Player '%s' at %s",
            self.name, self.ws_url
        )
        self._should_reconnect = True
        self._reconnect_count = 0
        self._current_reconnect_interval = RECONNECT_INTERVAL_MIN
        await self._connect_websocket()

    async def async_disconnect(self) -> None:
        """Disconnect from the Android device."""
        _LOGGER.info("Disconnecting from Android Media Player '%s'", self.name)
        self._should_reconnect = False

        if self._reconnect_task:
            _LOGGER.debug("Cancelling reconnect task")
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
            self._reconnect_task = None

        if self._ws_task:
            _LOGGER.debug("Cancelling WebSocket listener task")
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            self._ws_task = None

        if self._ws:
            _LOGGER.debug("Closing WebSocket connection")
            await self._ws.close()
            self._ws = None

        self._connected = False
        _LOGGER.info("Disconnected from Android Media Player '%s'", self.name)

    async def _connect_websocket(self) -> None:
        """Establish WebSocket connection."""
        _LOGGER.debug(
            "Attempting WebSocket connection to %s (timeout: %ds)",
            self.ws_url, CONNECTION_TIMEOUT
        )
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(
                    self.ws_url,
                    ping_interval=WS_HEARTBEAT,
                    ping_timeout=WS_HEARTBEAT,
                ),
                timeout=CONNECTION_TIMEOUT,
            )
            self._connected = True
            self._reconnect_count = 0
            self._current_reconnect_interval = RECONNECT_INTERVAL_MIN  # Reset on successful connect
            _LOGGER.info(
                "Successfully connected to Android Media Player '%s' at %s",
                self.name, self.ws_url
            )

            # Fetch initial state via REST (don't wait for device to push)
            await self.async_get_state()
            self._notify_listeners()

            # Start listening for messages
            self._ws_task = asyncio.create_task(self._listen_websocket())

        except asyncio.TimeoutError:
            _LOGGER.warning(
                "Connection timeout to Android Media Player '%s' at %s after %ds",
                self.name, self.ws_url, CONNECTION_TIMEOUT
            )
            self._connected = False
            self._schedule_reconnect()
        except ConnectionRefusedError:
            _LOGGER.warning(
                "Connection refused by Android Media Player '%s' at %s - is the app running?",
                self.name, self.ws_url
            )
            self._connected = False
            self._schedule_reconnect()
        except Exception as err:
            _LOGGER.warning(
                "Failed to connect to Android Media Player '%s' at %s: %s (%s)",
                self.name, self.ws_url, err, type(err).__name__
            )
            self._connected = False
            self._schedule_reconnect()

    async def _listen_websocket(self) -> None:
        """Listen for WebSocket messages."""
        _LOGGER.debug("Starting WebSocket message listener for '%s'", self.name)
        message_count = 0
        try:
            async for message in self._ws:
                message_count += 1
                try:
                    data = json.loads(message)
                    old_state = self._state.get("state", "unknown")
                    new_state = data.get("state", "unknown")

                    if old_state != new_state:
                        _LOGGER.info(
                            "Android Media Player '%s' state changed: %s -> %s",
                            self.name, old_state, new_state
                        )
                    else:
                        _LOGGER.debug(
                            "Android Media Player '%s' state update: state=%s, title=%s",
                            self.name, new_state, data.get("mediaTitle")
                        )

                    self._state = data
                    self.hass.loop.call_soon_threadsafe(self._notify_listeners)
                except json.JSONDecodeError as err:
                    _LOGGER.warning(
                        "Received invalid JSON from '%s': %s (message: %s)",
                        self.name, err, message[:100]
                    )
        except ConnectionClosed as err:
            _LOGGER.info(
                "WebSocket connection to '%s' closed: code=%s, reason=%s (received %d messages)",
                self.name, err.code, err.reason, message_count
            )
        except WebSocketException as err:
            _LOGGER.warning(
                "WebSocket error for '%s': %s (%s)",
                self.name, err, type(err).__name__
            )
        except asyncio.CancelledError:
            _LOGGER.debug("WebSocket listener for '%s' was cancelled", self.name)
            raise
        except Exception as err:
            _LOGGER.error(
                "Unexpected error in WebSocket listener for '%s': %s (%s)",
                self.name, err, type(err).__name__,
                exc_info=True
            )
        finally:
            was_connected = self._connected
            self._connected = False
            if was_connected:
                _LOGGER.info(
                    "Android Media Player '%s' is now unavailable",
                    self.name
                )
            self._notify_listeners()
            if self._should_reconnect:
                self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        """Schedule a reconnection attempt with exponential backoff."""
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_count += 1

            # Calculate next interval with exponential backoff
            next_interval = self._current_reconnect_interval
            _LOGGER.debug(
                "Scheduling reconnect attempt #%d for '%s' in %ds",
                self._reconnect_count, self.name, next_interval
            )
            self._reconnect_task = asyncio.create_task(self._reconnect(next_interval))

            # Increase interval for next time (exponential backoff with cap)
            self._current_reconnect_interval = min(
                self._current_reconnect_interval * 2,
                RECONNECT_INTERVAL_MAX
            )

    async def _reconnect(self, delay: int) -> None:
        """Attempt to reconnect after a delay."""
        await asyncio.sleep(delay)
        if self._should_reconnect:
            _LOGGER.info(
                "Attempting reconnect #%d to Android Media Player '%s' (next delay: %ds)...",
                self._reconnect_count, self.name, self._current_reconnect_interval
            )
            await self._connect_websocket()

    async def async_send_command(self, command: str, **kwargs: Any) -> bool:
        """Send a command via WebSocket."""
        _LOGGER.debug(
            "Sending command '%s' to '%s' with args: %s",
            command, self.name, kwargs
        )

        if not self._connected or not self._ws:
            _LOGGER.debug(
                "WebSocket not connected for '%s', falling back to REST API",
                self.name
            )
            return await self._send_rest_command(command, **kwargs)

        try:
            message = {"command": command, **kwargs}
            await self._ws.send(json.dumps(message))
            _LOGGER.debug(
                "Successfully sent WebSocket command '%s' to '%s'",
                command, self.name
            )
            return True
        except Exception as err:
            _LOGGER.warning(
                "Failed to send WebSocket command '%s' to '%s': %s - falling back to REST",
                command, self.name, err
            )
            return await self._send_rest_command(command, **kwargs)

    async def _send_rest_command(self, command: str, **kwargs: Any) -> bool:
        """Send a command via REST API."""
        endpoint_map = {
            "play": "/play",
            "pause": "/pause",
            "stop": "/stop",
            "volume": "/volume",
            "mute": "/mute",
            "seek": "/seek",
        }

        endpoint = endpoint_map.get(command)
        if not endpoint:
            _LOGGER.warning("Unknown command '%s' for '%s'", command, self.name)
            return False

        url = f"{self.base_url}{endpoint}"
        _LOGGER.debug("Sending REST command to %s", url)

        try:
            async with aiohttp.ClientSession() as session:
                # Prepare request body based on command
                json_body = None
                if command == "play" and "url" in kwargs:
                    json_body = {
                        "url": kwargs["url"],
                        "title": kwargs.get("title"),
                        "artist": kwargs.get("artist"),
                    }
                    _LOGGER.info(
                        "Playing media on '%s': url=%s, title=%s",
                        self.name, kwargs["url"], kwargs.get("title")
                    )
                elif command == "volume" and "level" in kwargs:
                    json_body = {"level": kwargs["level"]}
                    _LOGGER.debug("Setting volume on '%s' to %s", self.name, kwargs["level"])
                elif command == "mute":
                    json_body = {"muted": kwargs.get("muted")}
                    _LOGGER.debug("Setting mute on '%s' to %s", self.name, kwargs.get("muted"))
                elif command == "seek" and "position" in kwargs:
                    json_body = {"position": kwargs["position"]}
                    _LOGGER.debug("Seeking on '%s' to %sms", self.name, kwargs["position"])

                async with session.post(
                    url,
                    json=json_body,
                    timeout=aiohttp.ClientTimeout(total=CONNECTION_TIMEOUT),
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        success = data.get("success", True)
                        _LOGGER.debug(
                            "REST command '%s' to '%s' response: success=%s",
                            command, self.name, success
                        )
                        if "state" in data:
                            self._state = data["state"]
                            self._notify_listeners()
                        return success
                    else:
                        _LOGGER.warning(
                            "REST command '%s' to '%s' failed with status %d",
                            command, self.name, response.status
                        )
                        return False
        except aiohttp.ClientConnectorError as err:
            _LOGGER.warning(
                "Cannot connect to '%s' for REST command '%s': %s",
                self.name, command, err
            )
            return False
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "Timeout sending REST command '%s' to '%s'",
                command, self.name
            )
            return False
        except Exception as err:
            _LOGGER.warning(
                "Failed to send REST command '%s' to '%s': %s (%s)",
                command, self.name, err, type(err).__name__
            )
            return False

    async def async_get_state(self) -> dict[str, Any] | None:
        """Get current state via REST API."""
        url = f"{self.base_url}/state"
        _LOGGER.debug("Fetching state from %s", url)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=CONNECTION_TIMEOUT),
                ) as response:
                    if response.status == 200:
                        self._state = await response.json()
                        _LOGGER.debug(
                            "Got state from '%s': state=%s",
                            self.name, self._state.get("state")
                        )
                        return self._state
                    else:
                        _LOGGER.warning(
                            "Failed to get state from '%s': HTTP %d",
                            self.name, response.status
                        )
        except aiohttp.ClientConnectorError as err:
            _LOGGER.warning(
                "Cannot connect to '%s' to get state: %s",
                self.name, err
            )
        except asyncio.TimeoutError:
            _LOGGER.warning("Timeout getting state from '%s'", self.name)
        except Exception as err:
            _LOGGER.warning(
                "Failed to get state from '%s': %s (%s)",
                self.name, err, type(err).__name__
            )
        return None
