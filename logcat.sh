#!/bin/bash
# Logcat wrapper script for Android Media Player debugging
# Uses local Android SDK - no global installation required

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDK_DIR="${SCRIPT_DIR}/android-sdk"
ADB="${SDK_DIR}/platform-tools/adb"

# Check if SDK is installed
if [ ! -x "${ADB}" ]; then
    echo "Error: Android SDK not found."
    echo "Run ./build.sh first to download the SDK."
    exit 1
fi

# App component tags
TAGS="MediaPlayerService:* MediaHttpServer:* MainActivity:* BootReceiver:* MediaPlayerApp:*"

# Parse arguments
CLEAR=false
LEVEL=""
DEVICE_IP=""
ADB_PORT="5555"

while [[ $# -gt 0 ]]; do
    case $1 in
        -c|--clear)
            CLEAR=true
            shift
            ;;
        -v|--verbose)
            LEVEL="V"
            shift
            ;;
        -d|--debug)
            LEVEL="D"
            shift
            ;;
        -i|--info)
            LEVEL="I"
            shift
            ;;
        -w|--warning)
            LEVEL="W"
            shift
            ;;
        -e|--error)
            LEVEL="E"
            shift
            ;;
        -h|--help)
            echo "Usage: $0 <device-ip> [OPTIONS]"
            echo ""
            echo "View logcat output filtered for Android Media Player components."
            echo ""
            echo "Arguments:"
            echo "  device-ip       IP address of Android device (required)"
            echo ""
            echo "Options:"
            echo "  -c, --clear     Clear logcat buffer before starting"
            echo "  -v, --verbose   Show all log levels (V and above)"
            echo "  -d, --debug     Show debug and above (D, I, W, E)"
            echo "  -i, --info      Show info and above (I, W, E)"
            echo "  -w, --warning   Show warnings and errors only (W, E)"
            echo "  -e, --error     Show errors only (E)"
            echo "  -h, --help      Show this help message"
            echo ""
            echo "Components monitored:"
            echo "  - MediaPlayerService (foreground service, ExoPlayer)"
            echo "  - MediaHttpServer (Ktor HTTP/WebSocket server)"
            echo "  - MainActivity (settings UI)"
            echo "  - BootReceiver (auto-start on boot)"
            echo "  - MediaPlayerApp (application class)"
            echo ""
            echo "Examples:"
            echo "  $0 192.168.1.100        # Show all app logs"
            echo "  $0 192.168.1.100 -c     # Clear buffer, then show logs"
            echo "  $0 192.168.1.100 -w     # Show only warnings and errors"
            echo "  $0 192.168.1.100 -c -e  # Clear buffer, show only errors"
            exit 0
            ;;
        *)
            # First non-option argument is the device IP
            if [ -z "$DEVICE_IP" ] && [[ ! "$1" =~ ^- ]]; then
                DEVICE_IP="$1"
            else
                echo "Unknown option: $1"
                echo "Use --help for usage information."
                exit 1
            fi
            shift
            ;;
    esac
done

# Require device IP
if [ -z "$DEVICE_IP" ]; then
    echo "Usage: $0 <device-ip> [OPTIONS]"
    echo ""
    echo "Examples:"
    echo "  $0 192.168.1.100        # Show all app logs"
    echo "  $0 192.168.1.100 -c     # Clear buffer, then show logs"
    echo "  $0 192.168.1.100 -w     # Show only warnings and errors"
    echo ""
    echo "Use --help for more options."
    exit 1
fi

# Connect to device
echo "Connecting to ${DEVICE_IP}:${ADB_PORT}..."
"${ADB}" connect "${DEVICE_IP}:${ADB_PORT}" 2>/dev/null

# Verify connection
sleep 1
if ! "${ADB}" -s "${DEVICE_IP}:${ADB_PORT}" get-state 2>/dev/null | grep -q "device"; then
    echo "Error: Could not connect to ${DEVICE_IP}:${ADB_PORT}"
    exit 1
fi

# Use specific device for all commands
ADB="${ADB} -s ${DEVICE_IP}:${ADB_PORT}"

# Clear logcat buffer if requested
if [ "$CLEAR" = true ]; then
    echo "Clearing logcat buffer..."
    ${ADB} logcat -c
fi

echo "=== Android Media Player Logs (${DEVICE_IP}) ==="
echo "Press Ctrl+C to stop"
echo ""

# Build the logcat command
if [ -n "$LEVEL" ]; then
    # Filter by minimum level - use *:S to silence all others
    ${ADB} logcat -v time ${TAGS} *:S | grep -E "^[0-9].*[${LEVEL}WEFS]/"
else
    # Show all levels for our tags, silence others
    ${ADB} logcat -v time ${TAGS} *:S
fi
