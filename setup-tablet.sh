#!/bin/bash
#
# Android Media Player - Tablet Setup Script
# Sets up a tablet for Android Media Player with silent updates
#
# Usage: ./setup-tablet.sh <device-ip:port> [device-name] [--device-owner]
# Example: ./setup-tablet.sh 10.11.5.89:41297 "Living Room Tablet"
# Example: ./setup-tablet.sh 10.11.5.89:41297 "Living Room Tablet" --device-owner
#
# For silent updates, use --device-owner flag (requires removing Google account first)
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADB="${SCRIPT_DIR}/android-sdk/platform-tools/adb"
APK_PATH="${SCRIPT_DIR}/app/build/outputs/apk/debug/app-debug.apk"
PACKAGE="com.example.androidmediaplayer"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Check arguments
if [ -z "$1" ]; then
    echo "Usage: $0 <device-ip:port> [device-name] [--device-owner]"
    echo "Example: $0 10.11.5.89:41297 \"Living Room Tablet\""
    echo "Example: $0 10.11.5.89:41297 \"Living Room Tablet\" --device-owner"
    echo ""
    echo "Options:"
    echo "  --device-owner  Set app as device owner for silent updates"
    echo "                  (Requires removing Google account first)"
    exit 1
fi

DEVICE_ADDR="$1"
DEVICE_NAME="${2:-Android Media Player}"
DEVICE_OWNER=false

# Check for --device-owner flag
for arg in "$@"; do
    if [ "$arg" == "--device-owner" ]; then
        DEVICE_OWNER=true
    fi
done

UPDATE_SERVER_IP=$(hostname -I | awk '{print $1}')

log_info "Setting up tablet at $DEVICE_ADDR"
log_info "Device name: $DEVICE_NAME"
log_info "Update server: $UPDATE_SERVER_IP:9742"

# Check if ADB exists
if [ ! -f "$ADB" ]; then
    log_warn "ADB not found at $ADB, trying system adb..."
    ADB="adb"
fi

# Check if APK exists
if [ ! -f "$APK_PATH" ]; then
    log_error "APK not found at $APK_PATH"
    log_info "Building APK..."
    cd "$SCRIPT_DIR"
    ./gradlew assembleDebug
fi

# Connect to device
log_info "Connecting to device..."
$ADB connect "$DEVICE_ADDR" || true
sleep 2

# Verify connection
if ! $ADB -s "$DEVICE_ADDR" get-state > /dev/null 2>&1; then
    log_error "Failed to connect to device at $DEVICE_ADDR"
    log_info "Make sure wireless debugging is enabled on the device"
    exit 1
fi

log_info "Connected to device"

# Disable Play Protect and package verification
log_info "Disabling Play Protect and package verification..."
$ADB -s "$DEVICE_ADDR" shell settings put global package_verifier_enable 0
$ADB -s "$DEVICE_ADDR" shell settings put global verifier_verify_adb_installs 0
$ADB -s "$DEVICE_ADDR" shell settings put global package_verifier_user_consent -1

# Disable app verification dialogs
$ADB -s "$DEVICE_ADDR" shell settings put global upload_apk_enable 0 2>/dev/null || true

# Grant install permission for unknown sources (Android 8+)
log_info "Configuring install permissions..."
$ADB -s "$DEVICE_ADDR" shell pm grant $PACKAGE android.permission.REQUEST_INSTALL_PACKAGES 2>/dev/null || true

# Install the APK
log_info "Installing APK..."
$ADB -s "$DEVICE_ADDR" install -r -g "$APK_PATH"

# Set device owner if requested
if [ "$DEVICE_OWNER" = true ]; then
    log_info "Setting app as device owner..."

    # Check for accounts
    ACCOUNTS=$($ADB -s "$DEVICE_ADDR" shell "dumpsys account | grep 'Account {' | wc -l")
    if [ "$ACCOUNTS" -gt 0 ]; then
        log_error "Cannot set device owner - $ACCOUNTS account(s) found on device"
        log_error "Please remove all accounts from Settings > Accounts first"
        log_warn "Continuing without device owner mode..."
    else
        if $ADB -s "$DEVICE_ADDR" shell dpm set-device-owner $PACKAGE/.receiver.DeviceAdminReceiver 2>/dev/null; then
            log_info "Device owner set successfully - silent updates enabled!"
        else
            log_warn "Failed to set device owner - silent updates not available"
        fi
    fi
fi

# Grant all runtime permissions
log_info "Granting permissions..."
$ADB -s "$DEVICE_ADDR" shell pm grant $PACKAGE android.permission.POST_NOTIFICATIONS 2>/dev/null || true
$ADB -s "$DEVICE_ADDR" shell pm grant $PACKAGE android.permission.FOREGROUND_SERVICE 2>/dev/null || true
$ADB -s "$DEVICE_ADDR" shell pm grant $PACKAGE android.permission.INTERNET 2>/dev/null || true
$ADB -s "$DEVICE_ADDR" shell pm grant $PACKAGE android.permission.ACCESS_NETWORK_STATE 2>/dev/null || true
$ADB -s "$DEVICE_ADDR" shell pm grant $PACKAGE android.permission.ACCESS_WIFI_STATE 2>/dev/null || true
$ADB -s "$DEVICE_ADDR" shell pm grant $PACKAGE android.permission.RECEIVE_BOOT_COMPLETED 2>/dev/null || true
$ADB -s "$DEVICE_ADDR" shell pm grant $PACKAGE android.permission.WAKE_LOCK 2>/dev/null || true

# Configure app preferences
log_info "Configuring app settings..."
PREFS_XML="<?xml version=\"1.0\" encoding=\"utf-8\" standalone=\"yes\" ?><map><boolean name=\"logging_enabled\" value=\"true\" /><string name=\"device_name\">$DEVICE_NAME</string><string name=\"update_server_host\">$UPDATE_SERVER_IP</string><int name=\"port\" value=\"8765\" /><boolean name=\"service_running\" value=\"true\" /></map>"

$ADB -s "$DEVICE_ADDR" shell "run-as $PACKAGE sh -c 'echo \"$PREFS_XML\" > /data/data/$PACKAGE/shared_prefs/media_player_prefs.xml'"

# Start the app
log_info "Starting app..."
$ADB -s "$DEVICE_ADDR" shell am start -n $PACKAGE/.MainActivity

# Wait for app to start
sleep 3

# Get device IP
DEVICE_IP=$(echo "$DEVICE_ADDR" | cut -d: -f1)

# Test API
log_info "Testing API..."
if curl -s --connect-timeout 5 "http://$DEVICE_IP:8765/" > /dev/null 2>&1; then
    log_info "API is responding!"
    curl -s "http://$DEVICE_IP:8765/" | head -5
else
    log_warn "API not responding yet, may need manual start"
fi

# Check device owner status
IS_DEVICE_OWNER=$($ADB -s "$DEVICE_ADDR" shell "dumpsys device_policy | grep -c 'Device Owner' || echo 0")
if [ "$IS_DEVICE_OWNER" -gt 0 ]; then
    SILENT_UPDATES="Enabled (Device Owner)"
else
    SILENT_UPDATES="Disabled (manual confirmation required)"
fi

echo ""
log_info "Setup complete!"
echo ""
echo "Device: $DEVICE_NAME"
echo "API URL: http://$DEVICE_IP:8765"
echo "Update Server: http://$UPDATE_SERVER_IP:9742"
echo "Silent Updates: $SILENT_UPDATES"
echo ""
echo "To test: curl http://$DEVICE_IP:8765/state"
echo "To play: curl -X POST http://$DEVICE_IP:8765/play -H 'Content-Type: application/json' -d '{\"url\":\"https://ice1.somafm.com/groovesalad-128-mp3\"}'"
echo "To check update: curl http://$DEVICE_IP:8765/update"
echo "To trigger update: curl -X POST http://$DEVICE_IP:8765/update"
