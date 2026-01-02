# Android Media Player for Home Assistant

An Android app that exposes itself as a Home Assistant media player entity, allowing you to stream audio from Home Assistant to any Android device.

## Components

1. **Android App** (`app/`) - Kotlin app with ExoPlayer
2. **Home Assistant Integration** (`custom_components/android_media_player/`) - Custom component

## Features

- Stream audio URLs from Home Assistant
- Browse Home Assistant media sources
- Basic controls: play, pause, stop, volume, mute, seek
- Real-time state sync via WebSocket
- Auto-reconnect with exponential backoff recovery
- Foreground service with notification controls
- Auto-start on device boot
- Robust error handling for invalid URLs/media

## Requirements

- Android 10+ (API 29)
- Home Assistant 2023.1+
- Same local network (no authentication required)

---

## Quick Start

### Build the Android App

All build tools (JDK 17, Android SDK) are installed **locally in this project directory** - nothing is installed globally.

```bash
# First time: downloads JDK 17 + Android SDK (~500MB), then builds
./build.sh

# Subsequent builds are much faster
./build.sh
```

**Output:** `app/build/outputs/apk/debug/app-debug.apk`

### Deploy to Android Device (Wireless)

Use the deploy script to install and auto-start the app over the network:

```bash
# Basic deployment (uses default name and port 8765)
./deploy.sh 192.168.1.100

# With custom device name
./deploy.sh 192.168.1.100 "Living Room Tablet"

# With custom name and port
./deploy.sh 192.168.1.100 "Kitchen Display" 8766
```

**Prerequisites:** Enable wireless debugging on the Android device:
- **Android 11+**: Settings → Developer options → Wireless debugging
- **Android 10**: Connect via USB once, run `adb tcpip 5555`, then disconnect

The script will:
1. Connect to the device over the network
2. Install the APK
3. Grant notification permission
4. Configure device name and port
5. Auto-start the service

**Alternative:** Transfer the APK manually and install on device.

### Install Home Assistant Integration

1. Copy the integration to your Home Assistant config:
   ```bash
   cp -r custom_components/android_media_player /config/custom_components/
   ```

2. Restart Home Assistant

3. Go to **Settings → Devices & Services → Add Integration**

4. Search for "Android Media Player"

5. Enter:
   - **IP Address**: The IP shown in the Android app
   - **Port**: 8765 (default)
   - **Device Name**: Optional friendly name

6. The media player entity will appear as `media_player.<device_name>`

---

## Android App Usage

If using `./deploy.sh`, the app is configured and started automatically.

For manual setup:
1. Open the app on your Android device
2. Set a **Device Name** (e.g., "Living Room Tablet")
3. Keep the default **Port** (8765) or change if needed
4. Tap **Start Service**
5. Note the **IP address** displayed - you'll need this for Home Assistant

The service will:
- Run in the background with a notification
- Auto-restart when the device reboots (if it was running)
- Show playback controls in the notification

---

## Robustness Features

### Connection Recovery

| Scenario | Behavior |
|----------|----------|
| Android device reboots | Service auto-starts if it was running before |
| Home Assistant restarts | Integration reconnects with exponential backoff (5s → 10s → 20s → ... → 5min max) |
| Network interruption | WebSocket reconnects automatically |
| Command fails via WebSocket | Falls back to REST API |

### Error Handling

The app gracefully handles:
- Invalid/unreachable URLs
- Network connection failures
- Unsupported media formats
- Server errors (HTTP 4xx/5xx)

Errors are reported in the player state and logged for debugging.

---

## API Reference

The Android app exposes a REST API on the configured port:

| Endpoint | Method | Body | Description |
|----------|--------|------|-------------|
| `/` | GET | - | Device info and capabilities |
| `/state` | GET | - | Current player state |
| `/play` | POST | `{"url": "...", "title": "...", "artist": "..."}` | Play media URL |
| `/play` | POST | - | Resume playback |
| `/pause` | POST | - | Pause playback |
| `/stop` | POST | - | Stop playback |
| `/volume` | POST | `{"level": 0.0-1.0}` | Set volume |
| `/mute` | POST | `{"muted": true/false}` | Set mute state |
| `/seek` | POST | `{"position": <ms>}` | Seek to position |
| `/ws` | WebSocket | - | Real-time state updates |

### WebSocket Commands

Send JSON messages to `/ws`:

```json
{"command": "play", "url": "http://...", "title": "Song", "artist": "Artist"}
{"command": "pause"}
{"command": "stop"}
{"command": "volume", "level": 0.5}
{"command": "mute", "muted": true}
{"command": "seek", "position": 30000}
```

---

## Debugging

### Android Logs

Use the included logcat wrapper script (connects over the network):

```bash
# Show all app logs
./logcat.sh 192.168.1.100

# Clear buffer first, then show logs
./logcat.sh 192.168.1.100 -c

# Show only warnings and errors
./logcat.sh 192.168.1.100 -w

# Show only errors
./logcat.sh 192.168.1.100 -e

# See all options
./logcat.sh --help
```

Log levels:
- `I` (Info): Service start/stop, connections, media playback
- `D` (Debug): State changes, commands received
- `W` (Warning): Recoverable errors, reconnection attempts
- `E` (Error): Playback failures, connection errors

### Home Assistant Logs

Add to `configuration.yaml`:

```yaml
logger:
  default: warning
  logs:
    custom_components.android_media_player: debug
```

Then check **Settings → System → Logs** or `home-assistant.log`.

### Testing the API

```bash
# Get device info
curl http://<device-ip>:8765/

# Get current state
curl http://<device-ip>:8765/state

# Play a test stream
curl -X POST http://<device-ip>:8765/play \
  -H "Content-Type: application/json" \
  -d '{"url": "http://stream.example.com/audio.mp3", "title": "Test"}'

# Pause
curl -X POST http://<device-ip>:8765/pause

# Set volume to 50%
curl -X POST http://<device-ip>:8765/volume \
  -H "Content-Type: application/json" \
  -d '{"level": 0.5}'
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Home Assistant                          │
│  ┌─────────────────────────────────────────────────────────┐│
│  │            android_media_player integration              ││
│  │  ┌──────────────┐    ┌─────────────┐    ┌────────────┐ ││
│  │  │ Config Flow  │    │ Coordinator │    │   Entity   │ ││
│  │  │  (setup UI)  │    │ (WebSocket) │    │(media_player)│ ││
│  │  └──────────────┘    └──────┬──────┘    └────────────┘ ││
│  └─────────────────────────────┼───────────────────────────┘│
└────────────────────────────────┼────────────────────────────┘
                                 │ HTTP/WebSocket
                                 ▼
┌─────────────────────────────────────────────────────────────┐
│                      Android Device                          │
│  ┌─────────────────────────────────────────────────────────┐│
│  │              MediaPlayerService (Foreground)             ││
│  │  ┌──────────────┐    ┌─────────────┐    ┌────────────┐ ││
│  │  │  ExoPlayer   │◄───│ HTTP Server │◄───│ WebSocket  │ ││
│  │  │  (playback)  │    │   (Ktor)    │    │  Clients   │ ││
│  │  └──────────────┘    └─────────────┘    └────────────┘ ││
│  └─────────────────────────────────────────────────────────┘│
│  ┌─────────────────┐    ┌─────────────────────────────────┐ │
│  │  Boot Receiver  │    │  Notification (media controls)  │ │
│  │  (auto-start)   │    │                                 │ │
│  └─────────────────┘    └─────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

---

## Project Structure

```
android-media-player/
├── app/                                    # Android App
│   ├── build.gradle.kts
│   └── src/main/
│       ├── AndroidManifest.xml
│       ├── java/com/example/androidmediaplayer/
│       │   ├── MediaPlayerApp.kt           # Application class
│       │   ├── MainActivity.kt             # Settings UI
│       │   ├── model/PlayerState.kt        # Data models
│       │   ├── service/MediaPlayerService.kt  # ExoPlayer + foreground service
│       │   ├── server/MediaHttpServer.kt   # Ktor REST/WebSocket server
│       │   └── receiver/BootReceiver.kt    # Auto-start on boot
│       └── res/                            # Layouts, strings, themes
├── custom_components/android_media_player/ # Home Assistant Integration
│   ├── manifest.json
│   ├── __init__.py                         # Integration setup
│   ├── config_flow.py                      # Add device UI
│   ├── coordinator.py                      # WebSocket connection manager
│   ├── media_player.py                     # Media player entity
│   ├── const.py                            # Constants
│   └── translations/en.json                # UI strings
├── android-sdk/                            # Local Android SDK (auto-downloaded)
├── jdk-local/                              # Local JDK 17 (auto-downloaded)
├── gradle-local/                           # Local Gradle installation
├── build.sh                                # Build script
├── deploy.sh                               # Deploy to device over network
├── logcat.sh                               # View device logs over network
├── setup-android-sdk.sh                    # SDK setup script
└── README.md
```

---

## Troubleshooting

### App won't start service
- Check notification permission is granted (Android 13+)
- Check the notification channel isn't blocked in system settings

### Home Assistant can't connect
- Verify the IP address and port are correct
- Ensure both devices are on the same network
- Check Android firewall isn't blocking port 8765
- Try the curl commands above to test the API directly

### Media won't play
- Check the URL is accessible from the Android device
- Look at logcat for error messages
- Verify the media format is supported (MP3, AAC, OGG, FLAC, etc.)

### Service stops when app is closed
- The foreground service should keep running
- Some manufacturers aggressively kill background apps
- Try disabling battery optimization for the app

### Connection keeps dropping
- Check network stability
- The integration will auto-reconnect with exponential backoff
- Look at HA logs for connection error details

### Deploy script can't connect
- Ensure wireless debugging is enabled on the device
- Check the device IP address is correct
- For Android 11+: Use the IP shown in Wireless debugging settings
- For Android 10: May need USB connection first to run `adb tcpip 5555`
- Check that port 5555 isn't blocked by a firewall

---

## License

MIT License
