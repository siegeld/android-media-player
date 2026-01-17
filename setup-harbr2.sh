#!/bin/bash
# Setup script for android-media-player on harbr2
# Run this script on harbr2 after cloning the repo

set -e

TARGET_DIR="/srv/docker/android-media-player"
SOURCE_HOST="fedrbr1"
SOURCE_PATH="/u/siegeld/android-media-player/migration-bundle.tar.gz"

echo "=== Android Media Player Setup for harbr2 ==="
echo ""

# Check if we're in the right directory
if [ "$(pwd)" != "$TARGET_DIR" ]; then
    echo "This script should be run from $TARGET_DIR"
    echo ""
    echo "First, clone the repo:"
    echo "  git clone nfsrbr1:/home/git/android-media-player.git $TARGET_DIR"
    echo "  cd $TARGET_DIR"
    echo "  ./setup-harbr2.sh"
    exit 1
fi

# Step 1: Copy migration bundle from fedrbr1
echo "[1/6] Copying migration bundle from $SOURCE_HOST..."
scp "${SOURCE_HOST}:${SOURCE_PATH}" .

# Step 2: Extract keys and backup
echo "[2/6] Extracting migration bundle..."
tar -xzf migration-bundle.tar.gz
echo "  - ADB keys: $(ls android-sdk/keys/ | wc -l) files"
echo "  - Server data backup: $(ls -lh server-data-backup.tar.gz | awk '{print $5}')"

# Step 3: Build the APK
echo "[3/6] Building APK (this may take a while on first run)..."
./build.sh

# Step 4: Start docker to create the volume
echo "[4/6] Creating docker volume..."
docker compose up -d
sleep 2
docker compose stop

# Step 5: Restore server-data volume
echo "[5/6] Restoring server data volume..."
docker run --rm -v android-media-player_server-data:/data -v "$(pwd)":/backup alpine \
  sh -c "rm -rf /data/* && tar -xzf /backup/server-data-backup.tar.gz -C /data"

# Step 6: Start everything
echo "[6/6] Starting services..."
docker compose up -d

# Verify
echo ""
echo "=== Setup Complete ==="
echo ""
sleep 2

echo "Verifying..."
VERSION=$(curl -s http://localhost:9742/version 2>/dev/null || echo "FAILED")
echo "  Server version: $VERSION"

DEVICES=$(curl -s http://localhost:9742/api/devices 2>/dev/null | grep -o '"name"' | wc -l || echo "0")
echo "  Devices found: $DEVICES"

echo ""
echo "Web UI: http://harbr2:9742"
echo ""
echo "Next steps:"
echo "  1. Open http://harbr2:9742 in browser"
echo "  2. Click 'Push Update (ADB)' on each device to update server IP"
echo "  3. Verify devices reconnect and logs appear"
echo ""
echo "After verification, cleanup fedrbr1:"
echo "  ssh fedrbr1 'cd /u/siegeld/android-media-player && docker compose down -v'"
