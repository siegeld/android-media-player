"""Media Player platform for Android Media Player integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.media_player import (
    BrowseMedia,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.components.media_player.browse_media import async_process_play_media_url
from homeassistant.components import media_source
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, CONF_HOST, CONF_PORT, CONF_NAME
from .coordinator import AndroidMediaPlayerCoordinator

_LOGGER = logging.getLogger(__name__)

STATE_MAP = {
    "idle": MediaPlayerState.IDLE,
    "playing": MediaPlayerState.PLAYING,
    "paused": MediaPlayerState.PAUSED,
    "buffering": MediaPlayerState.BUFFERING,
    "off": MediaPlayerState.OFF,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Android Media Player from a config entry."""
    coordinator: AndroidMediaPlayerCoordinator = hass.data[DOMAIN][entry.entry_id]

    _LOGGER.info(
        "Setting up Android Media Player entity for '%s' (%s:%s)",
        entry.data.get(CONF_NAME),
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
    )

    async_add_entities([AndroidMediaPlayerEntity(coordinator, entry)])


class AndroidMediaPlayerEntity(MediaPlayerEntity):
    """Representation of an Android Media Player."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(
        self,
        coordinator: AndroidMediaPlayerCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the media player."""
        self.coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.data[CONF_HOST]}_{entry.data[CONF_PORT]}"
        self._device_name = entry.data.get(CONF_NAME, "Android Media Player")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._attr_unique_id)},
            name=self._device_name,
            manufacturer="Android",
            model="Media Player",
            sw_version="1.0",
        )
        self._remove_listener: callable | None = None

        _LOGGER.debug(
            "Initialized AndroidMediaPlayerEntity: unique_id=%s, name=%s",
            self._attr_unique_id, self._device_name
        )

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        """Return the supported features."""
        return (
            MediaPlayerEntityFeature.PLAY
            | MediaPlayerEntityFeature.PAUSE
            | MediaPlayerEntityFeature.STOP
            | MediaPlayerEntityFeature.VOLUME_SET
            | MediaPlayerEntityFeature.VOLUME_MUTE
            | MediaPlayerEntityFeature.PLAY_MEDIA
            | MediaPlayerEntityFeature.BROWSE_MEDIA
            | MediaPlayerEntityFeature.SEEK
        )

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.available

    @property
    def state(self) -> MediaPlayerState | None:
        """Return the state of the player."""
        if not self.coordinator.available:
            return MediaPlayerState.OFF

        state_str = self.coordinator.state.get("state", "idle")
        return STATE_MAP.get(state_str, MediaPlayerState.IDLE)

    @property
    def volume_level(self) -> float | None:
        """Return the volume level (0..1)."""
        return self.coordinator.state.get("volume", 1.0)

    @property
    def is_volume_muted(self) -> bool | None:
        """Return True if volume is muted."""
        return self.coordinator.state.get("muted", False)

    @property
    def media_title(self) -> str | None:
        """Return the title of current playing media."""
        return self.coordinator.state.get("mediaTitle")

    @property
    def media_artist(self) -> str | None:
        """Return the artist of current playing media."""
        return self.coordinator.state.get("mediaArtist")

    @property
    def media_duration(self) -> int | None:
        """Return the duration of current playing media in seconds."""
        duration_ms = self.coordinator.state.get("mediaDuration")
        if duration_ms:
            return duration_ms // 1000
        return None

    @property
    def media_position(self) -> int | None:
        """Return the position of current playing media in seconds."""
        position_ms = self.coordinator.state.get("mediaPosition")
        if position_ms:
            return position_ms // 1000
        return None

    @property
    def media_content_type(self) -> MediaType | None:
        """Return the content type of current playing media."""
        if self.coordinator.state.get("mediaUrl"):
            return MediaType.MUSIC
        return None

    async def async_added_to_hass(self) -> None:
        """Run when entity is added to hass."""
        await super().async_added_to_hass()
        _LOGGER.info(
            "Android Media Player '%s' added to Home Assistant",
            self._device_name
        )
        self._remove_listener = self.coordinator.add_listener(
            self._handle_coordinator_update
        )

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass."""
        _LOGGER.info(
            "Android Media Player '%s' being removed from Home Assistant",
            self._device_name
        )
        if self._remove_listener:
            self._remove_listener()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        _LOGGER.debug(
            "Entity '%s' received coordinator update: available=%s, state=%s",
            self._device_name,
            self.coordinator.available,
            self.coordinator.state.get("state")
        )
        self.async_write_ha_state()

    async def async_media_play(self) -> None:
        """Send play command."""
        _LOGGER.debug("async_media_play called for '%s'", self._device_name)
        result = await self.coordinator.async_send_command("play")
        if not result:
            _LOGGER.warning("Failed to send play command to '%s'", self._device_name)

    async def async_media_pause(self) -> None:
        """Send pause command."""
        _LOGGER.debug("async_media_pause called for '%s'", self._device_name)
        result = await self.coordinator.async_send_command("pause")
        if not result:
            _LOGGER.warning("Failed to send pause command to '%s'", self._device_name)

    async def async_media_stop(self) -> None:
        """Send stop command."""
        _LOGGER.debug("async_media_stop called for '%s'", self._device_name)
        result = await self.coordinator.async_send_command("stop")
        if not result:
            _LOGGER.warning("Failed to send stop command to '%s'", self._device_name)

    async def async_set_volume_level(self, volume: float) -> None:
        """Set volume level (0..1)."""
        _LOGGER.debug(
            "async_set_volume_level called for '%s': volume=%s",
            self._device_name, volume
        )
        result = await self.coordinator.async_send_command("volume", level=volume)
        if not result:
            _LOGGER.warning(
                "Failed to set volume to %s on '%s'",
                volume, self._device_name
            )

    async def async_mute_volume(self, mute: bool) -> None:
        """Mute/unmute the volume."""
        _LOGGER.debug(
            "async_mute_volume called for '%s': mute=%s",
            self._device_name, mute
        )
        result = await self.coordinator.async_send_command("mute", muted=mute)
        if not result:
            _LOGGER.warning(
                "Failed to set mute to %s on '%s'",
                mute, self._device_name
            )

    async def async_media_seek(self, position: float) -> None:
        """Seek to a position in seconds."""
        position_ms = int(position * 1000)
        _LOGGER.debug(
            "async_media_seek called for '%s': position=%ss (%sms)",
            self._device_name, position, position_ms
        )
        result = await self.coordinator.async_send_command("seek", position=position_ms)
        if not result:
            _LOGGER.warning(
                "Failed to seek to %ss on '%s'",
                position, self._device_name
            )

    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        """Play a piece of media."""
        title = kwargs.get("enqueue") or kwargs.get("extra", {}).get("title")
        artist = kwargs.get("extra", {}).get("artist")

        _LOGGER.info(
            "async_play_media called for '%s': type=%s, media_id=%s, title=%s, artist=%s",
            self._device_name, media_type, media_id, title, artist
        )

        # Resolve media_source URIs to actual URLs
        if media_source.is_media_source_id(media_id):
            _LOGGER.debug("Resolving media_source URI: %s", media_id)
            try:
                sourced_media = await media_source.async_resolve_media(
                    self.hass, media_id, self.entity_id
                )
                media_id = sourced_media.url
                # Use the mime_type as a hint if no title provided
                if not title and hasattr(sourced_media, 'mime_type'):
                    _LOGGER.debug("Resolved to URL: %s (mime: %s)", media_id, sourced_media.mime_type)
            except media_source.Unresolvable as err:
                _LOGGER.error("Cannot resolve media_source URI %s: %s", media_id, err)
                return

        # Process URL to ensure it's playable (handles local file paths, etc.)
        media_id = async_process_play_media_url(self.hass, media_id)

        _LOGGER.info("Playing URL on '%s': %s", self._device_name, media_id)

        result = await self.coordinator.async_send_command(
            "play",
            url=media_id,
            title=title,
            artist=artist,
        )
        if not result:
            _LOGGER.warning(
                "Failed to play media on '%s': url=%s",
                self._device_name, media_id
            )

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Implement the websocket media browsing helper.

        This allows browsing Home Assistant's media sources.
        """
        _LOGGER.debug(
            "async_browse_media called for '%s': content_type=%s, content_id=%s",
            self._device_name, media_content_type, media_content_id
        )

        try:
            result = await media_source.async_browse_media(
                self.hass,
                media_content_id,
                content_filter=lambda item: item.media_content_type.startswith("audio/"),
            )
            _LOGGER.debug(
                "Browse media returned %d children for '%s'",
                len(result.children) if result.children else 0,
                self._device_name
            )
            return result
        except Exception as err:
            _LOGGER.error(
                "Error browsing media for '%s': %s (%s)",
                self._device_name, err, type(err).__name__,
                exc_info=True
            )
            raise
