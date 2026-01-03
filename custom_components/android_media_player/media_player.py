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

        # Queue management
        self._queue: list[dict] = []  # List of {url, title, artist}
        self._queue_index: int = 0
        self._previous_state: str | None = None
        self._user_stopped: bool = False

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
            | MediaPlayerEntityFeature.NEXT_TRACK
            | MediaPlayerEntityFeature.PREVIOUS_TRACK
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
        self._user_stopped = True
        self._queue = []
        self._queue_index = 0
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
        # Log all kwargs for debugging - use INFO temporarily to ensure we see it
        _LOGGER.info("async_play_media FULL kwargs: %s", kwargs)

        # Extract metadata from various possible locations
        extra = kwargs.get("extra", {})
        title = extra.get("title")
        artist = extra.get("artist")

        # Check enqueue mode
        enqueue = kwargs.get("enqueue")
        _LOGGER.info(
            "async_play_media for '%s': type=%s, media_id=%s, enqueue=%s",
            self._device_name, media_type, media_id, enqueue
        )

        # Resolve media_source URIs to actual URLs and get metadata
        resolved_url = media_id
        if media_source.is_media_source_id(media_id):
            _LOGGER.debug("Resolving media_source URI: %s", media_id)
            try:
                # First, try to browse this specific item to get its metadata
                if not title:
                    try:
                        browse_result = await media_source.async_browse_media(
                            self.hass, media_id
                        )
                        if browse_result:
                            _LOGGER.info(
                                "Got browse metadata: title=%s, media_class=%s, thumbnail=%s",
                                browse_result.title,
                                getattr(browse_result, 'media_class', None),
                                getattr(browse_result, 'thumbnail', None)
                            )
                            if browse_result.title:
                                title = browse_result.title
                            # Try to get artist from media_content_id path
                            # Format: media-source://dlna_dms/server/:parentId/:itemId
                            if not artist:
                                content_id = getattr(browse_result, 'media_content_id', '') or media_id
                                # Try to get parent folder info for artist/album
                                parts = content_id.split('/')
                                _LOGGER.debug("media_content_id parts: %s", parts)
                    except Exception as browse_err:
                        _LOGGER.debug("Could not browse media for metadata: %s", browse_err)

                # Log ALL available browse_result attributes for debugging
                if browse_result:
                    _LOGGER.info(
                        "BrowseMedia ALL attrs: %s",
                        {k: getattr(browse_result, k, None) for k in dir(browse_result) if not k.startswith('_')}
                    )

                # Now resolve to playable URL
                sourced_media = await media_source.async_resolve_media(
                    self.hass, media_id, self.entity_id
                )
                resolved_url = sourced_media.url
                _LOGGER.debug("Resolved to URL: %s", resolved_url)
            except media_source.Unresolvable as err:
                _LOGGER.error("Cannot resolve media_source URI %s: %s", media_id, err)
                return

        # Helper to check if string looks like a hash/ID (skip these for metadata)
        def _is_hash_like(s: str) -> bool:
            if not s or len(s) < 8:
                return False
            # Skip if mostly hex chars or alphanumeric without spaces
            clean = s.replace("-", "").replace("_", "")
            if len(clean) > 16 and clean.isalnum() and not any(c.isspace() for c in s):
                return True
            return False

        # Try to extract title from the original media_id if not provided
        if not title:
            # media_id often contains path info like "media-source://media_source/local/Music/Artist/Album/Track.mp3"
            from urllib.parse import urlparse, unquote
            parsed_id = urlparse(media_id)
            if parsed_id.path:
                path_parts = [unquote(p) for p in parsed_id.path.split("/") if p]
                if path_parts:
                    filename = path_parts[-1]
                    # Remove common extensions
                    for ext in [".mp3", ".m4a", ".flac", ".wav", ".ogg", ".aac", ".opus"]:
                        if filename.lower().endswith(ext):
                            filename = filename[:-len(ext)]
                            break
                    # Skip generic names and hash-like IDs
                    if filename and filename not in ("file", "object") and not _is_hash_like(filename):
                        title = filename
                    # Try to get artist from parent folder
                    if not artist and len(path_parts) >= 2:
                        potential_artist = path_parts[-2]
                        # Skip generic folder names and hash-like IDs
                        if (potential_artist.lower() not in ("music", "media", "audio", "local", "object")
                            and not _is_hash_like(potential_artist)):
                            artist = potential_artist

        # Don't try to extract from Plex/hash URLs - they don't have useful info
        # Title will come from stream metadata instead

        # Process URL to ensure it's playable
        resolved_url = async_process_play_media_url(self.hass, resolved_url)

        # Create queue item
        queue_item = {"url": resolved_url, "title": title, "artist": artist}

        # Handle enqueue modes
        if enqueue == "add":
            # Add to end of queue
            self._queue.append(queue_item)
            _LOGGER.info("Added to queue: %s (queue size: %d)", title, len(self._queue))
            return
        elif enqueue == "next":
            # Add after current track
            insert_pos = self._queue_index + 1
            self._queue.insert(insert_pos, queue_item)
            _LOGGER.info("Inserted at position %d: %s", insert_pos, title)
            return
        elif enqueue == "replace":
            # Clear queue and play
            self._queue = [queue_item]
            self._queue_index = 0
        else:
            # Default: clear queue and play immediately
            self._queue = [queue_item]
            self._queue_index = 0

        _LOGGER.info(
            "Playing on '%s': url=%s, title=%s, artist=%s",
            self._device_name, resolved_url, title, artist
        )

        result = await self.coordinator.async_send_command(
            "play",
            url=resolved_url,
            title=title,
            artist=artist,
        )
        if not result:
            _LOGGER.warning(
                "Failed to play media on '%s': url=%s",
                self._device_name, resolved_url
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

    async def async_media_next_track(self) -> None:
        """Play the next track in the queue."""
        if not self._queue or self._queue_index >= len(self._queue) - 1:
            _LOGGER.warning(
                "No next track available for '%s' (index=%d, queue size=%d). "
                "Queue is populated when tracks are added with enqueue mode.",
                self._device_name, self._queue_index, len(self._queue)
            )
            return

        self._queue_index += 1
        track = self._queue[self._queue_index]
        _LOGGER.info("Playing next track %d/%d: %s",
                    self._queue_index + 1, len(self._queue), track.get("title"))

        await self.coordinator.async_send_command(
            "play",
            url=track["url"],
            title=track.get("title"),
            artist=track.get("artist"),
        )

    async def async_media_previous_track(self) -> None:
        """Play the previous track in the queue."""
        if not self._queue or self._queue_index <= 0:
            _LOGGER.debug("No previous track available (index=%d)", self._queue_index)
            return

        self._queue_index -= 1
        track = self._queue[self._queue_index]
        _LOGGER.info("Playing previous track %d/%d: %s",
                    self._queue_index + 1, len(self._queue), track.get("title"))

        await self.coordinator.async_send_command(
            "play",
            url=track["url"],
            title=track.get("title"),
            artist=track.get("artist"),
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        state = self.coordinator.state.get("state")
        _LOGGER.debug(
            "Entity '%s' update: state=%s (prev=%s), queue=%d/%d, user_stopped=%s",
            self._device_name, state, self._previous_state,
            self._queue_index + 1, len(self._queue), self._user_stopped
        )

        # Auto-advance to next track when track ends naturally
        # Only if: was playing -> now idle, user didn't stop, and more tracks in queue
        if (state == "idle" and
            self._previous_state == "playing" and
            not self._user_stopped and
            self._queue and
            self._queue_index < len(self._queue) - 1):
            _LOGGER.info("Track ended naturally, advancing to next track...")
            self.hass.async_create_task(self.async_media_next_track())

        # Reset user_stopped flag when playback starts
        if state == "playing":
            self._user_stopped = False

        self._previous_state = state
        self.async_write_ha_state()
