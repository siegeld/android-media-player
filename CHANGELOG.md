# Changelog

All notable changes to the Android Media Player for Home Assistant project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added
- **Music Assistant Compatibility**
  - Full integration with Music Assistant via DataUpdateCoordinator pattern
  - CoordinatorEntity for proper state propagation to all HA subscribers
  - Play/pause toggle support (`async_media_play_pause`)
  - Device class "speaker" for proper categorization
  - TURN_ON/TURN_OFF feature support
- **Queue Management**
  - Local queue support with next/previous track navigation
  - Enqueue modes: add, next, replace
  - Auto-advance to next track when playback ends naturally
- **Radio Stream Support**
  - ICY metadata parsing for internet radio streams
  - Automatic artist/title extraction from "Artist - Title" format
- **Enhanced Metadata Handling**
  - Fetch title from media_source browse API
  - Extract metadata from media_source URI paths
  - Filter out hash-like IDs from metadata
- In-app debug logging with toggle switch and scrollable log viewer
- OTA (Over-The-Air) update functionality via central update server
- Remote logging to central monitoring dashboard
- Web-based fleet monitoring dashboard for all connected devices
- Service watchdog with automatic restart on failure (30-second check interval)
- Version display in the app UI header
- Docker-based update server with web dashboard
- Device check-in and heartbeat functionality
- Track play history logging
- Log filtering and search in web dashboard

### Changed
- **Home Assistant Integration Architecture**
  - Coordinator now extends `DataUpdateCoordinator` instead of custom class
  - Media player entity extends `CoordinatorEntity` for automatic state sync
  - Uses `async_set_updated_data()` for proper listener notification
  - Uses `dt_util.utcnow()` for position timestamps (HA requirement)
- **Reconnection Behavior**
  - Reduced reconnect backoff: 1s initial, 30s max (was 5s-5min)
  - Improved reconnect loop handling after failures
- **Logging**
  - Reduced verbose INFO logs to DEBUG level for cleaner logs
  - Kept important connection and error messages at INFO/WARNING
- Upgraded Android Gradle Plugin from 8.2.0 to 8.5.0 for Java 21 compatibility
- Upgraded Gradle from 8.2 to 8.7
- Upgraded Kotlin from 1.9.20 to 1.9.23
- Changed update server port from 8888 to 9742
- Removed obsolete `version` attribute from docker-compose.yml

### Fixed
- String index out of range error for radio stations with short titles
- HA integration not updating state after WebSocket reconnect
- REST API commands now send empty JSON `{}` instead of `null` for commands without parameters
- Java 21 jlink compatibility issue with Android SDK's core-for-system-modules.jar
- Stale file handle error when APK is rebuilt while Docker container is running

---

## [1.0.0] - 2026-01-01

### Added
- Initial release of Android Media Player for Home Assistant
- Kotlin-based Android app with ExoPlayer for audio playback
- Embedded HTTP/WebSocket server using Ktor
- Home Assistant custom integration with config flow
- Real-time state synchronization via WebSocket
- REST API fallback for command delivery
- Auto-reconnect with exponential backoff
- Foreground service with notification controls
- Auto-start on device boot
- Media browser support for Home Assistant media sources
- Full playback controls: play, pause, stop, volume, mute, seek
- Support for MP3, AAC, OGG, FLAC, WAV, HLS, DASH formats
- Wireless deployment script (deploy.sh)
- Remote logcat viewing script (logcat.sh)
- Self-contained build system with local JDK, SDK, and Gradle

### Technical Details

#### Android App
- Minimum SDK: Android 10 (API 29)
- Target SDK: Android 34
- Language: Kotlin 1.9.x
- Build System: Gradle 8.7 with AGP 8.5.0
- Media Player: ExoPlayer (Media3)
- HTTP Server: Ktor

#### Home Assistant Integration
- Minimum HA Version: 2023.1
- Integration Type: Local Push (DataUpdateCoordinator)
- Entity Platform: media_player (CoordinatorEntity)
- Device Class: speaker
- Features: play, pause, stop, volume, mute, seek, browse_media, next_track, previous_track, turn_on, turn_off

---

## Version History Summary

| Version | Date | Highlights |
|---------|------|------------|
| 1.0.0 | 2026-01-01 | Initial release with core functionality |
| 1.1.0 | TBD | Music Assistant support, OTA updates, queue management |

---

## Migration Guide

### Upgrading from 1.0.0 to 1.1.0

1. **Update the Android app:**
   - Build the new APK: `./build.sh`
   - Deploy to devices: `./deploy.sh <device-ip>`
   - Or use OTA update if update server is running

2. **Update Home Assistant integration:**
   - Copy new integration files:
     ```bash
     cp -r custom_components/android_media_player /config/custom_components/
     ```
   - Restart Home Assistant
   - The integration now uses DataUpdateCoordinator for better Music Assistant compatibility

3. **Start the update server (optional):**
   ```bash
   docker compose up -d
   ```

4. **Music Assistant users:**
   - Remove and re-add player in Music Assistant settings to ensure fresh entity subscription
   - State changes should now propagate correctly

5. **New features available:**
   - Toggle debug logging in the app
   - View logs in the web dashboard
   - Monitor all devices from a single dashboard
   - Use queue features with enqueue modes

---

## Deprecation Notices

None at this time.

---

## Security

### Reporting Vulnerabilities

If you discover a security vulnerability, please report it privately by:
1. Opening a private issue on GitHub
2. Emailing the maintainers directly

Do not disclose security vulnerabilities publicly until a fix is available.

### Security Considerations

- The app runs an HTTP server accessible on the local network
- No authentication is implemented (relies on network security)
- Do not expose the app port to the internet
- The update server should be run on a trusted network

---

## Contributors

Thanks to everyone who has contributed to this project!

---

## Links

- [GitHub Repository](https://github.com/your-repo/android-media-player)
- [Home Assistant Community](https://community.home-assistant.io/)
- [Issue Tracker](https://github.com/your-repo/android-media-player/issues)
