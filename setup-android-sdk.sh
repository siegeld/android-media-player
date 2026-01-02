#!/bin/bash
# Setup Android SDK locally in this project directory only
# This script downloads and configures Android SDK without installing globally

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDK_DIR="${SCRIPT_DIR}/android-sdk"
CMDLINE_TOOLS_VERSION="11076708"  # Latest stable version
CMDLINE_TOOLS_URL="https://dl.google.com/android/repository/commandlinetools-linux-${CMDLINE_TOOLS_VERSION}_latest.zip"

echo "=== Android SDK Local Setup ==="
echo "SDK will be installed to: ${SDK_DIR}"

# Create SDK directory
mkdir -p "${SDK_DIR}"

# Download command line tools if not present
if [ ! -d "${SDK_DIR}/cmdline-tools/latest" ]; then
    echo "Downloading Android command line tools..."
    TEMP_ZIP="${SDK_DIR}/cmdline-tools.zip"
    curl -L -o "${TEMP_ZIP}" "${CMDLINE_TOOLS_URL}"

    echo "Extracting command line tools..."
    unzip -q "${TEMP_ZIP}" -d "${SDK_DIR}"

    # Reorganize to expected structure
    mkdir -p "${SDK_DIR}/cmdline-tools/latest"
    mv "${SDK_DIR}/cmdline-tools/"* "${SDK_DIR}/cmdline-tools/latest/" 2>/dev/null || true

    rm "${TEMP_ZIP}"
    echo "Command line tools installed."
else
    echo "Command line tools already present."
fi

# Set up environment
export ANDROID_SDK_ROOT="${SDK_DIR}"
export ANDROID_HOME="${SDK_DIR}"
export PATH="${SDK_DIR}/cmdline-tools/latest/bin:${SDK_DIR}/platform-tools:${PATH}"

# Accept licenses
echo "Accepting Android SDK licenses..."
yes | sdkmanager --licenses > /dev/null 2>&1 || true

# Install required SDK components
echo "Installing SDK components (this may take a few minutes)..."
sdkmanager --install \
    "platform-tools" \
    "platforms;android-34" \
    "build-tools;34.0.0"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "To build the app, run:"
echo "  source ${SCRIPT_DIR}/set-android-env.sh"
echo "  ./gradlew assembleDebug"
echo ""
echo "Or use the convenience script:"
echo "  ./build.sh"
