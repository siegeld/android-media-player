# Android Media Player for Home Assistant

An Android app that exposes itself as a Home Assistant media player entity, allowing you to stream audio from Home Assistant to any Android device.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Android](https://img.shields.io/badge/Android-10%2B-green.svg)](https://developer.android.com)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2023.1%2B-blue.svg)](https://www.home-assistant.io/)

---

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [System Requirements](#system-requirements)
- [Quick Start](#quick-start)
- [Installation](#installation)
  - [Building the Android App](#building-the-android-app)
  - [Deploying to Android Device](#deploying-to-android-device)
  - [Installing Home Assistant Integration](#installing-home-assistant-integration)
- [Update Server](#update-server)
  - [Overview](#update-server-overview)
  - [Running the Update Server](#running-the-update-server)
  - [Web Dashboard](#web-dashboard)
  - [Remote Logging](#remote-logging)
  - [OTA Updates](#ota-updates)
- [Configuration](#configuration)
  - [Android App Settings](#android-app-settings)
  - [Home Assistant Integration](#home-assistant-integration)
- [API Reference](#api-reference)
  - [REST Endpoints](#rest-endpoints)
  - [WebSocket API](#websocket-api)
- [Architecture](#architecture)
- [Debugging](#debugging)
  - [In-App Debug Logging](#in-app-debug-logging)
  - [Android Logcat](#android-logcat)
  - [Home Assistant Logs](#home-assistant-logs)
  - [Remote Logging Dashboard](#remote-logging-dashboard)
- [Robustness Features](#robustness-features)
- [Troubleshooting](#troubleshooting)
- [Project Structure](#project-structure)
- [Contributing](#contributing)
- [License](#license)

---

## Overview

This project provides a complete solution for turning any Android device into a network-controllable media player for Home Assistant. It consists of three main components:

1. **Android App** - A Kotlin-based media player using ExoPlayer with an embedded HTTP/WebSocket server
2. **Home Assistant Integration** - A custom component that creates a `media_player` entity for each Android device
3. **Update Server** - A Docker-based management server for OTA updates, remote logging, and fleet monitoring

---

## Features

### Core Playback
- Stream audio URLs from Home Assistant media sources
- Browse Home Assistant media library (music, playlists, radio stations)
- Full playback controls: play, pause, stop, volume, mute, seek
- Queue management with next/previous track support
- ICY metadata parsing for internet radio streams (artist - title)
- Support for MP3, AAC, OGG, FLAC, WAV, and streaming formats (HLS, DASH)

### Music Assistant Integration
- Full compatibility with Music Assistant
- Real-time state synchronization via DataUpdateCoordinator
- Play/pause toggle support
- Queue and enqueue modes (add, next, replace)

### Connectivity
- Real-time state synchronization via WebSocket
- REST API fallback for command delivery
- Auto-reconnect with exponential backoff (1s → 2s → 4s → ... → 30s max)
- Graceful handling of network interruptions

### Reliability
- Foreground service with persistent notification
- Auto-start on device boot
- Service watchdog with automatic restart on failure
- Robust error handling for invalid URLs and network errors

### Management
- **In-app debug logging** with toggle and scrollable view
- **OTA updates** via central update server
- **Remote logging** to central monitoring dashboard
- **Web-based fleet monitoring** for all connected devices
- Version display in app UI

---

## System Requirements

### Android Device
| Requirement | Minimum | Recommended |
|-------------|---------|-------------|
| Android Version | 10 (API 29) | 12+ (API 31+) |
| RAM | 1 GB | 2+ GB |
| Storage | 50 MB | 100 MB |
| Network | Wi-Fi | Wi-Fi 5GHz |

### Build Environment
| Component | Version |
|-----------|---------|
| JDK | 17+ (auto-downloaded) |
| Android SDK | 34 (auto-downloaded) |
| Gradle | 8.7 (auto-downloaded) |

### Home Assistant
- Version 2023.1 or newer
- Same local network as Android device (no authentication required)

### Update Server (Optional)
- Docker 20.10+
- Docker Compose 2.0+
- 100 MB disk space
- Port 9742 available

---

## Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/your-repo/android-media-player.git
cd android-media-player

# 2. Build the Android app (auto-downloads JDK, SDK, Gradle)
./build.sh

# 3. Deploy to Android device (wireless)
./deploy.sh 192.168.1.100 "Living Room Speaker"

# 4. Copy Home Assistant integration
cp -r custom_components/android_media_player /config/custom_components/

# 5. Restart Home Assistant and add the integration
```

---

## Installation

### Building the Android App

All build tools are installed **locally** in the project directory - nothing is installed globally on your system.

```bash
# First build (downloads ~500MB of tools, then builds)
./build.sh

# Subsequent builds (much faster)
./build.sh

# Clean build
./gradlew clean assembleDebug
```

**Output:** `app/build/outputs/apk/debug/app-debug.apk` (~30 MB)

### Deploying to Android Device

#### Option 1: Wireless Deployment (Recommended)

```bash
# Basic deployment
./deploy.sh <device-ip>

# With custom device name
./deploy.sh <device-ip> "Kitchen Tablet"

# With custom name and port
./deploy.sh <device-ip> "Bedroom Speaker" 8766
```

**Prerequisites for wireless deployment:**

| Android Version | Setup Steps |
|-----------------|-------------|
| 11+ | Settings → Developer options → Wireless debugging → Enable |
| 10 | Connect USB → Run `adb tcpip 5555` → Disconnect USB |

The deploy script will:
1. Connect to the device over the network
2. Install the APK
3. Grant notification permission
4. Configure device name and port
5. Auto-start the media player service

#### Option 2: Manual Installation

1. Transfer `app-debug.apk` to the device
2. Install via file manager or `adb install`
3. Open the app and configure settings
4. Tap "Start Service"

### Installing Home Assistant Integration

1. **Copy the integration:**
   ```bash
   cp -r custom_components/android_media_player /config/custom_components/
   ```

2. **Restart Home Assistant**

3. **Add the integration:**
   - Go to **Settings → Devices & Services → Add Integration**
   - Search for "Android Media Player"
   - Enter the device IP address and port
   - Optionally set a friendly name

4. **Use the media player:**
   - Entity appears as `media_player.<device_name>`
   - Supports all standard media player services
   - Browse media via Home Assistant media browser

---

## Update Server

### Update Server Overview

The update server provides centralized management for your Android Media Player fleet:

- **OTA Updates** - Push new APK versions to all devices
- **Remote Logging** - Collect and view logs from all devices
- **Fleet Monitoring** - Real-time dashboard showing device status
- **Track History** - Log of all media played on each device

### Running the Update Server

```bash
# Start the update server
docker compose up -d

# View logs
docker compose logs -f

# Stop the server
docker compose down
```

The server runs on **port 9742** by default.

### Fleet Setup Script

Use the setup script to quickly configure new tablets:

```bash
# Basic setup
./setup-tablet.sh <device-ip:port> "Device Name"

# With silent updates (Device Owner mode)
./setup-tablet.sh <device-ip:port> "Device Name" --device-owner
```

The setup script:
- Connects to the device via ADB wireless debugging
- Disables Play Protect verification
- Installs the APK and grants permissions
- Configures device name and update server
- Optionally sets Device Owner mode for silent updates

### Device Owner Mode

Device Owner mode enables **silent OTA updates** without user prompts:

**Requirements:**
- All Google accounts must be removed from device first
- Can only be set on a freshly installed app

**Setting via ADB:**
```bash
adb shell dpm set-device-owner com.example.androidmediaplayer/.receiver.DeviceAdminReceiver
```

**Setting via Web Dashboard:**
1. Open the web dashboard at `http://<server>:9742/`
2. Find the device in the device list
3. Click "Enable Silent Updates"

### Web Dashboard

Access the monitoring dashboard at `http://<server-ip>:9742/`

**Dashboard Features:**
- **Unified device view** - Merged view of all devices (ADB + App API)
- Real-time device status (online/offline, device owner, ADB connected)
- Version information for each device
- Live log streaming with filtering
- Track play history

**Device Management Actions:**
| Button | Description |
|--------|-------------|
| Push Update (ADB) | Install latest APK via ADB |
| OTA Update | Trigger app to download and install update |
| Enable Silent Updates | Set Device Owner mode (requires no Google accounts) |
| Disable Protect | Disable Play Protect verification |

**Adding New Devices:**
1. Click "+ Add Device" button
2. Enter device IP, pairing port, and pairing code (from Android Wireless Debugging)
3. Enter connection port
4. Click "Pair & Connect"

### Remote Logging

The Android app automatically sends logs to the update server when configured:

| Log Type | Description |
|----------|-------------|
| Info | Service start/stop, connections |
| Warning | Recoverable errors, reconnections |
| Error | Playback failures, crashes |
| Track | Media playback events |

**Configuring Remote Logging:**

The app automatically enables remote logging when it detects an update server. The server host is saved in app preferences.

### OTA Updates

**How OTA Updates Work:**

1. Build a new APK version (increment `versionCode` in `build.gradle.kts`)
2. The update server automatically detects the new APK
3. Devices check for updates periodically or manually
4. Users tap "Update" to download and install

**Update Server API:**

| Endpoint | Description |
|----------|-------------|
| `GET /version` | Returns current APK version info |
| `GET /apk` | Downloads the APK file |
| `POST /checkin` | Device registration/heartbeat |
| `POST /log` | Submit log entries |
| `POST /track` | Report track played |

---

## Configuration

### Android App Settings

| Setting | Default | Description |
|---------|---------|-------------|
| Device Name | "Android Media Player" | Friendly name shown in Home Assistant |
| Server Port | 8765 | HTTP/WebSocket server port |
| Debug Logging | Off | Enable in-app log display |

**Accessing Settings:**
1. Open the Android Media Player app
2. Settings are displayed on the main screen
3. Changes take effect immediately (service restart not required)

### Home Assistant Integration

**Integration Configuration:**

| Option | Required | Description |
|--------|----------|-------------|
| Host | Yes | IP address of Android device |
| Port | Yes | Server port (default: 8765) |
| Name | No | Custom entity name |

**Example configuration.yaml (alternative to UI):**

```yaml
# Not recommended - use the UI config flow instead
android_media_player:
  - host: 192.168.1.100
    port: 8765
    name: "Living Room Speaker"
```

---

## API Reference

### REST Endpoints

The Android app exposes these REST endpoints:

| Endpoint | Method | Request Body | Response | Description |
|----------|--------|--------------|----------|-------------|
| `/` | GET | - | JSON | Device info and capabilities |
| `/state` | GET | - | JSON | Current player state |
| `/play` | POST | `{"url": "...", "title": "...", "artist": "..."}` | JSON | Play media URL |
| `/play` | POST | - | JSON | Resume playback |
| `/pause` | POST | - | JSON | Pause playback |
| `/stop` | POST | - | JSON | Stop playback |
| `/volume` | POST | `{"level": 0.0-1.0}` | JSON | Set volume (0.0-1.0) |
| `/mute` | POST | `{"muted": true/false}` | JSON | Set mute state |
| `/seek` | POST | `{"position": <ms>}` | JSON | Seek to position in milliseconds |
| `/browse` | GET | `?media_content_id=...` | JSON | Browse media library |

**State Response Schema:**

```json
{
  "state": "idle|playing|paused|buffering",
  "volume": 0.75,
  "muted": false,
  "mediaPosition": 45000,
  "mediaDuration": 180000,
  "mediaTitle": "Song Title",
  "mediaArtist": "Artist Name",
  "mediaUrl": "http://...",
  "error": null
}
```

**States:**
| State | Description |
|-------|-------------|
| `idle` | No media loaded or playback ended |
| `playing` | Media is currently playing |
| `paused` | Media is paused |
| `buffering` | Media is buffering |

### WebSocket API

Connect to `/ws` for real-time updates and bidirectional communication.

**Sending Commands:**

```json
{"command": "play", "url": "http://...", "title": "Song", "artist": "Artist"}
{"command": "play"}
{"command": "pause"}
{"command": "stop"}
{"command": "volume", "level": 0.5}
{"command": "mute", "muted": true}
{"command": "seek", "position": 30000}
```

**Receiving State Updates:**

The server pushes the full state object whenever the player state changes:

```json
{
  "state": "playing",
  "volume": 0.75,
  "muted": false,
  "mediaPosition": 45000,
  "mediaDuration": 180000,
  "mediaTitle": "Song Title",
  "mediaArtist": "Artist Name",
  "mediaUrl": "http://..."
}
```

State updates are pushed on:
- Playback state changes (play, pause, stop, idle)
- Volume/mute changes
- Media metadata changes
- Seek position updates
- ICY metadata from radio streams

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           Update Server (Docker)                         │
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────────────────┐ │
│  │   Web Dashboard │  │  APK Server    │  │    Log Aggregator          │ │
│  │   (Port 9742)   │  │  /version /apk │  │    /log /track /checkin    │ │
│  └────────────────┘  └────────────────┘  └────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    │               │               │
                    ▼               ▼               ▼
┌─────────────────────────┐ ┌─────────────────────────┐ ┌─────────────────┐
│    Android Device 1     │ │    Android Device 2     │ │   Android N     │
│  ┌───────────────────┐  │ │  ┌───────────────────┐  │ │                 │
│  │ MediaPlayerService│  │ │  │ MediaPlayerService│  │ │      ...        │
│  │  ┌─────────────┐  │  │ │  │  ┌─────────────┐  │  │ │                 │
│  │  │  ExoPlayer  │  │  │ │  │  │  ExoPlayer  │  │  │ │                 │
│  │  └─────────────┘  │  │ │  │  └─────────────┘  │  │ │                 │
│  │  ┌─────────────┐  │  │ │  │  ┌─────────────┐  │  │ │                 │
│  │  │ HTTP Server │  │  │ │  │  │ HTTP Server │  │  │ │                 │
│  │  │ (Ktor)      │  │  │ │  │  │ (Ktor)      │  │  │ │                 │
│  │  └─────────────┘  │  │ │  │  └─────────────┘  │  │ │                 │
│  │  ┌─────────────┐  │  │ │  │  ┌─────────────┐  │  │ │                 │
│  │  │RemoteLogger │──┼──┼─┼──┼──│RemoteLogger │──┼──┼─┤                 │
│  │  └─────────────┘  │  │ │  │  └─────────────┘  │  │ │                 │
│  └───────────────────┘  │ │  └───────────────────┘  │ │                 │
└───────────┬─────────────┘ └───────────┬─────────────┘ └─────────────────┘
            │                           │
            │      HTTP/WebSocket       │
            ▼                           ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                           Home Assistant                                 │
│  ┌─────────────────────────────────────────────────────────────────────┐│
│  │                  android_media_player integration                    ││
│  │  ┌──────────────┐  ┌───────────────────┐  ┌────────────────────────┐││
│  │  │ Config Flow  │  │DataUpdateCoordinator│ │ CoordinatorEntity      │││
│  │  │ (Setup UI)   │  │    (WebSocket)     │  │ media_player.living   │││
│  │  │              │  │    + REST API      │  │ media_player.kitchen  │││
│  │  └──────────────┘  └───────────────────┘  └────────────────────────┘││
│  └─────────────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Debugging

### In-App Debug Logging

The Android app includes a built-in debug log viewer:

1. **Enable logging:** Toggle the switch in the Debug Logs section
2. **View logs:** Scroll through the log output in the app
3. **Clear logs:** Tap "Clear" to reset the log buffer
4. **Log levels:** INFO, WARN, ERROR with timestamps

**Logged Events:**
- Service start/stop
- Media playback events
- HTTP requests received
- WebSocket connections
- Errors and exceptions

### Android Logcat

Use the included logcat script for remote debugging:

```bash
# Show all app logs
./logcat.sh 192.168.1.100

# Clear buffer first
./logcat.sh 192.168.1.100 -c

# Warnings and errors only
./logcat.sh 192.168.1.100 -w

# Errors only
./logcat.sh 192.168.1.100 -e
```

**Log Tags:**
| Tag | Component |
|-----|-----------|
| `MainActivity` | UI and settings |
| `MediaPlayerService` | Playback service |
| `MediaHttpServer` | HTTP/WebSocket server |
| `RemoteLogger` | Remote logging client |

### Home Assistant Logs

Add to `configuration.yaml`:

```yaml
logger:
  default: warning
  logs:
    custom_components.android_media_player: debug
```

View logs in **Settings → System → Logs** or `home-assistant.log`.

### Remote Logging Dashboard

Access the web dashboard at `http://<update-server>:9742/`:

- **Filter by device:** Select specific devices
- **Filter by level:** INFO, WARN, ERROR
- **Search:** Full-text search through logs
- **Auto-refresh:** Live updates every 5 seconds

---

## Robustness Features

### Connection Recovery

| Scenario | Behavior |
|----------|----------|
| Android device reboots | Service auto-starts if previously running |
| Home Assistant restarts | Integration reconnects with exponential backoff |
| Network interruption | WebSocket reconnects automatically |
| Command fails via WebSocket | Falls back to REST API |
| Media player service crashes | Watchdog restarts within 30 seconds |

### Service Watchdog

The app includes a watchdog that monitors the media player service:

- Checks service health every 30 seconds
- Automatically restarts if service dies unexpectedly
- Logs restart events for diagnostics
- Maintains playback state across restarts

### Error Handling

| Error Type | Handling |
|------------|----------|
| Invalid URL | Reports error state, logs details |
| Network timeout | Retries with backoff, falls back to REST |
| Unsupported format | Reports error, suggests alternatives |
| Server error (4xx/5xx) | Logs error, reports to remote logger |
| Out of memory | Releases resources, restarts service |

---

## Troubleshooting

### Common Issues

<details>
<summary><strong>App won't start service</strong></summary>

- Check notification permission is granted (Android 13+)
- Verify notification channel isn't blocked in system settings
- Check battery optimization is disabled for the app
- Review logcat for startup errors
</details>

<details>
<summary><strong>Home Assistant can't connect</strong></summary>

- Verify IP address and port are correct
- Ensure both devices are on the same network
- Check Android firewall isn't blocking the port
- Test API directly: `curl http://<device-ip>:8765/state`
- Check Home Assistant logs for connection errors
</details>

<details>
<summary><strong>Media won't play</strong></summary>

- Verify URL is accessible from the Android device
- Check media format is supported
- Review in-app debug logs for errors
- Test with a known-working URL
- Check ExoPlayer error in logcat
</details>

<details>
<summary><strong>Service stops unexpectedly</strong></summary>

- Disable battery optimization for the app
- Check available memory on device
- Review crash logs in logcat
- Watchdog should auto-restart within 30 seconds
- Some manufacturers aggressively kill background services
</details>

<details>
<summary><strong>Connection keeps dropping</strong></summary>

- Check Wi-Fi signal strength
- Verify network stability
- Review HA coordinator logs for disconnect reasons
- Integration auto-reconnects with exponential backoff
</details>

<details>
<summary><strong>Update server can't find APK</strong></summary>

- Ensure APK is built: `./build.sh`
- Check Docker volume mount is correct
- Restart container after rebuilding: `docker compose restart`
- Verify APK path: `app/build/outputs/apk/debug/app-debug.apk`
</details>

<details>
<summary><strong>Deploy script can't connect</strong></summary>

- Enable wireless debugging on Android 11+
- For Android 10: Connect USB first, run `adb tcpip 5555`
- Verify device IP address
- Check port 5555 isn't firewalled
- Ensure ADB is in PATH or use local SDK
</details>

---

## Project Structure

```
android-media-player/
├── app/                                    # Android Application
│   ├── build.gradle.kts                    # App build configuration
│   └── src/main/
│       ├── AndroidManifest.xml             # App manifest
│       ├── java/com/example/androidmediaplayer/
│       │   ├── MediaPlayerApp.kt           # Application class
│       │   ├── MainActivity.kt             # Settings UI + Debug Logs
│       │   ├── model/
│       │   │   └── PlayerState.kt          # Data models
│       │   ├── service/
│       │   │   └── MediaPlayerService.kt   # ExoPlayer + foreground service
│       │   ├── server/
│       │   │   └── MediaHttpServer.kt      # Ktor REST/WebSocket server
│       │   ├── receiver/
│       │   │   ├── BootReceiver.kt         # Auto-start on boot
│       │   │   └── DeviceAdminReceiver.kt  # Device owner for silent updates
│       │   └── util/
│       │       ├── AppLog.kt               # Centralized logging
│       │       ├── RemoteLogger.kt         # Remote logging client
│       │       └── UpdateManager.kt        # OTA update + silent install
│       └── res/                            # Resources (layouts, strings, themes)
│
├── custom_components/android_media_player/ # Home Assistant Integration
│   ├── manifest.json                       # Integration manifest
│   ├── __init__.py                         # Integration setup
│   ├── config_flow.py                      # Configuration UI
│   ├── coordinator.py                      # DataUpdateCoordinator (WebSocket + REST)
│   ├── media_player.py                     # CoordinatorEntity media player
│   ├── const.py                            # Constants
│   └── translations/en.json                # UI strings
│
├── update-server.py                        # Update server (Python)
├── Dockerfile.update-server                # Docker image for update server
├── docker-compose.yml                      # Docker Compose configuration
│
├── android-sdk/                            # Local Android SDK (auto-downloaded)
├── jdk-local/                              # Local JDK 17 (auto-downloaded)
├── gradle-local/                           # Local Gradle installation
│
├── build.sh                                # Build script
├── deploy.sh                               # Deploy to device over network
├── logcat.sh                               # View device logs over network
├── setup-android-sdk.sh                    # SDK setup script
├── setup-tablet.sh                         # Fleet setup script (ADB + Device Owner)
│
└── README.md                               # This file
```

---

## Contributing

Contributions are welcome! Please follow these guidelines:

1. **Fork the repository** and create a feature branch
2. **Follow existing code style** (Kotlin conventions, Python PEP 8)
3. **Test your changes** on a real device
4. **Update documentation** if adding new features
5. **Submit a pull request** with a clear description

### Development Setup

```bash
# Clone your fork
git clone https://github.com/your-username/android-media-player.git
cd android-media-player

# Build and test
./build.sh
./deploy.sh <test-device-ip> "Test Device"

# Run update server locally
docker compose up -d
```

---

## License

MIT License

Copyright (c) 2026

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
