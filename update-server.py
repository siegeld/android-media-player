#!/usr/bin/env python3
"""
Simple HTTP server for Android app updates, remote logging, and device monitoring.
Features:
- Serves APK files for app updates
- Accepts and stores remote logs from devices
- Web UI for monitoring connected devices and viewing logs
"""

import concurrent.futures
import http.server
import json
import os
import queue
import re
import signal
import socket
import socketserver
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from collections import deque
from urllib.parse import parse_qs, urlparse
import threading
import urllib.request

PORT = 9742


def _read_version() -> str:
    """Monitor/server version — from the VERSION file (single source of truth;
    homelab-app-standard §14). Mounted at /app/VERSION so a bump needs no rebuild."""
    try:
        return (Path(__file__).resolve().parent / "VERSION").read_text().strip()
    except OSError:
        return "0.0.0-dev"


SERVER_VERSION = _read_version()  # Monitor/server version (separate from APK version)
APK_DIR = Path(__file__).parent / "app/build/outputs/apk/debug"
APK_PATH = APK_DIR / "app-debug.apk"  # Default path for backwards compatibility
DATA_DIR = Path(__file__).parent / "data"
LOG_FILE = DATA_DIR / "app_logs.jsonl"
STATE_FILE = DATA_DIR / "state.json"
ADB_PATH = Path(__file__).parent / "android-sdk/platform-tools/adb"
PACKAGE = "com.example.androidmediaplayer"

ADB_HOST = "127.0.0.1"
ADB_PORT = 5037
ENRICH_REFRESH_SECONDS = 90        # re-check device-owner/app-info on this cadence
PLAYER_STATE_POLL_SECONDS = 2.5    # poll device :8765/state for online devices
SSE_QUEUE_MAX = 32                 # bounded per-subscriber queue; slow consumers get dropped

# Ensure data directory exists
DATA_DIR.mkdir(exist_ok=True)

# Thread-safe storage
data_lock = threading.Lock()
recent_logs = deque(maxlen=5000)  # Keep last 5000 logs in memory
devices = {}  # device_id -> device info (legacy app-side check-ins)
adb_devices = {}  # ip:port -> connection info (legacy; populated by manual connect)

# Single-worker queue for ad-hoc adb commands. Serializes pair/connect/install/etc.
# so concurrent dashboard actions never trample each other through fork-server.
adb_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="adb-cmd")
# Pool for parallel HTTP probes against device :8765 endpoints.
http_executor = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="http-poll")

shutdown_event = threading.Event()


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
    """Save a log entry to file and memory, and push to SSE subscribers."""
    with data_lock:
        recent_logs.append(entry)
        try:
            with open(LOG_FILE, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            print(f"Error writing log: {e}")
    publish_logs_event([entry])


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


def delete_device(ip=None, adb_address=None):
    """Delete a device from tracking. Removes from both devices and adb_devices dicts."""
    removed = {"app_devices": [], "adb_devices": []}

    with data_lock:
        # Remove from app devices by IP
        if ip:
            to_remove = [did for did, d in devices.items() if d.get('ip_address') == ip]
            for did in to_remove:
                del devices[did]
                removed["app_devices"].append(did)

        # Remove from adb_devices
        if adb_address and adb_address in adb_devices:
            del adb_devices[adb_address]
            removed["adb_devices"].append(adb_address)

        # Also try to find adb_device by IP
        if ip:
            to_remove_adb = [addr for addr in adb_devices if addr.startswith(f"{ip}:")]
            for addr in to_remove_adb:
                del adb_devices[addr]
                removed["adb_devices"].append(addr)

    # Disconnect from ADB if connected
    if adb_address:
        run_adb("disconnect", adb_address, timeout=5)
    elif ip:
        # Try common ADB ports
        run_adb("disconnect", f"{ip}:5555", timeout=5)
        run_adb("disconnect", f"{ip}:41297", timeout=5)

    return removed


def _adb_binary():
    return str(ADB_PATH) if ADB_PATH.exists() else "adb"


def run_adb(*args, timeout=30):
    """Run an adb command, killing the entire process group on timeout.

    Uses Popen with start_new_session=True so a stuck adb invocation cannot
    leave grandchildren (server connections, shell helpers) behind: on
    timeout we SIGKILL the process group, then communicate() reaps cleanly.
    """
    cmd = [_adb_binary()] + list(args)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
    except OSError as e:
        return {"success": False, "error": str(e)}

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Kill whole process group to clean up any forks adb may have made.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        return {"success": False, "error": "Command timed out", "stdout": "", "stderr": ""}

    return {
        "success": proc.returncode == 0,
        "stdout": stdout,
        "stderr": stderr,
        "returncode": proc.returncode,
    }


def ensure_adb_server():
    """Start the adb server explicitly so we can talk to it on :5037.

    Idempotent — `adb start-server` is a no-op if already running. Run with
    detached stdio so adb's daemon-detach logic isn't confused by inherited
    pipes from a parent that's also the container's PID 1.
    """
    cmd = [_adb_binary(), "start-server"]
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
            return False
        return proc.returncode == 0
    except OSError as e:
        print(f"ensure_adb_server: failed to start adb: {e}")
        return False


# ---------------------------------------------------------------------------
# Device registry, broadcaster, ADB monitor, enrichment, player-state poller.
#
# Architecture:
#   AdbMonitor connects to localhost:5037 and runs `host:track-devices-l`.
#   The adb server pushes a fresh device list whenever any device transitions.
#   The monitor parses each push, updates DeviceRegistry, and triggers
#   enrichment for newly-arrived devices via the single-worker adb_executor.
#   PlayerStatePoller queries each online device's HTTP API on a slow cadence.
#   Broadcaster pushes registry snapshots over SSE to subscribed clients —
#   the dashboard never polls the server.
# ---------------------------------------------------------------------------


class DeviceRegistry:
    """Source of truth for ADB-known devices.

    Keyed by adb address (the string adb itself uses). Serial is the
    canonical de-dup key once we've discovered it via enrichment.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._by_addr = {}            # addr -> dict
        self._enriched_serials = set()  # serials we've fully enriched at least once
        self._listeners = []          # callables fired on any state change

    def add_listener(self, fn):
        with self._lock:
            self._listeners.append(fn)

    def _notify(self):
        # Listeners are called with the lock released to avoid deadlocks.
        listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn()
            except Exception as e:
                print(f"registry listener error: {e}")

    def replace_from_track(self, parsed):
        """Replace tracked-device state from a track-devices-l snapshot.

        `parsed` is a list of dicts with at least 'address' and 'state'.
        Removes addresses no longer present, adds new ones, updates state on
        existing entries. Returns (new_addrs, removed_addrs).
        """
        new_addrs, removed_addrs = [], []
        with self._lock:
            seen = set()
            for d in parsed:
                addr = d["address"]
                seen.add(addr)
                entry = self._by_addr.get(addr)
                if entry is None:
                    entry = {
                        "address": addr,
                        "state": d.get("state", "unknown"),
                        "model": d.get("model"),
                        "serial": None,
                        "is_device_owner": False,
                        "name": None,
                        "version": None,
                        "ip": None,
                        "resolved_ip": None,
                        "player_state": None,
                        "first_seen": datetime.now().isoformat(),
                        "last_state_change": datetime.now().isoformat(),
                        "enrichment_pending": False,
                        "enriched_at": None,
                    }
                    self._by_addr[addr] = entry
                    new_addrs.append(addr)
                else:
                    if entry["state"] != d.get("state"):
                        entry["state"] = d.get("state", "unknown")
                        entry["last_state_change"] = datetime.now().isoformat()
                    # Model only known once a device is "device" (online).
                    if d.get("model") and not entry.get("model"):
                        entry["model"] = d["model"]
            for addr in list(self._by_addr.keys()):
                if addr not in seen:
                    removed_addrs.append(addr)
                    del self._by_addr[addr]
        if new_addrs or removed_addrs:
            self._notify()
        return new_addrs, removed_addrs

    def update(self, addr, **fields):
        with self._lock:
            entry = self._by_addr.get(addr)
            if entry is None:
                return False
            entry.update(fields)
        self._notify()
        return True

    def update_player_state(self, addr, state):
        # Player state changes don't need to fan out a full notify storm —
        # we still notify, but callers can throttle if needed.
        return self.update(addr, player_state=state)

    def mark_enriched(self, addr, serial):
        with self._lock:
            entry = self._by_addr.get(addr)
            if entry is None:
                return
            entry["enrichment_pending"] = False
            entry["enriched_at"] = datetime.now().isoformat()
            if serial:
                entry["serial"] = serial
                self._enriched_serials.add(serial)
        self._notify()

    def claim_enrichment(self, addr):
        """Atomically mark a device's enrichment as in-flight. Returns True
        if the caller should run enrichment, False if someone else has it.
        """
        with self._lock:
            entry = self._by_addr.get(addr)
            if entry is None or entry.get("enrichment_pending"):
                return False
            entry["enrichment_pending"] = True
            return True

    def needs_refresh(self, addr, max_age_seconds):
        with self._lock:
            entry = self._by_addr.get(addr)
            if entry is None or entry.get("state") != "device":
                return False
            if entry.get("enrichment_pending"):
                return False
            ts = entry.get("enriched_at")
            if not ts:
                return True
            try:
                dt = datetime.fromisoformat(ts)
            except ValueError:
                return True
            return (datetime.now() - dt).total_seconds() >= max_age_seconds

    def online_addresses_with_ip(self):
        with self._lock:
            return [
                (addr, entry.get("resolved_ip") or entry.get("ip"))
                for addr, entry in self._by_addr.items()
                if entry.get("state") == "device"
                and (entry.get("resolved_ip") or entry.get("ip"))
            ]

    def all_addresses(self):
        with self._lock:
            return list(self._by_addr.keys())

    def snapshot(self):
        with self._lock:
            # Deep-ish copy: each entry is a flat dict so a shallow copy is enough.
            return [dict(e) for e in self._by_addr.values()]

    def deduped_snapshot(self):
        """Snapshot collapsed by serial (so the same physical tablet seen
        via both IP:port and mDNS shows up once). Falls back to address
        when serial isn't yet known.
        """
        seen_by_serial = {}
        ungrouped = []
        for d in self.snapshot():
            serial = d.get("serial")
            if not serial:
                ungrouped.append(d)
                continue
            existing = seen_by_serial.get(serial)
            if existing is None:
                seen_by_serial[serial] = d
            else:
                # Prefer the IP:port address over an mDNS name.
                cur = existing.get("address", "")
                new = d.get("address", "")
                if new and ":" in new and not new.startswith("adb-") and (
                    cur.startswith("adb-") or ":" not in cur
                ):
                    seen_by_serial[serial] = d
        return list(seen_by_serial.values()) + ungrouped

    def remove(self, addr):
        with self._lock:
            existed = self._by_addr.pop(addr, None) is not None
        if existed:
            self._notify()
        return existed


registry = DeviceRegistry()


class Broadcaster:
    """Fan-out for SSE subscribers.

    Each subscriber gets a bounded queue. If a queue fills up (slow consumer),
    the oldest message is dropped — we never block a publisher.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._subs = []  # list of queue.Queue

    def subscribe(self):
        q = queue.Queue(maxsize=SSE_QUEUE_MAX)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def publish(self, message):
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(message)
            except queue.Full:
                # Slow consumer — drop oldest, then push.
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(message)
                except queue.Full:
                    pass

    def subscriber_count(self):
        with self._lock:
            return len(self._subs)


broadcaster = Broadcaster()


def _parse_track_devices_payload(text):
    """Parse the body adb's track-devices-l service emits.

    Each non-empty line: "<serial-or-addr>\t<state> [<key>:<value> ...]".
    """
    devices_out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        addr = parts[0]
        state = parts[1]
        model = None
        for p in parts[2:]:
            if p.startswith("model:"):
                model = p.split(":", 1)[1]
                break
        devices_out.append({"address": addr, "state": state, "model": model})
    return devices_out


class AdbMonitor(threading.Thread):
    """Background thread that streams device state from adb's host service.

    Talks the adb wire protocol directly to localhost:5037 — never spawns
    `adb` subprocesses for tracking. Reconnects with capped backoff on
    failure, calling ensure_adb_server() to bring the daemon back up.
    """

    def __init__(self, registry, on_new_device=None):
        super().__init__(daemon=True, name="adb-monitor")
        self.registry = registry
        self.on_new_device = on_new_device
        self._sock = None

    def stop(self):
        try:
            if self._sock is not None:
                self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _connect(self):
        s = socket.create_connection((ADB_HOST, ADB_PORT), timeout=10)
        # Once connected we want the read to block; the adb server may go
        # arbitrarily long between updates.
        s.settimeout(None)
        return s

    def _send_request(self, sock, command):
        payload = command.encode("ascii")
        sock.sendall(b"%04x" % len(payload) + payload)
        status = self._read_exact(sock, 4)
        if status != b"OKAY":
            # Try to read the failure message.
            try:
                err_len_hex = self._read_exact(sock, 4).decode("ascii")
                err_len = int(err_len_hex, 16)
                err = self._read_exact(sock, err_len).decode("utf-8", "replace")
            except Exception:
                err = "unknown"
            raise RuntimeError(f"adb host service rejected {command!r}: {err}")

    @staticmethod
    def _read_exact(sock, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("adb server closed connection")
            buf.extend(chunk)
        return bytes(buf)

    def _read_message(self, sock):
        length_hex = self._read_exact(sock, 4).decode("ascii")
        length = int(length_hex, 16)
        if length == 0:
            return ""
        return self._read_exact(sock, length).decode("utf-8", "replace")

    def run(self):
        backoff = 1.0
        while not shutdown_event.is_set():
            try:
                ensure_adb_server()
                self._sock = self._connect()
                self._send_request(self._sock, "host:track-devices-l")
                backoff = 1.0  # successful connect resets backoff
                print("adb-monitor: connected to adb server, tracking devices")
                while not shutdown_event.is_set():
                    body = self._read_message(self._sock)
                    parsed = _parse_track_devices_payload(body)
                    new_addrs, removed_addrs = self.registry.replace_from_track(parsed)
                    if new_addrs and self.on_new_device:
                        for addr in new_addrs:
                            try:
                                self.on_new_device(addr)
                            except Exception as e:
                                print(f"adb-monitor on_new_device error: {e}")
                    if removed_addrs:
                        print(f"adb-monitor: devices removed: {removed_addrs}")
            except Exception as e:
                if shutdown_event.is_set():
                    break
                print(f"adb-monitor: connection lost ({e!r}); retrying in {backoff:.1f}s")
                shutdown_event.wait(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                try:
                    if self._sock is not None:
                        self._sock.close()
                except OSError:
                    pass
                self._sock = None


# ---------- Enrichment (slow-path adb shell calls, serialized) -------------


def _enrich_device(addr):
    """Populate serial / device-owner / app info / IP for a single device.

    Runs on the single-worker adb_executor so concurrent enrichment never
    multiplies adb shell calls. Updates the registry on completion.
    """
    if shutdown_event.is_set():
        return
    serial = None
    is_device_owner = False
    app_name = None
    app_version = None
    resolved_ip = None
    ip = None

    # Serial — short, cheap, stable.
    r = run_adb("-s", addr, "shell", "getprop", "ro.serialno", timeout=10)
    if r.get("success"):
        serial = r["stdout"].strip() or None

    # Resolved IP for the device. For IP:port addresses it's just the IP.
    if ":" in addr and not addr.startswith("adb-"):
        ip = addr.split(":", 1)[0]
    else:
        r = run_adb("-s", addr, "shell", "ip", "route", "get", "1", timeout=10)
        if r.get("success"):
            m = re.search(r"src\s+(\d+\.\d+\.\d+\.\d+)", r.get("stdout", ""))
            if m:
                ip = m.group(1)
        if not ip:
            ip = resolve_mdns_to_ip(addr)
        if ip:
            resolved_ip = ip

    # Device owner. dumpsys is heavy but only runs once per enrichment cycle.
    r = run_adb("-s", addr, "shell", "dumpsys", "device_policy", timeout=15)
    if r.get("success"):
        out = r.get("stdout", "")
        is_device_owner = "Device Owner" in out and PACKAGE in out

    # App name/version via the device's own HTTP API (cheap, doesn't go through adb).
    if ip:
        try:
            req = urllib.request.Request(
                f"http://{ip}:8765/", headers={"User-Agent": "UpdateServer"}
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read().decode())
                app_name = data.get("name") or app_name
                app_version = data.get("version") or app_version
        except Exception:
            pass

    fields = {
        "is_device_owner": is_device_owner,
        "ip": ip,
        "resolved_ip": resolved_ip,
    }
    if app_name:
        fields["name"] = app_name
    if app_version:
        fields["version"] = app_version
    registry.update(addr, **fields)
    registry.mark_enriched(addr, serial)


def schedule_enrichment(addr):
    """Schedule an enrichment for `addr` if not already in flight."""
    if not registry.claim_enrichment(addr):
        return
    adb_executor.submit(_enrich_device, addr)


class EnrichmentRefresher(threading.Thread):
    """Periodically re-enriches stale device entries (device-owner status,
    app version) so the dashboard reflects changes that happen out-of-band.
    """

    def __init__(self):
        super().__init__(daemon=True, name="enrichment-refresher")

    def run(self):
        while not shutdown_event.is_set():
            for addr in registry.all_addresses():
                if shutdown_event.is_set():
                    return
                if registry.needs_refresh(addr, ENRICH_REFRESH_SECONDS):
                    schedule_enrichment(addr)
            shutdown_event.wait(15)


# ---------- Player-state polling (HTTP, parallelized via http_executor) ----


def _fetch_player_state(ip):
    try:
        req = urllib.request.Request(
            f"http://{ip}:8765/state", headers={"User-Agent": "UpdateServer"}
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


class PlayerStatePoller(threading.Thread):
    """Polls each online device's :8765/state and updates the registry.

    Uses http_executor to fan out concurrently — total wall time per cycle
    is ~one HTTP timeout regardless of fleet size. The dashboard receives
    the change via SSE, so per-client polling is no longer needed.
    """

    def __init__(self):
        super().__init__(daemon=True, name="player-state-poller")

    def run(self):
        while not shutdown_event.is_set():
            targets = registry.online_addresses_with_ip()
            if targets:
                futures = {
                    http_executor.submit(_fetch_player_state, ip): (addr, ip)
                    for addr, ip in targets
                }
                for fut in concurrent.futures.as_completed(futures, timeout=5):
                    addr, _ip = futures[fut]
                    try:
                        state = fut.result()
                    except Exception:
                        state = None
                    if state and not state.get("error"):
                        registry.update_player_state(addr, state)
            shutdown_event.wait(PLAYER_STATE_POLL_SECONDS)


# ---------- View assembly + SSE publish wiring -----------------------------


def build_devices_view():
    """Build the merged devices list the dashboard renders.

    Combines ADB-known devices (from registry) with app-side check-ins
    (from the legacy `devices` dict), keyed by IP when possible.
    """
    adb_entries = registry.deduped_snapshot()
    with data_lock:
        app_devices_snapshot = {k: dict(v) for k, v in devices.items()}

    merged = {}
    for d in adb_entries:
        addr = d["address"]
        ip = d.get("resolved_ip") or d.get("ip")
        if not ip and ":" in addr and not addr.startswith("adb-"):
            ip = addr.split(":", 1)[0]
        key = ip or d.get("serial") or addr
        merged[key] = {
            "name": d.get("name") or d.get("model") or addr,
            "ip": ip,
            "adb_address": addr,
            "is_device_owner": bool(d.get("is_device_owner")),
            "adb_connected": d.get("state") == "device",
            "adb_state": d.get("state"),
            "app_connected": False,
            "version": d.get("version"),
            "last_seen": None,
            "player_state": d.get("player_state"),
            "model": d.get("model"),
            "serial": d.get("serial"),
            "resolved_ip": d.get("resolved_ip"),
        }

    for did, info in app_devices_snapshot.items():
        ip = info.get("ip_address")
        if ip and ip in merged:
            merged[ip]["version"] = info.get("app_version") or merged[ip].get("version")
            merged[ip]["last_seen"] = info.get("last_seen")
            merged[ip]["app_connected"] = True
        else:
            key = ip or did
            merged[key] = {
                "name": info.get("device_name") or did,
                "ip": ip,
                "adb_address": None,
                "is_device_owner": False,
                "adb_connected": False,
                "adb_state": None,
                "app_connected": True,
                "version": info.get("app_version"),
                "last_seen": info.get("last_seen"),
                "player_state": None,
                "model": None,
                "serial": None,
                "resolved_ip": None,
            }

    return list(merged.values())


def _registry_changed():
    """Listener on registry changes: publish a fresh devices snapshot."""
    payload = json.dumps({"type": "devices", "devices": build_devices_view()})
    broadcaster.publish(payload)


registry.add_listener(_registry_changed)


def publish_logs_event(entries):
    """Push log entries to SSE subscribers (no-op if nobody subscribed)."""
    if not entries:
        return
    if broadcaster.subscriber_count() == 0:
        return
    payload = json.dumps({"type": "logs", "logs": entries})
    broadcaster.publish(payload)


def publish_devices_now():
    """Force-publish the current snapshot (e.g., after a manual mutation)."""
    payload = json.dumps({"type": "devices", "devices": build_devices_view()})
    broadcaster.publish(payload)


def run_in_adb_executor(fn, *args, timeout=180):
    """Submit an adb command to the single-worker queue and wait.

    Serializes user-initiated adb operations so concurrent dashboard clicks
    can't pile up parallel adb invocations. Internal enrichment runs on the
    same executor — calls block behind in-flight work, which is what we want.
    """
    fut = adb_executor.submit(fn, *args)
    try:
        return fut.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        return {"success": False, "message": "ADB queue timeout"}


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


def get_server_ip():
    """Get this server's IP address for configuring devices."""
    import socket
    try:
        # Connect to a remote address to determine our local IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def adb_push_update(device):
    """Push APK update to device via ADB."""
    # Resolve mDNS name to IP:port if needed
    device = resolve_adb_address(device)

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

        # Configure update server host in SharedPreferences (only if not already set)
        server_ip = get_server_ip()
        if server_ip:
            import base64
            prefs_dir = f"/data/data/{PACKAGE}/shared_prefs"
            prefs_file = f"{prefs_dir}/media_player_prefs.xml"

            # Always update the server URL to this server's IP
            # This ensures devices point to the correct server after migration
            check_result = run_adb("-s", device, "shell", "run-as", PACKAGE,
                                   "cat", prefs_file)
            prefs_content = check_result.get("stdout", "")

            # Create prefs dir if needed
            run_adb("-s", device, "shell", f'run-as {PACKAGE} mkdir -p {prefs_dir}')

            if "</map>" in prefs_content:
                # Update or insert the server host
                import re
                if "update_server_host" in prefs_content:
                    # Replace existing server host
                    new_prefs = re.sub(
                        r'<string name="update_server_host">[^<]*</string>',
                        f'<string name="update_server_host">{server_ip}</string>',
                        prefs_content
                    )
                else:
                    # Insert the server host before </map>
                    new_prefs = prefs_content.replace(
                        "</map>",
                        f'    <string name="update_server_host">{server_ip}</string>\n</map>'
                    )
            else:
                # No existing prefs, create new file
                new_prefs = f'''<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
    <string name="update_server_host">{server_ip}</string>
    <boolean name="service_running" value="true" />
</map>'''

            # Base64 encode to avoid shell escaping issues, write within run-as context
            encoded = base64.b64encode(new_prefs.encode()).decode()
            shell_cmd = f'echo {encoded} | base64 -d > {prefs_file}'
            run_adb("-s", device, "shell", f'run-as {PACKAGE} sh -c "{shell_cmd}"')

        # Start the app
        run_adb("-s", device, "shell", "am", "start", "-n", f"{PACKAGE}/.MainActivity")
        return {"success": True, "message": f"Update installed successfully (server: {server_ip})"}
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
    device = resolve_adb_address(device)
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
    device = resolve_adb_address(device)
    result = run_adb("-s", device, "shell", "dumpsys", "device_policy")
    is_owner = "Device Owner" in result.get("stdout", "") and PACKAGE in result.get("stdout", "")
    return {"is_device_owner": is_owner}


def adb_disable_play_protect(device):
    """Disable Play Protect verification."""
    device = resolve_adb_address(device)
    run_adb("-s", device, "shell", "settings", "put", "global", "package_verifier_enable", "0")
    run_adb("-s", device, "shell", "settings", "put", "global", "verifier_verify_adb_installs", "0")
    return {"success": True, "message": "Play Protect disabled"}


def resolve_mdns_to_ip(service_name, with_port=False):
    """Resolve an mDNS service name to an IP address using avahi-browse.

    Args:
        service_name: An mDNS service name like 'adb-SERIAL-random._adb-tls-connect._tcp'
        with_port: If True, return 'ip:port' string; if False, return just IP

    Returns:
        IP address string (or ip:port if with_port=True), or None if resolution fails
    """
    # Extract the instance name (before the service type)
    # Format: adb-SERIAL-random._adb-tls-connect._tcp
    if '._adb-tls-connect._tcp' in service_name:
        instance_name = service_name.replace('._adb-tls-connect._tcp', '')
    else:
        instance_name = service_name

    try:
        # Use avahi-browse to find all ADB TLS services and their IPs
        result = subprocess.run(
            ['avahi-browse', '-rpt', '_adb-tls-connect._tcp'],
            capture_output=True, text=True, timeout=5
        )

        if result.returncode != 0:
            return None

        # Parse output - look for resolved lines (starting with '=')
        # Format: =;interface;protocol;name;type;domain;hostname;address;port;txt
        for line in result.stdout.split('\n'):
            if line.startswith('=') and instance_name in line:
                parts = line.split(';')
                if len(parts) >= 9:
                    ip = parts[7]
                    port = parts[8]
                    # Validate it looks like an IP
                    if re.match(r'\d+\.\d+\.\d+\.\d+', ip):
                        if with_port and port:
                            return f"{ip}:{port}"
                        return ip

        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
        print(f"avahi-browse failed: {e}")
        return None


def resolve_adb_address(device):
    """Resolve an ADB device address to IP:port if it's an mDNS name.

    Args:
        device: ADB device address (could be IP:port or mDNS service name)

    Returns:
        Resolved IP:port or original address if already IP-based
    """
    if device.startswith('adb-') or '._adb-tls-connect._tcp' in device:
        resolved = resolve_mdns_to_ip(device, with_port=True)
        if resolved:
            # Try to connect via the resolved address
            connect_result = run_adb("connect", resolved)
            if connect_result.get("success") or "already connected" in connect_result.get("stdout", ""):
                print(f"Resolved {device} to {resolved}")
                return resolved
            else:
                print(f"Failed to connect to resolved address {resolved}: {connect_result}")
    return device


def get_device_app_info(device):
    """Get the device name and version from the app's API."""
    import urllib.request
    try:
        # Get IP from device address
        if ':' in device and not device.startswith('adb-'):
            ip = device.split(':')[0]
        else:
            ip = None

            # For mDNS, try to get IP via adb shell first
            result = run_adb("-s", device, "shell", "ip", "route", "get", "1")
            match = re.search(r'src\s+(\d+\.\d+\.\d+\.\d+)', result.get("stdout", ""))
            if match:
                ip = match.group(1)

            # Fallback: try avahi-browse to resolve mDNS service name
            if not ip:
                ip = resolve_mdns_to_ip(device)

            if not ip:
                return None, None, None

        url = f"http://{ip}:8765/"
        req = urllib.request.Request(url, headers={'User-Agent': 'UpdateServer'})
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode())
            return data.get("name"), data.get("version"), ip
    except Exception:
        return None, None, None


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


def set_device_player_name(ip, name):
    """Set the media player name via the app's API."""
    import urllib.request
    try:
        url = f"http://{ip}:8765/name"
        data = json.dumps({"name": name}).encode()
        req = urllib.request.Request(url, data=data, headers={
            'User-Agent': 'UpdateServer',
            'Content-Type': 'application/json'
        }, method='POST')
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"success": False, "message": str(e)}


def adb_set_tablet_name(device, name):
    """Set the tablet device name via ADB (requires device owner or root)."""
    device = resolve_adb_address(device)
    # Try multiple methods to set device name
    results = []

    # Method 1: Set global device_name setting (Android 7+)
    result = run_adb("-s", device, "shell", "settings", "put", "global", "device_name", name)
    results.append(("global device_name", result))

    # Method 2: Set secure bluetooth_name (affects Bluetooth name)
    result = run_adb("-s", device, "shell", "settings", "put", "secure", "bluetooth_name", name)
    results.append(("bluetooth_name", result))

    # Method 3: Set system device_name (older Android versions)
    result = run_adb("-s", device, "shell", "settings", "put", "system", "device_name", name)
    results.append(("system device_name", result))

    # Check if any succeeded
    success = any(r[1].get("success", False) for r in results)

    if success:
        return {"success": True, "message": f"Tablet name set to '{name}'"}
    else:
        return {"success": False, "message": "Failed to set tablet name (may require device owner)"}


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
    <title>Android Media Player Monitor v{{SERVER_VERSION}}</title>
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
        .grid { display: grid; grid-template-columns: 2fr 3fr; gap: 20px; }
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
            display: flex; justify-content: space-between; align-items: center;
            border-left: 4px solid #333; transition: border-color 0.2s, background 0.2s; }
        .adb-device.expanded { border-left-color: #00d4ff; background: #0f3460; }
        .adb-device.outdated, .adb-device.expanded.outdated { background: #2d2416 !important; border-left-color: #f6ad55 !important; }
        .device-header { display: flex; align-items: flex-start; cursor: pointer; }
        .device-header:hover { opacity: 0.85; }
        .expand-btn { background: #1a3a5c; border: none; color: #aaa; font-size: 1em; cursor: pointer;
            padding: 4px 10px; border-radius: 4px; margin-right: 10px; }
        .expand-btn:hover { color: #00d4ff; background: #234; }
        .device-summary { display: flex; flex-direction: column; gap: 4px; flex: 1; }
        .device-state-mini { font-size: 0.85em; color: #888; max-width: 200px; overflow: hidden;
            text-overflow: ellipsis; white-space: nowrap; }
        .device-state-mini.playing { color: #48bb78; }
        .device-state-mini.paused { color: #ecc94b; }
        .device-details { margin-top: 12px; padding-top: 12px; border-top: 1px solid #1a3a5c; }
        .toast { position: fixed; bottom: 20px; right: 20px; padding: 15px 25px; border-radius: 8px;
            background: #48bb78; color: #fff; z-index: 2000; display: none; }
        .toast.error { background: #f56565; }
        .toast.active { display: block; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Android Media Player Monitor <span style="font-size:0.5em;color:#888;">v{{SERVER_VERSION}}</span></h1>

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
                <div class="stat-value">{{SERVER_VERSION}}</div>
                <div class="stat-label">Server Version</div>
            </div>
            <div class="stat-box">
                <div class="stat-value" id="apk-version">-</div>
                <div class="stat-label">APK Version</div>
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
            <!-- Step 1: Pairing -->
            <div id="pairStep">
                <h3 class="modal-title">Step 1: Pair Device</h3>
                <p style="color:#aaa;margin-bottom:20px;">Enable Wireless Debugging on the tablet, tap "Pair device with pairing code", then enter the info shown.</p>
                <div class="form-group">
                    <label>Device IP Address</label>
                    <input type="text" id="device-ip" placeholder="192.168.1.100">
                </div>
                <div class="form-group">
                    <label>Pairing Port (shown on device)</label>
                    <input type="text" id="pair-port" placeholder="37123">
                </div>
                <div class="form-group">
                    <label>Pairing Code (shown on device)</label>
                    <input type="text" id="pair-code" placeholder="123456">
                </div>
                <div style="margin-top:20px;">
                    <button class="btn btn-success" onclick="pairDevice()">Pair</button>
                    <button class="btn btn-secondary" onclick="closeModal()">Cancel</button>
                </div>
            </div>
            <!-- Step 2: Connect -->
            <div id="connectStep" style="display:none;">
                <h3 class="modal-title">Step 2: Connect</h3>
                <p style="color:#aaa;margin-bottom:20px;">Pairing successful! Now enter the connection port shown on the device under "Wireless debugging".</p>
                <div class="form-group">
                    <label>Device IP Address</label>
                    <input type="text" id="connect-ip" disabled>
                </div>
                <div class="form-group">
                    <label>Connection Port (shown on device)</label>
                    <input type="text" id="connect-port" placeholder="41297">
                </div>
                <div style="margin-top:20px;">
                    <button class="btn btn-success" onclick="connectDevice()">Connect</button>
                    <button class="btn btn-secondary" onclick="goBackToPairStep()">Back</button>
                </div>
            </div>
        </div>
    </div>

    <!-- Device Settings Modal -->
    <div id="settingsModal" class="modal">
        <div class="modal-content">
            <h3 class="modal-title">Device Settings</h3>
            <input type="hidden" id="settings-ip">
            <input type="hidden" id="settings-adb-address">
            <div class="form-group">
                <label>Media Player Name</label>
                <input type="text" id="settings-player-name" placeholder="Living Room Speaker">
                <p style="font-size:0.8em;color:#888;margin-top:5px;">Name shown in Home Assistant and the web UI</p>
            </div>
            <div class="form-group">
                <label>Tablet Device Name (via ADB)</label>
                <input type="text" id="settings-tablet-name" placeholder="Kitchen Tablet">
                <p style="font-size:0.8em;color:#888;margin-top:5px;">Android device name (requires ADB connection)</p>
            </div>
            <div style="margin-top:20px;">
                <button class="btn btn-success" onclick="saveDeviceSettings()">Save</button>
                <button class="btn btn-secondary" onclick="closeSettingsModal()">Cancel</button>
            </div>
        </div>
    </div>

    <!-- Toast notification -->
    <div id="toast" class="toast"></div>

    <script>
        // App base URL — computed from the current page so fetches work both
        // at origin root (http://host:9742/) and under a path prefix behind
        // a reverse proxy that strips the prefix before forwarding
        // (https://host/amplayer/).
        const API_BASE = new URL('.', document.baseURI).href.replace(/\\/$/, '');

        let allLogs = [];
        let allDevices = [];
        let latestApkVersion = null;
        let expandedDevices = new Set(JSON.parse(localStorage.getItem('expandedDevices') || '[]'));

        function toggleDevice(deviceId) {
            if (expandedDevices.has(deviceId)) {
                expandedDevices.delete(deviceId);
            } else {
                expandedDevices.add(deviceId);
            }
            localStorage.setItem('expandedDevices', JSON.stringify([...expandedDevices]));
            renderDevices();
        }

        function showToast(msg, isError = false) {
            const toast = document.getElementById('toast');
            toast.textContent = msg;
            toast.className = 'toast active' + (isError ? ' error' : '');
            setTimeout(() => toast.className = 'toast', 3000);
        }

        function showAddDeviceModal() {
            // Reset to step 1
            document.getElementById('pairStep').style.display = 'block';
            document.getElementById('connectStep').style.display = 'none';
            document.getElementById('device-ip').value = '';
            document.getElementById('pair-port').value = '';
            document.getElementById('pair-code').value = '';
            document.getElementById('connect-port').value = '';
            document.getElementById('addDeviceModal').classList.add('active');
        }

        function closeModal() {
            document.getElementById('addDeviceModal').classList.remove('active');
        }

        function showSettingsModal(name, ip, adbAddress) {
            // Extract IP from adbAddress if ip is not provided
            let effectiveIp = ip;
            if (!effectiveIp && adbAddress && adbAddress.includes(':') && !adbAddress.startsWith('adb-')) {
                effectiveIp = adbAddress.split(':')[0];
            }
            console.log('showSettingsModal:', {name, ip, adbAddress, effectiveIp});
            document.getElementById('settings-ip').value = effectiveIp || '';
            document.getElementById('settings-adb-address').value = adbAddress || '';
            document.getElementById('settings-player-name').value = name || '';
            document.getElementById('settings-tablet-name').value = name || '';
            document.getElementById('settingsModal').classList.add('active');
        }

        function closeSettingsModal() {
            document.getElementById('settingsModal').classList.remove('active');
        }

        async function saveDeviceSettings() {
            const ip = document.getElementById('settings-ip').value;
            const adbAddress = document.getElementById('settings-adb-address').value;
            const playerName = document.getElementById('settings-player-name').value.trim();
            const tabletName = document.getElementById('settings-tablet-name').value.trim();

            let playerSuccess = false;
            let tabletSuccess = false;
            let messages = [];

            console.log('saveDeviceSettings:', {ip, adbAddress, playerName, tabletName});

            // Set media player name (via HTTP to the app)
            if (!ip) {
                messages.push('No IP address available');
            } else if (!playerName) {
                messages.push('Player name is empty');
            }

            if (ip && playerName) {
                showToast('Setting media player name...');
                try {
                    const resp = await fetch(`${API_BASE}/api/set-player-name`, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ip, name: playerName})
                    });
                    const result = await resp.json();
                    if (result.success) {
                        playerSuccess = true;
                        messages.push('Media player name updated');
                    } else {
                        messages.push('Media player: ' + (result.message || 'Failed'));
                    }
                } catch (e) {
                    messages.push('Media player: ' + e.message);
                }
            }

            // Set tablet device name (via ADB)
            if (adbAddress && tabletName) {
                showToast('Setting tablet name via ADB...');
                try {
                    const resp = await fetch(`${API_BASE}/api/set-tablet-name`, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({device: adbAddress, name: tabletName})
                    });
                    const result = await resp.json();
                    if (result.success) {
                        tabletSuccess = true;
                        messages.push('Tablet name updated');
                    } else {
                        messages.push('Tablet: ' + (result.message || 'Failed'));
                    }
                } catch (e) {
                    messages.push('Tablet: ' + e.message);
                }
            }

            const anySuccess = playerSuccess || tabletSuccess;
            const allSuccess = (!ip || !playerName || playerSuccess) && (!adbAddress || !tabletName || tabletSuccess);

            if (messages.length > 0) {
                showToast(messages.join('; '), !allSuccess);
            }

            // Close modal if at least one operation succeeded
            if (anySuccess) {
                closeSettingsModal();
                // Small delay to ensure device has updated, then refresh
                setTimeout(() => fetchAllDevices(), 500);
            }
        }

        function goBackToPairStep() {
            document.getElementById('pairStep').style.display = 'block';
            document.getElementById('connectStep').style.display = 'none';
        }

        async function pairDevice() {
            const ip = document.getElementById('device-ip').value.trim();
            const pairPort = document.getElementById('pair-port').value.trim();
            const pairCode = document.getElementById('pair-code').value.trim();

            if (!ip || !pairPort || !pairCode) {
                showToast('All fields are required', true);
                return;
            }

            try {
                showToast('Pairing...');
                const pairResp = await fetch(`${API_BASE}/api/adb/pair`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ip, port: pairPort, code: pairCode})
                });
                const pairResult = await pairResp.json();
                if (!pairResult.success) {
                    showToast('Pairing failed: ' + pairResult.message, true);
                    return;
                }
                showToast('Paired successfully!');
                // Move to step 2
                document.getElementById('connect-ip').value = ip;
                document.getElementById('pairStep').style.display = 'none';
                document.getElementById('connectStep').style.display = 'block';
            } catch (e) {
                showToast('Error: ' + e.message, true);
            }
        }

        async function connectDevice() {
            const ip = document.getElementById('connect-ip').value.trim();
            const connectPort = document.getElementById('connect-port').value.trim();

            if (!connectPort) {
                showToast('Connection port is required', true);
                return;
            }

            try {
                showToast('Connecting...');
                const connResp = await fetch(`${API_BASE}/api/adb/connect`, {
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
                const resp = await fetch(`${API_BASE}/api/adb/push`, {
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
                const resp = await fetch(`${API_BASE}/api/adb/device-owner`, {
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
                const resp = await fetch(`${API_BASE}/api/adb/disable-protect`, {
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

        async function deleteDevice(name, ip, adbAddress) {
            if (!confirm(`Delete "${name}" from the devices list?\\n\\nThis will remove the device from tracking and disconnect ADB if connected.`)) return;
            showToast('Removing device...');
            try {
                const resp = await fetch(`${API_BASE}/api/delete-device`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ip: ip, adb_address: adbAddress})
                });
                const result = await resp.json();
                if (result.success) {
                    showToast('Device removed');
                    fetchAllDevices();
                } else {
                    showToast(result.message || 'Failed to remove device', true);
                }
            } catch (e) {
                showToast('Error: ' + e.message, true);
            }
        }

        async function fetchPlayerState(ip) {
            try {
                const resp = await fetch(`${API_BASE}/api/player-state/${ip}`);
                return await resp.json();
            } catch (e) {
                return null;
            }
        }

        async function fetchAllDevices() {
            try {
                // Fetch both ADB and app devices
                const [adbResp, appResp] = await Promise.all([
                    fetch(`${API_BASE}/api/adb/devices`),
                    fetch(`${API_BASE}/api/devices`)
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
                    // Use resolved_ip from avahi if available, otherwise extract from address
                    const ip = d.resolved_ip || (d.address.includes(':') && !d.address.startsWith('adb-')
                        ? d.address.split(':')[0] : null);
                    const key = ip || d.serial || d.address;
                    merged[key] = {
                        name: d.name || d.model || d.address,
                        ip: ip,
                        adb_address: d.address,
                        is_device_owner: d.is_device_owner,
                        adb_connected: true,
                        app_connected: false,
                        version: d.version || null,
                        last_seen: null,
                        player_state: ip ? existingStates[ip] || null : null
                    };
                }
                for (const [id, d] of Object.entries(appDevices)) {
                    const ip = d.ip_address;
                    if (ip && merged[ip]) {
                        // Don't overwrite name from ADB (fresh from device) with cached name
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
                // Player state for each device is pushed by the server via SSE
                // (or fetched in bulk via /api/state on the fallback path) —
                // no per-device polling needed from the browser.
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
                const isExpanded = expandedDevices.has(deviceId);

                let card = document.getElementById(deviceId);
                const isOnline = d.player_state || (d.app_connected && d.last_seen && (Date.now() - new Date(d.last_seen).getTime()) < 300000);

                // Build badges
                const badges =
                    (d.is_device_owner ? '<span class="status-badge status-owner">Silent Updates</span>' : '') +
                    (isOnline ? '<span class="status-badge status-online">Online</span>' : '') +
                    (d.adb_connected ? '<span class="status-badge" style="background:#4fc3f733;color:#4fc3f7;">ADB</span>' : '');

                // Mini state for collapsed view
                const psData = getPlayerStateData(d.player_state);
                const escapeHtml = (s) => s ? s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;') : '';
                const safeTitle = escapeHtml(psData.title);
                let miniState = '';
                if (psData.state === 'playing' && psData.title) {
                    miniState = `<span class="device-state-mini playing" title="${safeTitle}">${safeTitle}</span>`;
                } else if (psData.state === 'paused' && psData.title) {
                    miniState = `<span class="device-state-mini paused" title="${safeTitle}">${safeTitle}</span>`;
                } else if (psData.state === 'buffering') {
                    miniState = `<span class="device-state-mini">Buffering...</span>`;
                } else {
                    miniState = `<span class="device-state-mini">Idle</span>`;
                }

                // Check if device is running an older version
                // d.version is like "2.0.3 (33)" - extract just the version number
                const deviceVersion = d.version ? d.version.split(' ')[0] : null;
                const isOutdated = latestApkVersion && deviceVersion && deviceVersion !== latestApkVersion;

                // Static hash includes expanded state and outdated status
                const staticHash = d.name + badges + isExpanded + psData.state + psData.title + psData.artist + isOutdated;

                // Build expanded content
                const info =
                    (d.ip ? 'IP: ' + d.ip + ':8765' : '') +
                    (d.version ? ' | Version: ' + d.version : '') +
                    (d.adb_address && d.adb_address !== d.ip + ':41297' ? ' | ADB: ' + d.adb_address : '');
                const isPlaying = d.player_state && d.player_state.state === 'playing';
                const isPaused = d.player_state && d.player_state.state === 'paused';
                const settingsBtn = `<button class="btn btn-small btn-secondary" onclick="event.stopPropagation();showSettingsModal('${(d.name || '').replace(/'/g, "\\'")}', '${d.ip || ''}', '${d.adb_address || ''}')" title="Edit device name">Edit Name</button>`;
                const deleteBtn = `<button class="btn btn-small btn-danger" onclick="event.stopPropagation();deleteDevice('${(d.name || '').replace(/'/g, "\\'")}', '${d.ip || ''}', '${d.adb_address || ''}')" title="Remove device">Delete</button>`;
                const buttons =
                    settingsBtn +
                    (d.ip ? `<button class="btn btn-small" onclick="event.stopPropagation();playTestStream('${d.ip}')">Test Stream</button>` : '') +
                    (d.ip && isPlaying ? `<button class="btn btn-small btn-secondary" onclick="event.stopPropagation();pausePlayback('${d.ip}')">Pause</button>` : '') +
                    (d.ip && isPaused ? `<button class="btn btn-small btn-success" onclick="event.stopPropagation();resumePlayback('${d.ip}')">Play</button>` : '') +
                    (d.ip ? `<button class="btn btn-small btn-danger" onclick="event.stopPropagation();stopPlayback('${d.ip}')">Stop</button>` : '') +
                    (d.adb_connected ? `<button class="btn btn-small" onclick="event.stopPropagation();pushUpdate('${d.adb_address}')">Push Update</button>` : '') +
                    (d.is_device_owner && d.ip ? `<button class="btn btn-small btn-success" onclick="event.stopPropagation();triggerOtaUpdate('${d.ip}')">OTA Update</button>` : '') +
                    (d.adb_connected && !d.is_device_owner ? `<button class="btn btn-small btn-secondary" onclick="event.stopPropagation();setDeviceOwner('${d.adb_address}')">Silent Updates</button>` : '') +
                    (d.adb_connected ? `<button class="btn btn-small btn-secondary" onclick="event.stopPropagation();disablePlayProtect('${d.adb_address}')">Disable Protect</button>` : '') +
                    deleteBtn;

                const expandedContent = isExpanded ? `
                    <div class="device-details">
                        <div style="font-size:0.85em;color:#888;margin-bottom:10px;">${info}</div>
                        ${renderPlayerState(d.player_state)}
                        <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:10px;">${buttons}</div>
                    </div>` : '';

                if (!card || card.dataset.staticHash !== staticHash) {
                    if (!card) {
                        card = document.createElement('div');
                        card.id = deviceId;
                        container.appendChild(card);
                    }
                    card.className = 'adb-device' + (isExpanded ? ' expanded' : '') + (isOutdated ? ' outdated' : '');
                    card.style.cssText = 'flex-direction:column;align-items:stretch;';
                    card.dataset.staticHash = staticHash;
                    const safeName = escapeHtml(d.name);
                    card.innerHTML = `
                        <div class="device-header" onclick="toggleDevice('${deviceId}')">
                            <button class="expand-btn">${isExpanded ? '-' : '+'}</button>
                            <div class="device-summary">
                                <div><strong style="font-size:1.1em;">${safeName}</strong> ${badges}</div>
                                <div>${miniState}</div>
                            </div>
                        </div>
                        ${expandedContent}`;
                } else if (isExpanded) {
                    // Update player state in place for expanded cards
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
                const resp = await fetch(`${API_BASE}/api/logs?limit=500`);
                allLogs = await resp.json();
                filterLogs();
                document.getElementById('log-count').textContent = allLogs.length;
            } catch (e) { console.error('Failed to fetch logs:', e); }
        }

        async function fetchVersion() {
            try {
                const resp = await fetch(`${API_BASE}/version`);
                const data = await resp.json();
                if (data.available) {
                    latestApkVersion = data.versionName;
                    document.getElementById('apk-version').textContent = data.versionName;
                    document.getElementById('apk-details').innerHTML =
                        `Version: ${data.versionName} (code ${data.versionCode})<br>Size: ${(data.size / 1024 / 1024).toFixed(2)} MB`;
                    // Re-render devices to update outdated status
                    renderDevices();
                } else {
                    latestApkVersion = null;
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

        // ----- Server-Sent Events: primary state transport -----
        // The dashboard does NOT poll. The server pushes a full snapshot
        // on connect and deltas on change. Polling fallback only kicks in
        // when SSE is unavailable (proxy strips it, network issue).

        let evtSource = null;
        let sseFallbackTimer = null;

        function applyDevicesPayload(devicesList) {
            allDevices = devicesList || [];
            document.getElementById('device-count').textContent = allDevices.length;
            renderDevices();
            updateDeviceFilter();
        }

        function applyLogsPayload(logs) {
            allLogs = logs || [];
            filterLogs();
            document.getElementById('log-count').textContent = allLogs.length;
        }

        function applyApkPayload(apk) {
            if (apk && apk.available) {
                latestApkVersion = apk.versionName;
                document.getElementById('apk-version').textContent = apk.versionName;
                document.getElementById('apk-details').innerHTML =
                    `Version: ${apk.versionName} (code ${apk.versionCode})<br>Size: ${(apk.size / 1024 / 1024).toFixed(2)} MB`;
                renderDevices();
            } else {
                latestApkVersion = null;
                document.getElementById('apk-version').textContent = 'N/A';
                document.getElementById('apk-details').textContent = 'APK not found';
            }
        }

        function appendLogs(newLogs) {
            if (!newLogs || !newLogs.length) return;
            allLogs = allLogs.concat(newLogs).slice(-2000);
            filterLogs();
            document.getElementById('log-count').textContent = allLogs.length;
        }

        async function fetchStateOnce() {
            try {
                const resp = await fetch(`${API_BASE}/api/state`);
                const data = await resp.json();
                applyDevicesPayload(data.devices);
                applyLogsPayload(data.logs);
                applyApkPayload(data.apk);
            } catch (e) { console.error('Failed to fetch state:', e); }
        }

        function startFallbackPolling() {
            if (sseFallbackTimer) return;
            // Slow polling — only when SSE refuses to stay open. One bulk
            // request fetches the same payload SSE would have pushed, so
            // there's no per-device fan-out from the browser.
            sseFallbackTimer = setInterval(fetchStateOnce, 15000);
            fetchStateOnce();
        }

        function stopFallbackPolling() {
            if (sseFallbackTimer) {
                clearInterval(sseFallbackTimer);
                sseFallbackTimer = null;
            }
        }

        function connectEventStream() {
            if (evtSource) {
                try { evtSource.close(); } catch (e) {}
            }
            try {
                evtSource = new EventSource(`${API_BASE}/api/events`);
            } catch (e) {
                startFallbackPolling();
                return;
            }
            evtSource.onmessage = (ev) => {
                stopFallbackPolling();
                let msg;
                try { msg = JSON.parse(ev.data); } catch (e) { return; }
                if (msg.type === 'snapshot') {
                    applyDevicesPayload(msg.devices);
                    applyLogsPayload(msg.logs);
                    applyApkPayload(msg.apk);
                } else if (msg.type === 'devices') {
                    applyDevicesPayload(msg.devices);
                } else if (msg.type === 'logs') {
                    appendLogs(msg.logs);
                }
            };
            evtSource.onerror = () => {
                // EventSource will auto-reconnect. If errors persist beyond
                // the browser's retry budget we fall back to slow polling.
                startFallbackPolling();
            };
            evtSource.onopen = () => {
                stopFallbackPolling();
            };
        }

        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible') {
                if (!evtSource || evtSource.readyState === 2 /* CLOSED */) {
                    connectEventStream();
                }
            }
        });

        connectEventStream();
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

        if path == "/healthz":
            self.handle_healthz()
        elif path == "/version":
            self.handle_version()
        elif path == "/apk" or path == "/update.apk":
            self.handle_apk()
        elif path == "/api/logs":
            self.handle_get_logs(parsed.query)
        elif path == "/api/devices":
            self.handle_get_devices()
        elif path == "/api/adb/devices":
            self.handle_get_adb_devices()
        elif path == "/api/state":
            self.handle_get_state()
        elif path == "/api/events":
            self.handle_events()
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
        elif self.path == "/api/set-player-name":
            self.handle_set_player_name()
        elif self.path == "/api/set-tablet-name":
            self.handle_set_tablet_name()
        elif self.path == "/api/delete-device":
            self.handle_delete_device()
        else:
            self.send_error(404, "Not Found")

    def handle_web_ui(self):
        """Serve the web UI."""
        html = WEB_UI_HTML.replace("{{SERVER_VERSION}}", SERVER_VERSION)
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def handle_healthz(self):
        """Liveness + version for the Homepage dashboard's badge (§14)."""
        body = json.dumps({"status": "ok", "version": SERVER_VERSION}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
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

    def handle_get_state(self):
        """Return the same snapshot SSE sends. Used by fallback polling."""
        with data_lock:
            logs = list(recent_logs)[-500:]
        apk_path, vcode, vname = get_best_apk()
        apk_info = (
            {
                "available": True,
                "versionCode": vcode,
                "versionName": vname,
                "size": apk_path.stat().st_size,
                "filename": apk_path.name,
            }
            if apk_path
            else {"available": False}
        )
        body = json.dumps({
            "devices": build_devices_view(),
            "logs": logs,
            "apk": apk_info,
            "server_version": SERVER_VERSION,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def handle_events(self):
        """Server-Sent Events stream of dashboard state.

        On connect, emits a snapshot containing devices, logs and APK
        version info. After that, only deltas are pushed: new logs as they
        arrive, devices on registry changes (state, enrichment, player
        state). Browser EventSource auto-reconnects on transport errors.
        """
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")  # disable proxy buffering
            self.end_headers()
        except Exception:
            return

        q = broadcaster.subscribe()
        try:
            # Initial snapshot.
            with data_lock:
                logs = list(recent_logs)[-500:]
            apk_path, vcode, vname = get_best_apk()
            apk_info = (
                {
                    "available": True,
                    "versionCode": vcode,
                    "versionName": vname,
                    "size": apk_path.stat().st_size,
                    "filename": apk_path.name,
                }
                if apk_path
                else {"available": False}
            )
            snapshot = {
                "type": "snapshot",
                "devices": build_devices_view(),
                "logs": logs,
                "apk": apk_info,
                "server_version": SERVER_VERSION,
            }
            self._sse_write(json.dumps(snapshot))

            # Push loop. Heartbeat comments every 15s keep proxies/firewalls
            # from killing idle connections.
            last_heartbeat = time.monotonic()
            while not shutdown_event.is_set():
                try:
                    msg = q.get(timeout=5)
                    self._sse_write(msg)
                except queue.Empty:
                    pass
                if time.monotonic() - last_heartbeat > 15:
                    try:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        return
                    last_heartbeat = time.monotonic()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            broadcaster.unsubscribe(q)

    def _sse_write(self, data_str):
        # SSE frame: "data: <line>\n" repeated, terminated by blank line.
        for line in data_str.splitlines() or [""]:
            self.wfile.write(b"data: " + line.encode("utf-8") + b"\n")
        self.wfile.write(b"\n")
        self.wfile.flush()

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
        """Return list of ADB-connected devices from the registry.

        Backwards-compatible with the previous endpoint shape. Reads
        in-memory state populated by AdbMonitor + enricher — never spawns
        adb subprocesses on a request thread.
        """
        out = []
        for d in registry.deduped_snapshot():
            out.append(
                {
                    "address": d.get("address"),
                    "status": d.get("state"),
                    "model": d.get("model") or "",
                    "name": d.get("name") or d.get("model") or d.get("address"),
                    "version": d.get("version"),
                    "is_device_owner": bool(d.get("is_device_owner")),
                    "serial": d.get("serial"),
                    "resolved_ip": d.get("resolved_ip"),
                }
            )
        body = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def handle_adb_pair(self):
        """Pair with a device. Serialized through adb_executor."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(content_length).decode())
            result = run_in_adb_executor(adb_pair, data['ip'], data['port'], data['code'])
            self._send_json(result)
        except Exception as e:
            self.send_error(400, str(e))

    def handle_adb_connect(self):
        """Connect to a device. Serialized through adb_executor."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(content_length).decode())
            result = run_in_adb_executor(adb_connect, data['ip'], data['port'])
            if result.get('success'):
                publish_devices_now()
            self._send_json(result)
        except Exception as e:
            self.send_error(400, str(e))

    def handle_adb_push(self):
        """Push APK update to device. Serialized through adb_executor."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(content_length).decode())
            device = data.get('device') or f"{data['ip']}:{data['port']}"
            # Install can legitimately take a while on slow devices.
            result = run_in_adb_executor(adb_push_update, device, timeout=300)
            self._send_json(result)
        except Exception as e:
            self.send_error(400, str(e))

    def handle_adb_device_owner(self):
        """Set device owner. Serialized through adb_executor."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(content_length).decode())
            device = data.get('device') or f"{data['ip']}:{data['port']}"
            result = run_in_adb_executor(adb_set_device_owner, device)
            if result.get('success'):
                # Force a re-enrichment so device-owner state refreshes promptly.
                schedule_enrichment(device)
            self._send_json(result)
        except Exception as e:
            self.send_error(400, str(e))

    def handle_adb_disable_protect(self):
        """Disable Play Protect. Serialized through adb_executor."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(content_length).decode())
            device = data.get('device') or f"{data['ip']}:{data['port']}"
            result = run_in_adb_executor(adb_disable_play_protect, device)
            self._send_json(result)
        except Exception as e:
            self.send_error(400, str(e))

    def handle_set_player_name(self):
        """Set media player name via the app's HTTP API."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode()
            data = json.loads(body)
            ip = data.get('ip')
            name = data.get('name')
            if not ip or not name:
                self.send_error(400, "Missing 'ip' or 'name' parameter")
                return
            result = set_device_player_name(ip, name)
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_error(400, str(e))

    def handle_set_tablet_name(self):
        """Set tablet device name via ADB. Serialized through adb_executor."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(content_length).decode())
            device = data.get('device')
            name = data.get('name')
            if not device or not name:
                self.send_error(400, "Missing 'device' or 'name' parameter")
                return
            result = run_in_adb_executor(adb_set_tablet_name, device, name)
            self._send_json(result)
        except Exception as e:
            self.send_error(400, str(e))

    def handle_delete_device(self):
        """Delete a device from tracking. Serialized through adb_executor
        because it issues `adb disconnect` calls."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(content_length).decode())
            ip = data.get('ip')
            adb_address = data.get('adb_address')
            if not ip and not adb_address:
                self.send_error(400, "Missing 'ip' or 'adb_address' parameter")
                return
            removed = run_in_adb_executor(delete_device, ip, adb_address)
            # delete_device returns a dict, not a Result object
            if adb_address:
                registry.remove(adb_address)
            publish_devices_now()
            self._send_json({"success": True, "message": "Device removed", "removed": removed})
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
    print(f"  http://0.0.0.0:{PORT}/api/events - SSE state stream")
    print(f"  POST /log                      - Submit single log")
    print(f"  POST /logs                     - Submit multiple logs")
    print(f"  POST /checkin                  - Device check-in")
    print(f"  POST /track                    - Track played notification")
    print(f"  GET /api/logs                  - Get logs (JSON)")
    print(f"  GET /api/devices               - Get devices (JSON)")
    print()

    # Background workers. Order matters: bring up adb server before starting
    # the monitor so its first connection succeeds.
    ensure_adb_server()
    monitor = AdbMonitor(registry, on_new_device=schedule_enrichment)
    refresher = EnrichmentRefresher()
    poller = PlayerStatePoller()
    monitor.start()
    refresher.start()
    poller.start()

    def _signal_shutdown(signum, frame):
        print(f"\nReceived signal {signum}, shutting down...")
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _signal_shutdown)
    signal.signal(signal.SIGINT, _signal_shutdown)

    httpd = ThreadedHTTPServer(("0.0.0.0", PORT), UpdateHandler)
    httpd.daemon_threads = True  # don't block shutdown on slow SSE clients
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("Shutting down workers...")
        shutdown_event.set()
        try:
            monitor.stop()
        except Exception:
            pass
        try:
            httpd.server_close()
        except Exception:
            pass
        adb_executor.shutdown(wait=False, cancel_futures=True)
        http_executor.shutdown(wait=False, cancel_futures=True)
        # Threads are daemon — process exit reaps them.


if __name__ == "__main__":
    main()
