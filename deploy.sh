#!/bin/bash
# Deploy and start Android Media Player on a device over the network
# Usage: ./deploy.sh <device-ip> [device-name] [port]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDK_DIR="${SCRIPT_DIR}/android-sdk"
ADB="${SDK_DIR}/platform-tools/adb"
APK="${SCRIPT_DIR}/app/build/outputs/apk/debug/app-debug.apk"
PACKAGE="com.example.androidmediaplayer"

# Parse arguments
if [ -z "$1" ]; then
    echo "Usage: ./deploy.sh <device-ip> [device-name] [port]"
    echo ""
    echo "Arguments:"
    echo "  device-ip     IP address of Android device (required)"
    echo "  device-name   Friendly name for the device (default: Android Media Player)"
    echo "  port          Server port on the device (default: 8765)"
    echo ""
    echo "Examples:"
    echo "  ./deploy.sh 192.168.1.100"
    echo "  ./deploy.sh 192.168.1.100 \"Living Room Tablet\""
    echo "  ./deploy.sh 192.168.1.100 \"Kitchen Display\" 8766"
    echo ""
    echo "Note: Wireless debugging must be enabled on the device."
    echo "  Android 11+: Settings → Developer options → Wireless debugging"
    echo "  Android 10:  Connect via USB first, run 'adb tcpip 5555', then disconnect"
    exit 1
fi

DEVICE_IP="$1"
DEVICE_NAME="${2:-Android Media Player}"
PORT="${3:-8765}"
ADB_PORT="${4:-5555}"

# Check prerequisites
if [ ! -x "${ADB}" ]; then
    echo "Error: Android SDK not found. Run ./build.sh first."
    exit 1
fi

if [ ! -f "${APK}" ]; then
    echo "Error: APK not found. Run ./build.sh first."
    exit 1
fi

# Connect to device
echo "Connecting to ${DEVICE_IP}:${ADB_PORT}..."
"${ADB}" connect "${DEVICE_IP}:${ADB_PORT}"

# Verify connection
sleep 1
if ! "${ADB}" -s "${DEVICE_IP}:${ADB_PORT}" get-state 2>/dev/null | grep -q "device"; then
    echo "Error: Could not connect to ${DEVICE_IP}:${ADB_PORT}"
    echo ""
    echo "Make sure:"
    echo "  1. The device is on the same network"
    echo "  2. Wireless debugging is enabled on the device"
    echo "  3. The IP address is correct"
    exit 1
fi

# Use specific device for all commands
ADB="${ADB} -s ${DEVICE_IP}:${ADB_PORT}"

echo "=== Deploying Android Media Player ==="
echo "Device name: ${DEVICE_NAME}"
echo "Port: ${PORT}"
echo ""

# Install APK
echo "Installing APK..."
"${ADB}" install -r "${APK}"

# Grant notification permission (Android 13+, fails silently on older)
echo "Granting permissions..."
"${ADB}" shell pm grant "${PACKAGE}" android.permission.POST_NOTIFICATIONS 2>/dev/null || true

# Add to battery optimization whitelist to prevent freezing
echo "Adding to battery optimization whitelist..."
"${ADB}" shell dumpsys deviceidle whitelist "+${PACKAGE}" 2>/dev/null || true

# Write config to shared preferences via adb
echo "Configuring device..."
"${ADB}" shell "run-as ${PACKAGE} sh -c 'mkdir -p shared_prefs'"
"${ADB}" shell "run-as ${PACKAGE} sh -c 'cat > shared_prefs/media_player_prefs.xml << EOF
<?xml version=\"1.0\" encoding=\"utf-8\" standalone=\"yes\" ?>
<map>
    <string name=\"device_name\">${DEVICE_NAME}</string>
    <int name=\"port\" value=\"${PORT}\" />
    <boolean name=\"service_running\" value=\"true\" />
</map>
EOF'"

# Force stop to pick up new prefs
"${ADB}" shell am force-stop "${PACKAGE}"

# Start the service via activity (which will read prefs and start service)
echo "Starting app..."
"${ADB}" shell am start -n "${PACKAGE}/.MainActivity" \
    --es "auto_start" "true"

# Wait a moment for service to start
sleep 2

echo ""
echo "=== Deployment Complete ==="
echo "Device: ${DEVICE_NAME}"
echo "API endpoint: http://${DEVICE_IP}:${PORT}/"
echo ""
echo "Test with: curl http://${DEVICE_IP}:${PORT}/state"
