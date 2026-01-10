# Contributing to Android Media Player for Home Assistant

Thank you for your interest in contributing to this project! This document provides guidelines and information for contributors.

---

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Environment](#development-environment)
- [Project Structure](#project-structure)
- [Coding Standards](#coding-standards)
- [Testing](#testing)
- [Submitting Changes](#submitting-changes)
- [Issue Guidelines](#issue-guidelines)
- [Pull Request Process](#pull-request-process)

---

## Code of Conduct

This project follows a standard code of conduct:

- **Be respectful** - Treat all contributors with respect
- **Be constructive** - Provide helpful feedback
- **Be inclusive** - Welcome contributors of all backgrounds
- **Be patient** - Understand that maintainers are volunteers

---

## Getting Started

### Prerequisites

- Git
- Docker and Docker Compose (for update server development)
- Android device or emulator for testing
- Basic knowledge of Kotlin, Python, and/or Home Assistant

### Fork and Clone

```bash
# Fork the repository on GitHub, then clone your fork
git clone https://github.com/YOUR-USERNAME/android-media-player.git
cd android-media-player

# Add upstream remote
git remote add upstream https://github.com/ORIGINAL-OWNER/android-media-player.git
```

### Build the Project

```bash
# Build Android app (auto-downloads JDK, SDK, Gradle)
./build.sh

# Start update server
docker compose up -d
```

---

## Development Environment

### Android App Development

The Android app is written in Kotlin and uses:

| Component | Technology |
|-----------|------------|
| Language | Kotlin 1.9.x |
| Build System | Gradle 8.7 + AGP 8.5.0 |
| Media Player | ExoPlayer (Media3) |
| HTTP Server | Ktor |
| UI | Android Views with ViewBinding |
| Async | Kotlin Coroutines + StateFlow |

**IDE Setup:**
1. Install Android Studio (latest stable)
2. Open the project root directory
3. Let Gradle sync complete
4. Run configurations are pre-configured

**Building:**
```bash
# Debug build
./gradlew assembleDebug

# Release build (requires signing config)
./gradlew assembleRelease

# Clean build
./gradlew clean assembleDebug
```

### Home Assistant Integration Development

The integration is written in Python and follows Home Assistant conventions:

| Component | Technology |
|-----------|------------|
| Language | Python 3.11+ |
| Framework | Home Assistant Core |
| Async | asyncio + aiohttp |
| Config | Config Flow |

**Testing the integration:**
1. Copy to HA config: `cp -r custom_components/android_media_player /config/custom_components/`
2. Restart Home Assistant
3. Check logs for errors: `tail -f /config/home-assistant.log`

### Update Server Development

The update server is a Python HTTP server:

| Component | Technology |
|-----------|------------|
| Language | Python 3.11 |
| Server | http.server (stdlib) |
| Container | Docker |

**Local development:**
```bash
# Run directly (without Docker)
python3 update-server.py

# Run with Docker
docker compose up --build
```

---

## Project Structure

```
android-media-player/
├── app/                        # Android App
│   ├── src/main/java/         # Kotlin source code
│   │   └── com/example/androidmediaplayer/
│   │       ├── MainActivity.kt
│   │       ├── MediaPlayerApp.kt
│   │       ├── model/         # Data classes
│   │       ├── service/       # Background services
│   │       ├── server/        # HTTP server
│   │       ├── receiver/      # Broadcast receivers
│   │       └── util/          # Utilities
│   ├── src/main/res/          # Android resources
│   └── build.gradle.kts       # App build config
│
├── custom_components/         # Home Assistant Integration
│   └── android_media_player/
│       ├── __init__.py        # Setup
│       ├── config_flow.py     # Configuration UI
│       ├── coordinator.py     # Data coordinator
│       ├── media_player.py    # Entity implementation
│       └── const.py           # Constants
│
├── update-server.py           # Update server
├── Dockerfile.update-server   # Docker image
├── docker-compose.yml         # Docker compose config
│
├── build.sh                   # Build script
├── deploy.sh                  # Deployment script
├── logcat.sh                  # Log viewing script
│
├── README.md                  # Main documentation
├── CHANGELOG.md               # Version history
└── CONTRIBUTING.md            # This file
```

---

## Coding Standards

### Kotlin (Android App)

Follow the [Kotlin Coding Conventions](https://kotlinlang.org/docs/coding-conventions.html):

```kotlin
// Use meaningful names
class MediaPlayerService : Service() { ... }

// Use data classes for models
data class PlayerState(
    val state: String,
    val volume: Float,
    val position: Long
)

// Use coroutines for async operations
suspend fun fetchData(): Result<Data> {
    return withContext(Dispatchers.IO) {
        // ...
    }
}

// Document public APIs
/**
 * Starts media playback from the given URL.
 * @param url The media URL to play
 * @param title Optional title for the media
 */
fun play(url: String, title: String? = null)
```

### Python (Home Assistant Integration)

Follow [PEP 8](https://pep8.org/) and Home Assistant conventions:

```python
# Use type hints
async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry
) -> bool:
    """Set up the integration from a config entry."""
    ...

# Use constants
DOMAIN = "android_media_player"
DEFAULT_PORT = 8765

# Use docstrings
class AndroidMediaPlayer(MediaPlayerEntity):
    """Representation of an Android Media Player."""

    @property
    def state(self) -> MediaPlayerState:
        """Return the state of the player."""
        return self._state
```

### Commit Messages

Follow the [Conventional Commits](https://www.conventionalcommits.org/) specification:

```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

**Types:**
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation only
- `style`: Code style (formatting, etc.)
- `refactor`: Code refactoring
- `test`: Adding tests
- `chore`: Maintenance tasks

**Examples:**
```
feat(android): add in-app debug logging

- Add AppLog singleton for centralized logging
- Add log viewer in MainActivity
- Add toggle to enable/disable logging

Closes #42
```

```
fix(ha): handle WebSocket reconnection correctly

The coordinator now properly waits for the connection
to be fully established before sending commands.

Fixes #37
```

---

## Testing

### Android App Testing

```bash
# Run unit tests
./gradlew test

# Run instrumented tests (requires device/emulator)
./gradlew connectedAndroidTest
```

**Manual testing checklist:**
- [ ] App installs and launches
- [ ] Service starts and shows notification
- [ ] HTTP API responds correctly
- [ ] WebSocket connections work
- [ ] Media playback works
- [ ] Volume control works
- [ ] Service survives app closure
- [ ] Service auto-starts on boot

### Home Assistant Integration Testing

```bash
# Run pytest (from HA development environment)
pytest tests/components/android_media_player/
```

**Manual testing checklist:**
- [ ] Integration adds via config flow
- [ ] Entity appears in HA
- [ ] Play/pause/stop work
- [ ] Volume control works
- [ ] State updates in real-time
- [ ] Media browser works
- [ ] Reconnection works after HA restart

### Update Server Testing

```bash
# Test endpoints
curl http://localhost:9742/version
curl http://localhost:9742/api/devices
curl http://localhost:9742/api/logs
```

---

## Submitting Changes

### Before Submitting

1. **Test your changes** thoroughly
2. **Update documentation** if needed
3. **Add changelog entry** for significant changes
4. **Run linters** and fix any issues
5. **Rebase on latest** upstream changes

### Creating a Pull Request

1. Push your branch to your fork
2. Open a PR against the `main` branch
3. Fill out the PR template completely
4. Wait for CI checks to pass
5. Respond to review feedback

---

## Issue Guidelines

### Bug Reports

Include:
- **Environment**: Android version, HA version, device model
- **Steps to reproduce**: Clear, numbered steps
- **Expected behavior**: What should happen
- **Actual behavior**: What actually happens
- **Logs**: Relevant error messages
- **Screenshots**: If applicable

### Feature Requests

Include:
- **Use case**: Why is this feature needed?
- **Proposed solution**: How should it work?
- **Alternatives considered**: Other approaches
- **Additional context**: Mockups, examples

### Questions

Before asking:
1. Check the README documentation
2. Search existing issues
3. Check Home Assistant community forums

---

## Pull Request Process

### PR Requirements

- [ ] Code follows project style guidelines
- [ ] Tests pass locally
- [ ] Documentation updated (if applicable)
- [ ] Changelog updated (for user-facing changes)
- [ ] PR description explains the change
- [ ] Linked to related issues

### Review Process

1. **Automated checks**: CI must pass
2. **Code review**: At least one maintainer review
3. **Testing**: Manual testing may be requested
4. **Approval**: Maintainer approves the PR
5. **Merge**: Squash and merge to main

### After Merge

- Your contribution will be included in the next release
- You'll be credited in the release notes
- The issue will be closed automatically

---

## Questions?

- **GitHub Issues**: For bugs and feature requests
- **GitHub Discussions**: For questions and ideas
- **Home Assistant Community**: For HA-specific questions

Thank you for contributing!
