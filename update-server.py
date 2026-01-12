#!/usr/bin/env python3
"""
Simple HTTP server for Android app updates, remote logging, and device monitoring.
Features:
- Serves APK files for app updates
- Accepts and stores remote logs from devices
- Web UI for monitoring connected devices and viewing logs
"""

import http.server
import json
import os
import re
import socketserver
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from collections import deque
from urllib.parse import parse_qs, urlparse
import threading

PORT = 9742
APK_DIR = Path(__file__).parent / "app/build/outputs/apk/debug"
APK_PATH = APK_DIR / "app-debug.apk"  # Default path for backwards compatibility
DATA_DIR = Path(__file__).parent / "data"
LOG_FILE = DATA_DIR / "app_logs.jsonl"
STATE_FILE = DATA_DIR / "state.json"
ADB_PATH = Path(__file__).parent / "android-sdk/platform-tools/adb"
PACKAGE = "com.example.androidmediaplayer"

# Ensure data directory exists
DATA_DIR.mkdir(exist_ok=True)

# Thread-safe storage
data_lock = threading.Lock()
recent_logs = deque(maxlen=5000)  # Keep last 5000 logs in memory
devices = {}  # device_id -> device info
adb_devices = {}  # ip:port -> connection info


def get_apk_version_from_file(apk_path):
    """Extract version info from a single APK using aapt or aapt2."""
    if not apk_path.exists():
        return None, None

    # Try aapt2 first, then aapt
    aapt_paths = [
        Path(__file__).parent / "android-sdk/build-tools/34.0.0/aapt2",
        Path(__file__).parent / "android-sdk/build-tools/34.0.0/aapt",
    ]

    for aapt in aapt_paths:
        if aapt.exists():
            try:
                result = subprocess.run(
                    [str(aapt), "dump", "badging", str(apk_path)],
                    capture_output=True, text=True
                )
                output = result.stdout
                version_code_match = re.search(r"versionCode='(\d+)'", output)
                version_name_match = re.search(r"versionName='([^']+)'", output)
                version_code = int(version_code_match.group(1)) if version_code_match else 1
                version_name = version_name_match.group(1) if version_name_match else "1.0"
                return version_code, version_name
            except Exception as e:
                print(f"Error running {aapt}: {e}")
                continue

    # Fallback: read from build.gradle.kts
    try:
        build_gradle = Path(__file__).parent / "app/build.gradle.kts"
        content = build_gradle.read_text()
        version_code_match = re.search(r'versionCode\s*=\s*(\d+)', content)
        version_name_match = re.search(r'versionName\s*=\s*"([^"]+)"', content)
        version_code = int(version_code_match.group(1)) if version_code_match else 1
        version_name = version_name_match.group(1) if version_name_match else "1.0"
        return version_code, version_name
    except Exception as e:
        print(f"Error reading build.gradle.kts: {e}")
        return 1, "1.0"


def get_best_apk():
    """Find the APK with the highest version code. Returns (path, version_code, version_name)."""
    if not APK_DIR.exists():
        return None, None, None

    best_apk = None
    best_version_code = -1
    best_version_name = None

    # Scan all APK files in the directory
    for apk_file in APK_DIR.glob("*.apk"):
        version_code, version_name = get_apk_version_from_file(apk_file)
        if version_code is not None and version_code > best_version_code:
            best_version_code = version_code
            best_version_name = version_name
            best_apk = apk_file

    if best_apk is None:
        return None, None, None

    return best_apk, best_version_code, best_version_name


def get_apk_version():
    """Get version info from the best available APK."""
    apk_path, version_code, version_name = get_best_apk()
    if apk_path is None:
        return None, None
    return version_code, version_name


def save_log_entry(entry):
    """Save a log entry to file and memory."""
    with data_lock:
        recent_logs.append(entry)
        try:
            with open(LOG_FILE, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            print(f"Error writing log: {e}")


def update_device(device_id, info):
    """Update device information."""
    with data_lock:
        if device_id not in devices:
            devices[device_id] = {
                "first_seen": datetime.now().isoformat(),
                "log_count": 0,
                "tracks_played": []
            }
        devices[device_id].update(info)
        devices[device_id]["last_seen"] = datetime.now().isoformat()


def run_adb(*args, timeout=30):
    """Run an ADB command and return result."""
    adb = str(ADB_PATH) if ADB_PATH.exists() else "adb"
    cmd = [adb] + list(args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Command timed out"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def adb_pair(ip, port, code):
    """Pair with a device using wireless debugging."""
    result = run_adb("pair", f"{ip}:{port}", code)
    if result["success"] or "Successfully paired" in result.get("stdout", ""):
        return {"success": True, "message": "Paired successfully"}
    return {"success": False, "message": result.get("stderr", result.get("error", "Pairing failed"))}


def adb_connect(ip, port):
    """Connect to a device."""
    result = run_adb("connect", f"{ip}:{port}")
    output = result.get("stdout", "") + result.get("stderr", "")
    if "connected" in output.lower():
        with data_lock:
            adb_devices[f"{ip}:{port}"] = {
                "ip": ip,
                "port": port,
                "connected_at": datetime.now().isoformat(),
                "status": "connected"
            }
        return {"success": True, "message": f"Connected to {ip}:{port}"}
    return {"success": False, "message": output}


def adb_push_update(device):
    """Push APK update to device via ADB."""
    apk_path, version_code, version_name = get_best_apk()
    if apk_path is None:
        return {"success": False, "message": "APK not found"}

    # Install the APK
    result = run_adb("-s", device, "install", "-r", str(apk_path), timeout=120)
    if result["success"]:
        # Grant notification permission (Android 13+, fails silently on older)
        run_adb("-s", device, "shell", "pm", "grant", PACKAGE, "android.permission.POST_NOTIFICATIONS")
        # Add to battery optimization whitelist to prevent freezing
        run_adb("-s", device, "shell", "dumpsys", "deviceidle", "whitelist", f"+{PACKAGE}")
        # Start the app
        run_adb("-s", device, "shell", "am", "start", "-n", f"{PACKAGE}/.MainActivity")
        return {"success": True, "message": "Update installed successfully"}
    return {"success": False, "message": result.get("stderr", result.get("error", "Install failed"))}


def get_adb_devices():
    """Get list of connected ADB devices."""
    result = run_adb("devices", "-l")
    if not result["success"]:
        return []

    connected = []
    for line in result["stdout"].strip().split("\n")[1:]:
        if line.strip() and "device" in line:
            parts = line.split()
            addr = parts[0]
            status = parts[1] if len(parts) > 1 else "unknown"
            model = ""
            for p in parts:
                if p.startswith("model:"):
                    model = p.split(":")[1]
            connected.append({
                "address": addr,
                "status": status,
                "model": model
            })
    return connected


def adb_set_device_owner(device):
    """Set app as device owner for silent updates."""
    # Check for accounts first
    result = run_adb("-s", device, "shell", "dumpsys", "account")
    if "Account {" in result.get("stdout", ""):
        return {"success": False, "message": "Remove all accounts from device first (Settings > Accounts)"}

    # Set device owner
    result = run_adb("-s", device, "shell", "dpm", "set-device-owner",
                     f"{PACKAGE}/.receiver.DeviceAdminReceiver")
    if result["success"] or "Success" in result.get("stdout", ""):
        return {"success": True, "message": "Device owner set - silent updates enabled!"}
    return {"success": False, "message": result.get("stderr", result.get("stdout", "Failed"))}


def adb_check_device_owner(device):
    """Check if app is device owner."""
    result = run_adb("-s", device, "shell", "dumpsys", "device_policy")
    is_owner = "Device Owner" in result.get("stdout", "") and PACKAGE in result.get("stdout", "")
    return {"is_device_owner": is_owner}


def adb_disable_play_protect(device):
    """Disable Play Protect verification."""
    run_adb("-s", device, "shell", "settings", "put", "global", "package_verifier_enable", "0")
    run_adb("-s", device, "shell", "settings", "put", "global", "verifier_verify_adb_installs", "0")
    return {"success": True, "message": "Play Protect disabled"}


def get_device_app_name(device):
    """Get the device name from the app's API."""
    import urllib.request
    try:
        # Get IP from device address
        if ':' in device and not device.startswith('adb-'):
            ip = device.split(':')[0]
        else:
            # For mDNS, try to get IP via adb
            result = run_adb("-s", device, "shell", "ip", "route", "get", "1")
            match = re.search(r'src\s+(\d+\.\d+\.\d+\.\d+)', result.get("stdout", ""))
            if match:
                ip = match.group(1)
            else:
                return None

        url = f"http://{ip}:8765/"
        req = urllib.request.Request(url, headers={'User-Agent': 'UpdateServer'})
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode())
            return data.get("name")
    except Exception:
        return None


def get_device_player_state(ip):
    """Get the player state from the app's API."""
    import urllib.request
    try:
        url = f"http://{ip}:8765/state"
        req = urllib.request.Request(url, headers={'User-Agent': 'UpdateServer'})
        with urllib.request.urlopen(req, timeout=2) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def get_device_serial(device):
    """Get device serial number for deduplication."""
    result = run_adb("-s", device, "shell", "getprop", "ro.serialno")
    if result["success"]:
        return result["stdout"].strip()
    return None


WEB_UI_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Android Media Player Monitor</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #1a1a2e; color: #eee; padding: 20px;
        }
        h1 { color: #00d4ff; margin-bottom: 20px; }
        h2 { color: #00d4ff; margin: 20px 0 10px; font-size: 1.2em; }
        .container { max-width: 1400px; margin: 0 auto; }
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
        @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
        .card {
            background: #16213e; border-radius: 12px; padding: 20px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.3); margin-bottom: 15px;
        }
        .device-card { border-left: 4px solid #00d4ff; }
        .device-card.offline { border-left-color: #ff6b6b; opacity: 0.7; }
        .device-name { font-size: 1.3em; font-weight: bold; color: #00d4ff; }
        .device-info { margin-top: 10px; font-size: 0.9em; color: #aaa; }
        .device-info span { display: block; margin: 4px 0; }
        .status-badge {
            display: inline-block; padding: 3px 10px; border-radius: 12px;
            font-size: 0.8em; font-weight: bold; margin-left: 8px;
        }
        .status-online { background: #00d4ff33; color: #00d4ff; }
        .status-offline { background: #ff6b6b33; color: #ff6b6b; }
        .status-owner { background: #81c78433; color: #81c784; }
        .log-container {
            height: 400px; overflow-y: auto; background: #0f0f1a;
            border-radius: 8px; padding: 10px; font-family: monospace;
            font-size: 11px; line-height: 1.4;
        }
        .log-entry { padding: 2px 0; border-bottom: 1px solid #222; }
        .log-V { color: #888; } .log-D { color: #4fc3f7; }
        .log-I { color: #81c784; } .log-W { color: #ffb74d; } .log-E { color: #e57373; }
        .log-time { color: #666; margin-right: 8px; }
        .log-tag { color: #ce93d8; margin-right: 8px; }
        .log-device { color: #4dd0e1; margin-right: 8px; }
        .stats { display: flex; gap: 20px; flex-wrap: wrap; margin-bottom: 20px; }
        .stat-box { background: #16213e; padding: 15px 25px; border-radius: 8px; text-align: center; }
        .stat-value { font-size: 2em; font-weight: bold; color: #00d4ff; }
        .stat-label { font-size: 0.9em; color: #888; }
        .btn {
            background: #00d4ff; color: #000; border: none; padding: 8px 16px;
            border-radius: 6px; cursor: pointer; font-weight: bold; margin: 4px;
        }
        .btn:hover { background: #00a8cc; }
        .btn-secondary { background: #4a5568; color: #fff; }
        .btn-secondary:hover { background: #5a6578; }
        .btn-success { background: #48bb78; }
        .btn-success:hover { background: #38a169; }
        .btn-danger { background: #f56565; }
        .btn-danger:hover { background: #e53e3e; }
        .btn-small { padding: 4px 10px; font-size: 0.85em; }
        .filter-bar { margin-bottom: 15px; }
        .filter-bar select, .filter-bar input {
            background: #0f0f1a; color: #eee; border: 1px solid #333;
            padding: 8px 12px; border-radius: 6px; margin-right: 10px;
        }
        .track-list { max-height: 100px; overflow-y: auto; margin-top: 10px; }
        .track-item { padding: 5px; background: #0f0f1a; margin: 3px 0; border-radius: 4px; font-size: 0.85em; }
        .player-state { background: #0f0f1a; padding: 12px; border-radius: 8px; margin: 10px 0; }
        .player-state .state-label { font-size: 0.8em; color: #888; text-transform: uppercase; }
        .player-state .now-playing { color: #00d4ff; font-size: 1.1em; margin: 5px 0; }
        .player-state .artist { color: #aaa; font-size: 0.95em; }
        .player-state .meta { display: flex; gap: 15px; margin-top: 8px; font-size: 0.85em; color: #666; }
        .state-playing { border-left: 3px solid #48bb78; }
        .state-paused { border-left: 3px solid #ecc94b; }
        .state-idle { border-left: 3px solid #666; }
        .state-buffering { border-left: 3px solid #4299e1; }
        .apk-info { margin-top: 20px; padding: 15px; background: #0f3460; border-radius: 8px; }
        .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(0,0,0,0.7); z-index: 1000; justify-content: center; align-items: center; }
        .modal.active { display: flex; }
        .modal-content { background: #16213e; padding: 30px; border-radius: 12px; max-width: 500px; width: 90%; }
        .modal-title { color: #00d4ff; margin-bottom: 20px; font-size: 1.5em; }
        .form-group { margin-bottom: 15px; }
        .form-group label { display: block; margin-bottom: 5px; color: #aaa; }
        .form-group input { width: 100%; padding: 10px; background: #0f0f1a; border: 1px solid #333;
            border-radius: 6px; color: #eee; }
        .device-actions { margin-top: 15px; display: flex; flex-wrap: wrap; gap: 8px; }
        .adb-devices { margin-top: 20px; }
        .adb-device { background: #0f3460; padding: 10px 15px; border-radius: 8px; margin: 8px 0;
            display: flex; justify-content: space-between; align-items: center; }
        .toast { position: fixed; bottom: 20px; right: 20px; padding: 15px 25px; border-radius: 8px;
            background: #48bb78; color: #fff; z-index: 2000; display: none; }
        .toast.error { background: #f56565; }
        .toast.active { display: block; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Android Media Player Monitor</h1>

        <div class="stats" id="stats">
            <div class="stat-box">
                <div class="stat-value" id="device-count">0</div>
                <div class="stat-label">Devices</div>
            </div>
            <div class="stat-box">
                <div class="stat-value" id="log-count">0</div>
                <div class="stat-label">Log Entries</div>
            </div>
            <div class="stat-box">
                <div class="stat-value" id="apk-version">-</div>
                <div class="stat-label">Latest APK</div>
            </div>
        </div>

        <button class="btn" onclick="refreshAll()">Refresh All</button>
        <button class="btn btn-success" onclick="showAddDeviceModal()">+ Add Device</button>

        <div class="grid">
            <div>
                <h2>Devices</h2>
                <div id="all-devices" class="adb-devices"></div>

                <div class="apk-info">
                    <strong>APK Info:</strong><br>
                    <span id="apk-details">Loading...</span>
                </div>
            </div>

            <div>
                <h2>Live Logs</h2>
                <div class="filter-bar">
                    <select id="level-filter" onchange="filterLogs()">
                        <option value="">All Levels</option>
                        <option value="E">Errors</option>
                        <option value="W">Warnings</option>
                        <option value="I">Info</option>
                        <option value="D">Debug</option>
                    </select>
                    <select id="device-filter" onchange="filterLogs()">
                        <option value="">All Devices</option>
                    </select>
                    <input type="text" id="search-filter" placeholder="Search..." oninput="filterLogs()">
                </div>
                <div class="log-container" id="logs"></div>
            </div>
        </div>
    </div>

    <!-- Add Device Modal -->
    <div id="addDeviceModal" class="modal">
        <div class="modal-content">
            <h3 class="modal-title">Add Device via ADB</h3>
            <p style="color:#aaa;margin-bottom:20px;">Enable Wireless Debugging on the tablet, then enter the pairing info.</p>
            <div class="form-group">
                <label>Device IP Address</label>
                <input type="text" id="device-ip" placeholder="192.168.1.100">
            </div>
            <div class="form-group">
                <label>Pairing Port (from device)</label>
                <input type="text" id="pair-port" placeholder="37123">
            </div>
            <div class="form-group">
                <label>Pairing Code (from device)</label>
                <input type="text" id="pair-code" placeholder="123456">
            </div>
            <div class="form-group">
                <label>Connection Port (usually 5555 or shown on device)</label>
                <input type="text" id="connect-port" placeholder="41297">
            </div>
            <div style="margin-top:20px;">
                <button class="btn btn-success" onclick="pairAndConnectDevice()">Pair & Connect</button>
                <button class="btn btn-secondary" onclick="closeModal()">Cancel</button>
            </div>
        </div>
    </div>

    <!-- Toast notification -->
    <div id="toast" class="toast"></div>

    <script>
        let allLogs = [];
        let allDevices = [];

        function showToast(msg, isError = false) {
            const toast = document.getElementById('toast');
            toast.textContent = msg;
            toast.className = 'toast active' + (isError ? ' error' : '');
            setTimeout(() => toast.className = 'toast', 3000);
        }

        function showAddDeviceModal() {
            document.getElementById('addDeviceModal').classList.add('active');
        }

        function closeModal() {
            document.getElementById('addDeviceModal').classList.remove('active');
        }

        async function pairAndConnectDevice() {
            const ip = document.getElementById('device-ip').value.trim();
            const pairPort = document.getElementById('pair-port').value.trim();
            const pairCode = document.getElementById('pair-code').value.trim();
            const connectPort = document.getElementById('connect-port').value.trim();

            if (!ip || !connectPort) {
                showToast('IP and connection port are required', true);
                return;
            }

            try {
                if (pairPort && pairCode) {
                    showToast('Pairing...');
                    const pairResp = await fetch('/api/adb/pair', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ip, port: pairPort, code: pairCode})
                    });
                    const pairResult = await pairResp.json();
                    if (!pairResult.success) {
                        showToast('Pairing failed: ' + pairResult.message, true);
                        return;
                    }
                }
                showToast('Connecting...');
                const connResp = await fetch('/api/adb/connect', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ip, port: connectPort})
                });
                const connResult = await connResp.json();
                if (connResult.success) {
                    showToast('Connected successfully!');
                    closeModal();
                    fetchAllDevices();
                } else {
                    showToast('Connection failed: ' + connResult.message, true);
                }
            } catch (e) {
                showToast('Error: ' + e.message, true);
            }
        }

        async function pushUpdate(address) {
            if (!confirm('Push update via ADB to this device?')) return;
            showToast('Installing update...');
            try {
                const resp = await fetch('/api/adb/push', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({device: address})
                });
                const result = await resp.json();
                showToast(result.message, !result.success);
            } catch (e) {
                showToast('Error: ' + e.message, true);
            }
        }

        async function triggerOtaUpdate(ip) {
            showToast('Triggering OTA update...');
            try {
                const resp = await fetch(`http://${ip}:8765/update`, { method: 'POST' });
                const result = await resp.json();
                showToast(result.message, !result.success);
            } catch (e) {
                showToast('Error: ' + e.message, true);
            }
        }

        async function setDeviceOwner(address) {
            if (!confirm('Set device owner?\\n\\nNote: All Google accounts must be removed first!')) return;
            showToast('Setting device owner...');
            try {
                const resp = await fetch('/api/adb/device-owner', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({device: address})
                });
                const result = await resp.json();
                showToast(result.message, !result.success);
                fetchAllDevices();
            } catch (e) {
                showToast('Error: ' + e.message, true);
            }
        }

        async function disablePlayProtect(address) {
            showToast('Disabling Play Protect...');
            try {
                const resp = await fetch('/api/adb/disable-protect', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({device: address})
                });
                const result = await resp.json();
                showToast(result.message, !result.success);
            } catch (e) {
                showToast('Error: ' + e.message, true);
            }
        }

        async function playTestStream(ip) {
            showToast('Starting test stream...');
            try {
                const resp = await fetch(`http://${ip}:8765/play`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        url: 'http://stream.radioparadise.com/aac-320',
                        title: 'Radio Paradise',
                        artist: 'Test Stream (320k AAC)'
                    })
                });
                const result = await resp.json();
                showToast(result.success ? 'Playing test stream' : result.message, !result.success);
                fetchAllDevices();
            } catch (e) {
                showToast('Error: ' + e.message, true);
            }
        }

        async function stopPlayback(ip) {
            try {
                const resp = await fetch(`http://${ip}:8765/stop`, { method: 'POST' });
                const result = await resp.json();
                showToast(result.success ? 'Stopped' : result.message, !result.success);
                fetchAllDevices();
            } catch (e) {
                showToast('Error: ' + e.message, true);
            }
        }

        async function pausePlayback(ip) {
            try {
                const resp = await fetch(`http://${ip}:8765/pause`, { method: 'POST' });
                const result = await resp.json();
                showToast(result.success ? 'Paused' : result.message, !result.success);
                fetchAllDevices();
            } catch (e) {
                showToast('Error: ' + e.message, true);
            }
        }

        async function resumePlayback(ip) {
            try {
                const resp = await fetch(`http://${ip}:8765/play`, { method: 'POST' });
                const result = await resp.json();
                showToast(result.success ? 'Playing' : result.message, !result.success);
                fetchAllDevices();
            } catch (e) {
                showToast('Error: ' + e.message, true);
            }
        }

        async function fetchPlayerState(ip) {
            try {
                const resp = await fetch(`/api/player-state/${ip}`);
                return await resp.json();
            } catch (e) {
                return null;
            }
        }

        async function fetchAllDevices() {
            try {
                // Fetch both ADB and app devices
                const [adbResp, appResp] = await Promise.all([
                    fetch('/api/adb/devices'),
                    fetch('/api/devices')
                ]);
                const adbDevices = await adbResp.json();
                const appDevices = await appResp.json();

                // Build lookup of existing player states to preserve them
                const existingStates = {};
                for (const d of allDevices) {
                    if (d.ip && d.player_state) existingStates[d.ip] = d.player_state;
                }

                // Merge by IP address
                const merged = {};
                for (const d of adbDevices) {
                    const ip = d.address.includes(':') && !d.address.startsWith('adb-')
                        ? d.address.split(':')[0] : null;
                    const key = ip || d.serial || d.address;
                    merged[key] = {
                        name: d.name || d.model || d.address,
                        ip: ip,
                        adb_address: d.address,
                        is_device_owner: d.is_device_owner,
                        adb_connected: true,
                        app_connected: false,
                        version: null,
                        last_seen: null,
                        player_state: ip ? existingStates[ip] || null : null
                    };
                }
                for (const [id, d] of Object.entries(appDevices)) {
                    const ip = d.ip_address;
                    if (ip && merged[ip]) {
                        merged[ip].name = d.device_name || merged[ip].name;
                        merged[ip].version = d.app_version;
                        merged[ip].last_seen = d.last_seen;
                        merged[ip].app_connected = true;
                        if (!merged[ip].player_state) merged[ip].player_state = existingStates[ip] || null;
                    } else {
                        const key = ip || id;
                        merged[key] = {
                            name: d.device_name || id,
                            ip: ip,
                            adb_address: null,
                            is_device_owner: false,
                            adb_connected: false,
                            app_connected: true,
                            version: d.app_version,
                            last_seen: d.last_seen,
                            player_state: ip ? existingStates[ip] || null : null
                        };
                    }
                }
                allDevices = Object.values(merged);
                document.getElementById('device-count').textContent = allDevices.length;
                renderDevices();
                updateDeviceFilter();

                // Fetch player state for each device with an IP (update in-place, don't re-render)
                for (const d of allDevices) {
                    if (d.ip) {
                        const deviceId = 'device-' + (d.ip || d.name).replace(/[^a-zA-Z0-9]/g, '-');
                        fetchPlayerState(d.ip).then(state => {
                            if (state && !state.error) {
                                d.player_state = state;
                                const card = document.getElementById(deviceId);
                                if (card) {
                                    updatePlayerStateInPlace(card, state);
                                }
                            }
                        });
                    }
                }
            } catch (e) { console.error('Failed to fetch devices:', e); }
        }

        function formatTime(sec) {
            if (sec === null || sec === undefined || sec < 0) return '--:--';
            const s = Math.floor(sec);
            const m = Math.floor(s / 60);
            const secs = s % 60;
            return `${m}:${secs.toString().padStart(2, '0')}`;
        }

        function getPlayerStateData(ps) {
            if (!ps) return { state: 'idle', stateLabel: 'Idle', title: '', artist: '', time: '--:--', volume: '--' };
            const state = ps.state || 'idle';
            const stateLabel = state.charAt(0).toUpperCase() + state.slice(1);
            let title = ps.mediaTitle || ps.title || '';
            let artist = ps.mediaArtist || ps.artist || '';
            if (artist.startsWith('media_player.')) artist = '';
            const positionMs = ps.mediaPosition || ps.position || 0;
            const durationMs = ps.mediaDuration || ps.duration || null;
            const position = positionMs / 1000;
            const duration = durationMs ? durationMs / 1000 : null;
            const volume = ps.volume !== undefined ? Math.round(ps.volume * 100) + '%' : '--';
            const muted = ps.muted ? ' (Muted)' : '';
            const url = ps.mediaUrl || '';
            if (!title && url) {
                const parts = url.split('/');
                title = decodeURIComponent(parts[parts.length - 1]).replace(/\.[^.]+$/, '');
            }
            const time = duration ? `${formatTime(position)} / ${formatTime(duration)}` : formatTime(position);
            return { state, stateLabel, title, artist, time, volume: volume + muted };
        }

        function renderPlayerState(ps) {
            const d = getPlayerStateData(ps);
            // Always render all data-field elements for in-place updates
            const showMeta = d.state !== 'idle' || d.title;
            return `<div class="player-state state-${d.state}">
                <span class="state-label" data-field="state">${d.stateLabel}</span>
                <div class="now-playing" data-field="title" ${d.title ? '' : 'style="display:none"'}>${d.title || ''}</div>
                <div class="artist" data-field="artist" ${d.artist ? '' : 'style="display:none"'}>${d.artist || ''}</div>
                <div class="meta" data-field="meta" ${showMeta ? '' : 'style="display:none"'}>
                    <span data-field="time">Time: ${d.time}</span>
                    <span data-field="volume">Vol: ${d.volume}</span>
                </div>
                <div data-field="no-media" style="color:#666;margin-top:5px;${showMeta ? 'display:none' : ''}">No media loaded</div>
            </div>`;
        }

        function updatePlayerStateInPlace(container, ps) {
            const d = getPlayerStateData(ps);
            const playerDiv = container.querySelector('.player-state');
            if (!playerDiv) return false;

            // Update state class
            playerDiv.className = 'player-state state-' + d.state;

            // Update individual fields
            const stateEl = playerDiv.querySelector('[data-field="state"]');
            const titleEl = playerDiv.querySelector('[data-field="title"]');
            const artistEl = playerDiv.querySelector('[data-field="artist"]');
            const timeEl = playerDiv.querySelector('[data-field="time"]');
            const volumeEl = playerDiv.querySelector('[data-field="volume"]');
            const metaEl = playerDiv.querySelector('[data-field="meta"]');
            const noMediaEl = playerDiv.querySelector('[data-field="no-media"]');

            if (stateEl && stateEl.textContent !== d.stateLabel) stateEl.textContent = d.stateLabel;
            if (titleEl) {
                if (d.title) {
                    if (titleEl.textContent !== d.title) titleEl.textContent = d.title;
                    titleEl.style.display = '';
                } else {
                    titleEl.style.display = 'none';
                }
            }
            if (artistEl) {
                if (d.artist) {
                    if (artistEl.textContent !== d.artist) artistEl.textContent = d.artist;
                    artistEl.style.display = '';
                } else {
                    artistEl.style.display = 'none';
                }
            }
            if (timeEl) timeEl.textContent = 'Time: ' + d.time;
            if (volumeEl) volumeEl.textContent = 'Vol: ' + d.volume;

            // Show/hide meta vs no-media based on state
            const showMeta = d.state !== 'idle' || d.title;
            if (metaEl) metaEl.style.display = showMeta ? '' : 'none';
            if (noMediaEl) noMediaEl.style.display = showMeta ? 'none' : '';

            return true;
        }

        function renderDevices() {
            const container = document.getElementById('all-devices');
            if (allDevices.length === 0) {
                container.innerHTML = '<div class="card"><p>No devices found. Click "Add Device" to pair a tablet via ADB.</p></div>';
                return;
            }

            // Track which device IDs we've seen
            const seenIds = new Set();

            allDevices.forEach(d => {
                const deviceId = 'device-' + (d.ip || d.name).replace(/[^a-zA-Z0-9]/g, '-');
                seenIds.add(deviceId);

                let card = document.getElementById(deviceId);
                const isOnline = d.player_state || (d.app_connected && d.last_seen && (Date.now() - new Date(d.last_seen).getTime()) < 300000);

                // Build static content hash (excludes player state time/position)
                const badges =
                    (d.is_device_owner ? '<span class="status-badge status-owner">Silent Updates</span>' : '') +
                    (isOnline ? '<span class="status-badge status-online">Online</span>' : '') +
                    (d.adb_connected ? '<span class="status-badge" style="background:#4fc3f733;color:#4fc3f7;">ADB</span>' : '');
                const info =
                    (d.ip ? 'IP: ' + d.ip + ':8765' : '') +
                    (d.version ? ' | Version: ' + d.version : '') +
                    (d.adb_address && d.adb_address !== d.ip + ':41297' ? ' | ADB: ' + d.adb_address : '');
                const isPlaying = d.player_state && d.player_state.state === 'playing';
                const isPaused = d.player_state && d.player_state.state === 'paused';
                const buttons =
                    (d.ip ? `<button class="btn btn-small" onclick="playTestStream('${d.ip}')">Test Stream</button>` : '') +
                    (d.ip && isPlaying ? `<button class="btn btn-small btn-secondary" onclick="pausePlayback('${d.ip}')">Pause</button>` : '') +
                    (d.ip && isPaused ? `<button class="btn btn-small btn-success" onclick="resumePlayback('${d.ip}')">Play</button>` : '') +
                    (d.ip ? `<button class="btn btn-small btn-danger" onclick="stopPlayback('${d.ip}')">Stop</button>` : '') +
                    (d.adb_connected ? `<button class="btn btn-small" onclick="pushUpdate('${d.adb_address}')">Push Update (ADB)</button>` : '') +
                    (d.is_device_owner && d.ip ? `<button class="btn btn-small btn-success" onclick="triggerOtaUpdate('${d.ip}')">OTA Update</button>` : '') +
                    (d.adb_connected && !d.is_device_owner ? `<button class="btn btn-small btn-secondary" onclick="setDeviceOwner('${d.adb_address}')">Enable Silent Updates</button>` : '') +
                    (d.adb_connected ? `<button class="btn btn-small btn-secondary" onclick="disablePlayProtect('${d.adb_address}')">Disable Protect</button>` : '');

                // Static hash includes play state for button updates
                const psData = getPlayerStateData(d.player_state);
                const staticHash = d.name + badges + info + isPlaying + isPaused + psData.state + psData.title + psData.artist;

                if (!card) {
                    // Create new card
                    card = document.createElement('div');
                    card.id = deviceId;
                    card.className = 'adb-device';
                    card.style.cssText = 'flex-direction:column;align-items:stretch;';
                    card.dataset.staticHash = staticHash;
                    card.innerHTML = `
                        <div style="display:flex;justify-content:space-between;align-items:center;">
                            <div>
                                <strong style="font-size:1.1em;">${d.name}</strong>
                                ${badges}
                            </div>
                        </div>
                        <div style="font-size:0.85em;color:#888;margin:8px 0;">${info}</div>
                        ${renderPlayerState(d.player_state)}
                        <div style="display:flex;flex-wrap:wrap;gap:6px;">${buttons}</div>`;
                    container.appendChild(card);
                } else if (card.dataset.staticHash !== staticHash) {
                    // Static content changed - rebuild card
                    card.dataset.staticHash = staticHash;
                    card.innerHTML = `
                        <div style="display:flex;justify-content:space-between;align-items:center;">
                            <div>
                                <strong style="font-size:1.1em;">${d.name}</strong>
                                ${badges}
                            </div>
                        </div>
                        <div style="font-size:0.85em;color:#888;margin:8px 0;">${info}</div>
                        ${renderPlayerState(d.player_state)}
                        <div style="display:flex;flex-wrap:wrap;gap:6px;">${buttons}</div>`;
                } else {
                    // Only player state time/volume changed - update in place
                    updatePlayerStateInPlace(card, d.player_state);
                }
            });

            // Remove cards for devices that no longer exist
            Array.from(container.children).forEach(child => {
                if (child.id && child.id.startsWith('device-') && !seenIds.has(child.id)) {
                    child.remove();
                }
            });
        }

        async function fetchLogs() {
            try {
                const resp = await fetch('/api/logs?limit=500');
                allLogs = await resp.json();
                filterLogs();
                document.getElementById('log-count').textContent = allLogs.length;
            } catch (e) { console.error('Failed to fetch logs:', e); }
        }

        async function fetchVersion() {
            try {
                const resp = await fetch('/version');
                const data = await resp.json();
                if (data.available) {
                    document.getElementById('apk-version').textContent = data.versionName;
                    document.getElementById('apk-details').innerHTML =
                        `Version: ${data.versionName} (code ${data.versionCode})<br>Size: ${(data.size / 1024 / 1024).toFixed(2)} MB`;
                } else {
                    document.getElementById('apk-version').textContent = 'N/A';
                    document.getElementById('apk-details').textContent = 'APK not found';
                }
            } catch (e) { console.error('Failed to fetch version:', e); }
        }

        function updateDeviceFilter() {
            const select = document.getElementById('device-filter');
            const current = select.value;
            select.innerHTML = '<option value="">All Devices</option>' +
                allDevices.map(d => `<option value="${d.ip || d.name}">${d.name}</option>`).join('');
            select.value = current;
        }

        function filterLogs() {
            const level = document.getElementById('level-filter').value;
            const device = document.getElementById('device-filter').value;
            const search = document.getElementById('search-filter').value.toLowerCase();
            const filtered = allLogs.filter(log => {
                if (level && log.level !== level) return false;
                if (device) {
                    // Match by IP, device_id, or device_name
                    const matches = log.device_id === device ||
                                    log.device_name === device ||
                                    (log.client_ip && log.client_ip === device);
                    if (!matches) return false;
                }
                if (search && !JSON.stringify(log).toLowerCase().includes(search)) return false;
                return true;
            });
            renderLogs(filtered);
        }

        function renderLogs(logs) {
            const container = document.getElementById('logs');
            // Check if user is at bottom (within 50px threshold)
            const wasAtBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 50;
            const oldScrollTop = container.scrollTop;
            container.innerHTML = logs.map(log => `
                <div class="log-entry log-${log.level}">
                    <span class="log-time">${log.timestamp || ''}</span>
                    <span class="log-device">[${log.device_name || log.device_id || '?'}]</span>
                    <span class="log-tag">${log.tag || ''}</span>
                    ${log.message || ''}
                </div>
            `).join('');
            if (wasAtBottom) {
                // Auto-scroll to bottom
                container.scrollTop = container.scrollHeight;
            } else {
                // Preserve scroll position
                container.scrollTop = oldScrollTop;
            }
        }

        function refreshAll() {
            fetchAllDevices();
            fetchLogs();
            fetchVersion();
        }

        refreshAll();
        setInterval(refreshAll, 5000);
    </script>
</body>
</html>
"""


class UpdateHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[{self.address_string()}] {format % args}")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/version":
            self.handle_version()
        elif path == "/apk" or path == "/update.apk":
            self.handle_apk()
        elif path == "/api/logs":
            self.handle_get_logs(parsed.query)
        elif path == "/api/devices":
            self.handle_get_devices()
        elif path == "/api/adb/devices":
            self.handle_get_adb_devices()
        elif path.startswith("/api/player-state/"):
            ip = path.split("/")[-1]
            self.handle_get_player_state(ip)
        elif path == "/" or path == "/ui":
            self.handle_web_ui()
        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        if self.path == "/log":
            self.handle_post_log()
        elif self.path == "/logs":
            self.handle_post_logs()
        elif self.path == "/checkin":
            self.handle_checkin()
        elif self.path == "/track":
            self.handle_track_played()
        elif self.path == "/api/adb/pair":
            self.handle_adb_pair()
        elif self.path == "/api/adb/connect":
            self.handle_adb_connect()
        elif self.path == "/api/adb/push":
            self.handle_adb_push()
        elif self.path == "/api/adb/device-owner":
            self.handle_adb_device_owner()
        elif self.path == "/api/adb/disable-protect":
            self.handle_adb_disable_protect()
        else:
            self.send_error(404, "Not Found")

    def handle_web_ui(self):
        """Serve the web UI."""
        body = WEB_UI_HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def handle_version(self):
        """Return version info as JSON."""
        apk_path, version_code, version_name = get_best_apk()

        if apk_path is None:
            response = {"error": "APK not found", "available": False}
        else:
            response = {
                "available": True,
                "versionCode": version_code,
                "versionName": version_name,
                "size": apk_path.stat().st_size,
                "filename": apk_path.name
            }

        body = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def handle_apk(self):
        """Serve the APK file with the highest version."""
        apk_path, version_code, version_name = get_best_apk()

        if apk_path is None:
            self.send_error(404, "APK not found")
            return

        file_size = apk_path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.android.package-archive")
        self.send_header("Content-Length", file_size)
        self.send_header("Content-Disposition", f"attachment; filename={apk_path.name}")
        self.end_headers()

        with open(apk_path, "rb") as f:
            chunk_size = 1024 * 1024
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def handle_get_logs(self, query_string):
        """Return recent logs as JSON."""
        params = parse_qs(query_string)
        limit = int(params.get('limit', [500])[0])
        device_id = params.get('device', [None])[0]
        level = params.get('level', [None])[0]

        with data_lock:
            logs = list(recent_logs)

        # Filter
        if device_id:
            logs = [l for l in logs if l.get('device_id') == device_id]
        if level:
            logs = [l for l in logs if l.get('level') == level]

        # Return most recent
        logs = logs[-limit:]

        body = json.dumps(logs).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def handle_get_devices(self):
        """Return device list as JSON."""
        with data_lock:
            device_list = dict(devices)

        body = json.dumps(device_list).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def handle_get_player_state(self, ip):
        """Get player state from device."""
        state = get_device_player_state(ip)
        if state is None:
            state = {"error": "Could not connect to device"}
        body = json.dumps(state).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def handle_post_log(self):
        """Accept a single log entry."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode()
            entry = json.loads(body)

            # Add server timestamp and client IP
            entry['server_time'] = datetime.now().isoformat()
            entry['client_ip'] = self.client_address[0]

            # Update device info
            device_id = entry.get('device_id', 'unknown')
            update_device(device_id, {
                'device_name': entry.get('device_name'),
                'app_version': entry.get('app_version'),
                'ip_address': self.client_address[0]
            })

            # Increment log count
            with data_lock:
                if device_id in devices:
                    devices[device_id]['log_count'] = devices[device_id].get('log_count', 0) + 1

            save_log_entry(entry)

            # Print important logs to console
            level = entry.get('level', 'I')
            if level in ('E', 'W', 'I'):
                print(f"[{entry.get('device_name', device_id)}] {level}/{entry.get('tag', '?')}: {entry.get('message', '')}")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"success": true}')
        except Exception as e:
            print(f"Error handling log: {e}")
            self.send_error(400, str(e))

    def handle_post_logs(self):
        """Accept multiple log entries."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode()
            entries = json.loads(body)

            for entry in entries:
                entry['server_time'] = datetime.now().isoformat()
                entry['client_ip'] = self.client_address[0]

                device_id = entry.get('device_id', 'unknown')
                update_device(device_id, {
                    'device_name': entry.get('device_name'),
                    'app_version': entry.get('app_version'),
                    'ip_address': self.client_address[0]
                })

                with data_lock:
                    if device_id in devices:
                        devices[device_id]['log_count'] = devices[device_id].get('log_count', 0) + 1

                save_log_entry(entry)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "count": len(entries)}).encode())
        except Exception as e:
            print(f"Error handling logs: {e}")
            self.send_error(400, str(e))

    def handle_checkin(self):
        """Handle device check-in."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode()
            info = json.loads(body)

            device_id = info.get('device_id', f"unknown_{self.client_address[0]}")
            info['ip_address'] = self.client_address[0]

            update_device(device_id, info)
            print(f"Device check-in: {info.get('device_name', device_id)} @ {self.client_address[0]}")

            # Return current APK version
            version_code, version_name = get_apk_version()
            response = {
                "success": True,
                "server_version_code": version_code,
                "server_version_name": version_name
            }

            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            print(f"Error handling check-in: {e}")
            self.send_error(400, str(e))

    def handle_track_played(self):
        """Handle track played notification."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode()
            track_info = json.loads(body)

            device_id = track_info.get('device_id', 'unknown')
            track_info['played_at'] = datetime.now().isoformat()

            with data_lock:
                if device_id in devices:
                    if 'tracks_played' not in devices[device_id]:
                        devices[device_id]['tracks_played'] = []
                    devices[device_id]['tracks_played'].append(track_info)
                    # Keep only last 50 tracks
                    devices[device_id]['tracks_played'] = devices[device_id]['tracks_played'][-50:]

            print(f"Track played on {track_info.get('device_name', device_id)}: {track_info.get('title', track_info.get('url', '?'))}")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"success": true}')
        except Exception as e:
            print(f"Error handling track: {e}")
            self.send_error(400, str(e))

    def handle_get_adb_devices(self):
        """Return list of ADB-connected devices, deduplicated by serial."""
        raw_devices = get_adb_devices()

        # Deduplicate by serial number
        seen_serials = {}
        for d in raw_devices:
            addr = d['address']
            serial = get_device_serial(addr)

            if serial and serial in seen_serials:
                # Prefer IP:port over mDNS
                if ':' in addr and not addr.startswith('adb-'):
                    seen_serials[serial]['address'] = addr
                continue

            # Get device owner status
            owner_info = adb_check_device_owner(addr)
            d['is_device_owner'] = owner_info.get('is_device_owner', False)

            # Get app name
            app_name = get_device_app_name(addr)
            if app_name:
                d['name'] = app_name
            else:
                d['name'] = d.get('model', addr)

            d['serial'] = serial
            if serial:
                seen_serials[serial] = d
            else:
                seen_serials[addr] = d

        devices_list = list(seen_serials.values())
        body = json.dumps(devices_list).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def handle_adb_pair(self):
        """Pair with a device."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode()
            data = json.loads(body)
            result = adb_pair(data['ip'], data['port'], data['code'])
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_error(400, str(e))

    def handle_adb_connect(self):
        """Connect to a device."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode()
            data = json.loads(body)
            result = adb_connect(data['ip'], data['port'])
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_error(400, str(e))

    def handle_adb_push(self):
        """Push APK update to device."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode()
            data = json.loads(body)
            device = data.get('device') or f"{data['ip']}:{data['port']}"
            result = adb_push_update(device)
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_error(400, str(e))

    def handle_adb_device_owner(self):
        """Set device owner."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode()
            data = json.loads(body)
            device = data.get('device') or f"{data['ip']}:{data['port']}"
            result = adb_set_device_owner(device)
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_error(400, str(e))

    def handle_adb_disable_protect(self):
        """Disable Play Protect."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode()
            data = json.loads(body)
            device = data.get('device') or f"{data['ip']}:{data['port']}"
            result = adb_disable_play_protect(device)
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_error(400, str(e))


class ThreadedHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True


def main():
    print(f"Starting update/monitoring server on port {PORT}")
    print(f"APK directory: {APK_DIR}")

    apk_path, version_code, version_name = get_best_apk()
    if apk_path:
        print(f"Best APK: {apk_path.name} - version {version_name} (code: {version_code})")
        print(f"APK size: {apk_path.stat().st_size:,} bytes")
    else:
        print("WARNING: No APK found! Build the app first.")

    print(f"\nEndpoints:")
    print(f"  http://0.0.0.0:{PORT}/         - Web UI (monitoring dashboard)")
    print(f"  http://0.0.0.0:{PORT}/version  - Version JSON")
    print(f"  http://0.0.0.0:{PORT}/apk      - Download APK")
    print(f"  POST /log                      - Submit single log")
    print(f"  POST /logs                     - Submit multiple logs")
    print(f"  POST /checkin                  - Device check-in")
    print(f"  POST /track                    - Track played notification")
    print(f"  GET /api/logs                  - Get logs (JSON)")
    print(f"  GET /api/devices               - Get devices (JSON)")
    print()

    with ThreadedHTTPServer(("0.0.0.0", PORT), UpdateHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down...")


if __name__ == "__main__":
    main()
