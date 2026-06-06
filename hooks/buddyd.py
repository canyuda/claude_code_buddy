#!/usr/bin/env python3
"""
buddyd.py — BLE daemon for claude-code-buddy.

Maintains a persistent BLE connection to the ESP32 desk pet,
listens on a Unix domain socket for commands from Claude Code hooks,
and forwards JSON state updates / permission prompts over Nordic UART Service.

Usage:
    python3 buddyd.py [--socket PATH] [--log PATH] [--daemon]
"""

import asyncio
import json
import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path
from typing import Optional

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print("Error: bleak is required. Install with: pip install bleak", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constants — Nordic UART Service
# ---------------------------------------------------------------------------
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # host → device
NUS_TX_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # device → host

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_SOCKET = os.path.expanduser("~/.claude/buddy.sock")
DEFAULT_LOG = os.path.expanduser("~/.claude/buddyd.log")
HEARTBEAT_INTERVAL = 10  # seconds
RECONNECT_DELAY = 3      # seconds
DEVICE_NAME_PREFIX = "Claude-"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(log_path: str) -> logging.Logger:
    logger = logging.getLogger("buddyd")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Also log warnings+ to stderr for foreground mode
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.WARNING)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger

log: logging.Logger  # set in main()

# ---------------------------------------------------------------------------
# BLE line buffer — NUS TX notifications are chunked; we reassemble
# into newline-delimited JSON, exactly like the firmware's _LineBuf.
# ---------------------------------------------------------------------------

class BleLineBuffer:
    """Accumulates bytes from BLE notifications into newline-delimited lines."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[str]:
        """Feed raw bytes, return complete lines (stripped)."""
        self.buf.extend(data)
        lines: list[str] = []
        while b"\n" in self.buf:
            idx = self.buf.index(b"\n")
            line = bytes(self.buf[:idx]).decode("utf-8", errors="replace").strip()
            self.buf = self.buf[idx + 1:]
            if line:
                lines.append(line)
        return lines


# ---------------------------------------------------------------------------
# Pending permission tracker
# ---------------------------------------------------------------------------

class PendingPermission:
    """A permission prompt awaiting a button-press response from the ESP32."""

    def __init__(self, prompt_id: str, timeout: float = 25.0) -> None:
        self.prompt_id = prompt_id
        self.timeout = timeout
        self.event: asyncio.Event = asyncio.Event()
        self.decision: Optional[str] = None  # "once", "always", "deny"

    def resolve(self, decision: str) -> None:
        self.decision = decision
        self.event.set()


# ---------------------------------------------------------------------------
# Daemon core
# ---------------------------------------------------------------------------

class BuddyDaemon:
    def __init__(self, sock_path: str) -> None:
        self.sock_path = sock_path
        self.ble_client: Optional[BleakClient] = None
        self.ble_connected = False
        self.line_buf = BleLineBuffer()
        self.pending: dict[str, PendingPermission] = {}  # id → PendingPermission
        self._stop_event = asyncio.Event()
        self._write_lock = asyncio.Lock()
        self._last_heartbeat = 0.0

    # ------------------------------------------------------------------
    # BLE: scan & connect
    # ------------------------------------------------------------------

    async def _scan_for_device(self) -> Optional[str]:
        """Scan for a BLE device named Claude-XXXX, return its address."""
        log.info("Scanning for BLE device with prefix '%s'...", DEVICE_NAME_PREFIX)
        try:
            devices = await BleakScanner.scan(timeout=5.0)
            for d in devices:
                name = d.name or ""
                if name.startswith(DEVICE_NAME_PREFIX):
                    log.info("Found device: %s (%s)", name, d.address)
                    return d.address
            log.warning("No Claude-* device found in scan")
        except Exception as e:
            log.error("BLE scan failed: %s", e)
        return None

    def _notification_handler(self, _char, data: bytearray) -> None:
        """Handle incoming NUS TX notifications — parse into JSON lines."""
        lines = self.line_buf.feed(bytes(data))
        for line in lines:
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                log.warning("Invalid JSON from ESP32: %s", line[:80])
                continue

            cmd = obj.get("cmd")
            if cmd == "permission":
                pid = obj.get("id", "")
                decision = obj.get("decision", "")
                log.info("Permission response: id=%s decision=%s", pid, decision)
                pending = self.pending.get(pid)
                if pending:
                    pending.resolve(decision)
                else:
                    log.warning("Unknown permission id: %s", pid)
            elif cmd == "ack":
                # Acknowledgement from file transfer or other commands
                log.debug("ESP32 ack: %s", json.dumps(obj, separators=(",", ":")))
            else:
                log.debug("ESP32 msg: %s", json.dumps(obj, separators=(",", ":")))

    async def _connect_ble(self) -> bool:
        """Scan, connect, and subscribe to NUS TX. Returns True on success."""
        addr = await self._scan_for_device()
        if not addr:
            return False

        try:
            self.ble_client = BleakClient(
                addr,
                disconnected_callback=self._on_ble_disconnect,
            )
            await self.ble_client.connect()
            await self.ble_client.start_notify(
                NUS_TX_CHAR_UUID, self._notification_handler
            )
            self.ble_connected = True
            log.info("BLE connected to %s", addr)

            # Send time sync on connect
            await self._send_time_sync()

            return True
        except Exception as e:
            log.error("BLE connect failed: %s", e)
            self.ble_connected = False
            self.ble_client = None
            return False

    def _on_ble_disconnect(self, client: BleakClient) -> None:
        """Called when BLE disconnects unexpectedly."""
        log.warning("BLE disconnected from %s", client.address)
        self.ble_connected = False

    # ------------------------------------------------------------------
    # BLE: write helpers
    # ------------------------------------------------------------------

    async def _ble_write(self, payload: str) -> bool:
        """Write a JSON line to NUS RX. Returns True on success."""
        if not self.ble_connected or not self.ble_client:
            return False
        async with self._write_lock:
            try:
                data = (payload + "\n").encode("utf-8")
                # NUS RX has a 20-byte MTU cap on some stacks; chunk if needed
                CHUNK = 20
                for i in range(0, len(data), CHUNK):
                    await self.ble_client.write_gatt_char(
                        NUS_RX_CHAR_UUID, data[i:i + CHUNK], response=False
                    )
                    if i + CHUNK < len(data):
                        await asyncio.sleep(0.01)  # small delay between chunks
                return True
            except Exception as e:
                log.error("BLE write failed: %s", e)
                self.ble_connected = False
                return False

    async def _send_time_sync(self) -> None:
        """Send current time to ESP32 for RTC sync."""
        import struct
        now = int(time.time())
        # Local timezone offset in seconds
        if time.daylight and time.localtime().tm_isdst:
            tz_offset = -time.altzone
        else:
            tz_offset = -time.timezone
        payload = json.dumps({"time": [now, tz_offset]})
        await self._ble_write(payload)

    async def _heartbeat_loop(self) -> None:
        """Periodically send heartbeat to keep ESP32 dataConnected() alive."""
        while not self._stop_event.is_set():
            if self.ble_connected:
                now = time.time()
                if now - self._last_heartbeat >= HEARTBEAT_INTERVAL:
                    self._last_heartbeat = now
                    # Minimal heartbeat — just the time sync is enough
                    # ESP32 uses _lastLiveMs to detect connection
                    await self._send_time_sync()
                    log.debug("Heartbeat sent")
            await asyncio.sleep(1)

    # ------------------------------------------------------------------
    # BLE: reconnect loop
    # ------------------------------------------------------------------

    async def _reconnect_loop(self) -> None:
        """Monitor BLE connection and reconnect on failure."""
        while not self._stop_event.is_set():
            if not self.ble_connected:
                log.info("Attempting BLE reconnect in %ds...", RECONNECT_DELAY)
                await asyncio.sleep(RECONNECT_DELAY)
                if self._stop_event.is_set():
                    break
                ok = await self._connect_ble()
                if ok:
                    # Fail all pending permissions — they're stale after reconnect
                    for p in self.pending.values():
                        p.resolve("timeout")
                    self.pending.clear()
            else:
                await asyncio.sleep(1)

    # ------------------------------------------------------------------
    # Unix socket server
    # ------------------------------------------------------------------

    async def _handle_socket_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle a single Unix socket client (a hook script)."""
        try:
            data = await asyncio.wait_for(reader.read(4096), timeout=30.0)
            if not data:
                return

            try:
                cmd = json.loads(data.decode("utf-8"))
            except json.JSONDecodeError:
                log.warning("Invalid JSON from socket client")
                return

            action = cmd.get("action", "")

            if action == "state":
                # Forward state update to ESP32 via BLE
                state_data = cmd.get("data", {})
                payload = json.dumps(state_data, separators=(",", ":"))
                ok = await self._ble_write(payload)
                response = json.dumps({"ok": ok})
                writer.write(response.encode("utf-8"))
                await writer.drain()

            elif action == "prompt":
                # Send permission prompt to ESP32, wait for button response
                prompt_id = cmd.get("id", "")
                tool = cmd.get("tool", "")
                hint = cmd.get("hint", "")
                timeout_s = min(cmd.get("timeout", 25), 30)

                prompt_payload = json.dumps({
                    "total": 1,
                    "running": 1,
                    "waiting": 1,
                    "msg": f"approve: {tool}"[:24],
                    "prompt": {
                        "id": prompt_id,
                        "tool": tool[:20],
                        "hint": hint[:44],
                    },
                }, separators=(",", ":"))

                ok = await self._ble_write(prompt_payload)
                if not ok:
                    response = json.dumps({"decision": "ask"})
                    writer.write(response.encode("utf-8"))
                    await writer.drain()
                    return

                # Wait for button response
                pending = PendingPermission(prompt_id, timeout=timeout_s)
                self.pending[prompt_id] = pending
                try:
                    await asyncio.wait_for(pending.event.wait(), timeout=timeout_s)
                    decision = pending.decision or "ask"
                except asyncio.TimeoutError:
                    decision = "ask"
                    log.info("Permission prompt timed out: %s", prompt_id)
                finally:
                    self.pending.pop(prompt_id, None)

                # Clear the prompt on ESP32
                clear_payload = json.dumps(
                    {"total": 1, "running": 0, "waiting": 0, "prompt": {}},
                    separators=(",", ":"),
                )
                await self._ble_write(clear_payload)

                response = json.dumps({"decision": decision})
                writer.write(response.encode("utf-8"))
                await writer.drain()

            elif action == "ping":
                writer.write(json.dumps({"ok": True, "ble": self.ble_connected}).encode("utf-8"))
                await writer.drain()

            else:
                log.warning("Unknown socket action: %s", action)
                writer.write(json.dumps({"error": f"unknown action: {action}"}).encode("utf-8"))
                await writer.drain()

        except asyncio.TimeoutError:
            log.warning("Socket client timed out")
        except Exception as e:
            log.error("Socket handler error: %s", e)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _start_socket_server(self) -> asyncio.AbstractServer:
        """Start the Unix domain socket server."""
        # Remove stale socket
        if os.path.exists(self.sock_path):
            os.unlink(self.sock_path)

        server = await asyncio.start_unix_server(
            self._handle_socket_client,
            path=self.sock_path,
        )
        # Make socket accessible to user only
        os.chmod(self.sock_path, 0o600)
        log.info("Listening on %s", self.sock_path)
        return server

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main daemon loop: start BLE, socket, heartbeat, reconnect."""
        socket_server: Optional[asyncio.AbstractServer] = None

        try:
            # Initial BLE connection (non-fatal if it fails)
            await self._connect_ble()

            # Start Unix socket server
            socket_server = await self._start_socket_server()

            # Run concurrent tasks
            await asyncio.gather(
                self._heartbeat_loop(),
                self._reconnect_loop(),
                self._wait_for_stop(),
            )
        except Exception as e:
            log.error("Daemon error: %s", e)
        finally:
            self._stop_event.set()
            if socket_server:
                socket_server.close()
                await socket_server.wait_closed()
            if os.path.exists(self.sock_path):
                os.unlink(self.sock_path)
            if self.ble_client and self.ble_connected:
                try:
                    await self.ble_client.disconnect()
                except Exception:
                    pass
            log.info("Daemon stopped")

    async def _wait_for_stop(self) -> None:
        """Block until stop signal."""
        await self._stop_event.wait()

    def stop(self) -> None:
        """Signal the daemon to stop."""
        self._stop_event.set()


# ---------------------------------------------------------------------------
# PID file management
# ---------------------------------------------------------------------------

def pid_path(sock_path: str) -> str:
    return sock_path + ".pid"

def write_pid(sock_path: str) -> None:
    pid_file = pid_path(sock_path)
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))

def remove_pid(sock_path: str) -> None:
    pid_file = pid_path(sock_path)
    if os.path.exists(pid_file):
        os.unlink(pid_file)

def read_pid(sock_path: str) -> Optional[int]:
    pid_file = pid_path(sock_path)
    if not os.path.exists(pid_file):
        return None
    try:
        with open(pid_file) as f:
            return int(f.read().strip())
    except (ValueError, OSError):
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    import argparse
    p = argparse.ArgumentParser(description="BLE daemon for claude-code-buddy")
    p.add_argument("--socket", default=DEFAULT_SOCKET, help="Unix socket path")
    p.add_argument("--log", default=DEFAULT_LOG, help="Log file path")
    p.add_argument("--foreground", action="store_true", help="Run in foreground (no daemon)")
    return p.parse_args()


def main() -> int:
    global log

    args = parse_args()
    log = setup_logging(args.log)
    log.info("buddyd starting (socket=%s)", args.socket)

    daemon = BuddyDaemon(args.socket)

    # Handle signals for graceful shutdown
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, daemon.stop)

    if not args.foreground:
        # Daemonize: fork, setsid, close stdio
        pid = os.fork()
        if pid > 0:
            # Parent: wait briefly for child to start, then exit
            time.sleep(0.5)
            child_pid = read_pid(args.socket)
            if child_pid:
                print(f"buddyd started (pid {child_pid})")
            else:
                print("buddyd may have failed to start; check log", file=sys.stderr)
                return 1
            return 0
        # Child
        os.setsid()
        # Redirect stdio to /dev/null
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.close(devnull)

    write_pid(args.socket)

    try:
        loop.run_until_complete(daemon.run())
    except KeyboardInterrupt:
        daemon.stop()
        loop.run_until_complete(daemon.run())
    finally:
        remove_pid(args.socket)
        loop.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
