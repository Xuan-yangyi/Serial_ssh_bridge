# -*- coding: utf-8 -*-
import argparse
import logging
import os
import queue
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import serial
import paramiko

from uart_upgrade import (
    UartUpgradeRunner,
    check_uart_upgrade_deps,
    is_upgrade_command,
)

# ──────────────────────── Logging ────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("serial-ssh")


def _exe_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _scp_send_ok(chan) -> None:
    chan.send(b"\x00")


def _scp_send_err(chan, msg: str) -> None:
    data = (msg or "scp error").encode("utf-8", errors="replace")
    if not data.endswith(b"\n"):
        data += b"\n"
    chan.send(b"\x01" + data)


def _scp_recv_exact(chan, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = chan.recv(n - len(buf))
        if not chunk:
            raise EOFError("scp: unexpected EOF")
        buf += chunk
    return bytes(buf)


def _scp_recv_line(chan, max_len: int = 8192) -> bytes:
    buf = bytearray()
    while True:
        b = chan.recv(1)
        if not b:
            raise EOFError("scp: EOF waiting for line")
        buf += b
        if b == b"\n":
            return bytes(buf)
        if len(buf) >= max_len:
            raise ValueError("scp: line too long")


def _handle_scp_sink(chan, cmd: str) -> int:
    """
    Minimal SCP receiver (sink): handles 'scp -t <path>'.
    Saves uploaded files into exe directory (same-level as serial_ssh_bridge.exe).
    """
    # Client expects an initial OK before it starts sending control records.
    _scp_send_ok(chan)

    base_dir = _exe_dir()
    total_files = 0
    total_bytes = 0

    while True:
        try:
            line = _scp_recv_line(chan)
        except EOFError:
            break

        if not line:
            break

        rec = line.decode("utf-8", errors="replace").rstrip("\n")
        if rec == "E":
            _scp_send_ok(chan)
            continue
        if not rec:
            continue

        # Expect: C<mode> <size> <name>
        if not rec.startswith("C"):
            _scp_send_err(chan, f"unsupported scp record: {rec[:64]}")
            return 1

        try:
            _mode = rec[1:5]
            rest = rec[6:] if len(rec) > 6 else ""
            size_str, name = rest.split(" ", 1)
            size = int(size_str, 10)
        except Exception:
            _scp_send_err(chan, f"bad scp record: {rec[:128]}")
            return 1

        # Security: always write to base_dir, strip any path components.
        name = os.path.basename(name.strip())
        if not name or name in (".", ".."):
            _scp_send_err(chan, "bad filename")
            return 1

        dst = os.path.join(base_dir, name)
        try:
            _scp_send_ok(chan)  # ack header
            with open(dst, "wb") as fp:
                remaining = size
                while remaining > 0:
                    chunk = chan.recv(min(65536, remaining))
                    if not chunk:
                        raise EOFError("scp: EOF during file data")
                    fp.write(chunk)
                    remaining -= len(chunk)
                    total_bytes += len(chunk)
            # After file data, client sends a single 0 byte.
            _ = _scp_recv_exact(chan, 1)
            _scp_send_ok(chan)
            total_files += 1
            log.info("SCP received: %s (%d bytes) -> %s", name, size, dst)
        except Exception as e:
            _scp_send_err(chan, f"write failed: {e}")
            return 1

    if total_files:
        log.info("SCP done: files=%d bytes=%d dir=%s", total_files, total_bytes, base_dir)
    return 0

# ──────────────────────── Serial port manager ────────────────────────
class SerialManager:
    """Thread-safe serial I/O; exec commands queued; shell clients use broadcast."""

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        max_shell: int = 64,
        exec_queue_size: int = 2000,
        exec_timeout: float = 2.0,
    ):
        self.ser = None
        self.lock = threading.Lock()
        self._shell_clients = set()
        self._clients_lock = threading.Lock()
        self._running = True
        self.max_shell = max_shell
        self.exec_timeout = exec_timeout
        self._exec_queue = queue.Queue(maxsize=exec_queue_size)
        self._stats_lock = threading.Lock()
        self._total_accepted = 0
        self._total_exec_done = 0
        self._total_exec_rejected = 0
        self._upgrade_active = threading.Event()
        self.upgrade_runner = None

        try:
            self.ser = serial.Serial(
                port=port,
                baudrate=baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.05,
            )
            log.info(f"Serial port opened: {port} @ {baudrate}")
        except Exception as e:
            log.warning(f"Cannot open serial port {port}: {e}")
            log.warning("Running without serial (SSH features still available)")

        self._exec_worker = threading.Thread(
            target=self._exec_worker_loop, name="serial-exec", daemon=True
        )
        self._exec_worker.start()

    def bump_accepted(self):
        with self._stats_lock:
            self._total_accepted += 1
            n = self._total_accepted
        if n % 100 == 0:
            log.info(
                f"Total connections {n}, shells={self.shell_count()}, "
                f"exec_done={self._total_exec_done}, exec_rejected={self._total_exec_rejected}"
            )

    def shell_count(self):
        with self._clients_lock:
            return len(self._shell_clients)

    def register_shell(self, client):
        with self._clients_lock:
            if len(self._shell_clients) >= self.max_shell:
                return False
            self._shell_clients.add(client)
            n = len(self._shell_clients)
        if n <= 10 or n % 50 == 0:
            log.info(f"[+] shells online: {n}/{self.max_shell}")
        return True

    def unregister_shell(self, client):
        removed = False
        with self._clients_lock:
            if client in self._shell_clients:
                self._shell_clients.discard(client)
                removed = True
                n = len(self._shell_clients)
        if removed and (n <= 10 or n % 50 == 0):
            log.info(f"[-] shells online: {n}/{self.max_shell}")

    def read_loop(self):
        """Read serial port continuously and broadcast to all shell clients."""
        if not self.ser:
            return

        buf = b""
        while self._running:
            if self._upgrade_active.is_set():
                time.sleep(0.05)
                continue
            try:
                with self.lock:
                    if self.ser.in_waiting:
                        buf += self.ser.read(self.ser.in_waiting)
                if buf:
                    time.sleep(0.02)
                    with self.lock:
                        if self.ser.in_waiting:
                            buf += self.ser.read(self.ser.in_waiting)
                    self._broadcast(buf)
                    buf = b""
                else:
                    time.sleep(0.01)
            except Exception:
                break

    def write(self, data):
        if self._upgrade_active.is_set():
            log.debug("upgrade active — shell write ignored")
            return
        if self.ser:
            try:
                with self.lock:
                    self.ser.write(data)
            except Exception as e:
                # Serial glitches should not tear down SSH shells.
                log.error(f"Serial write failed (ignored): {e}")
                # Best-effort: if the port is gone, close it to stop repeated failures.
                try:
                    with self.lock:
                        self.ser.close()
                except Exception:
                    pass
                self.ser = None
        else:
            log.debug(f"[serial disabled] write attempted: {data}")

    def begin_upgrade(self):
        self._upgrade_active.set()
        log.info("UART upgrade started — passthrough writes paused")

    def end_upgrade(self):
        self._upgrade_active.clear()
        log.info("UART upgrade ended — passthrough resumed")

    def upgrade_write(self, data):
        if self.ser:
            with self.lock:
                self.ser.write(data)

    def broadcast_text(self, text: str):
        if isinstance(text, str):
            text = text.encode("utf-8", errors="replace")
        self._broadcast(text)

    def _broadcast(self, data):
        with self._clients_lock:
            targets = [c for c in self._shell_clients if c._alive and c.channel.active]
        dead = []
        for c in targets:
            try:
                c.send(data)
            except Exception:
                dead.append(c)
        for c in dead:
            self.unregister_shell(c)

    def submit_exec(self, channel, command):
        """Enqueue exec command; returns done Event, or None if queue is full."""
        done = threading.Event()
        try:
            self._exec_queue.put_nowait((channel, command, done))
            return done
        except queue.Full:
            with self._stats_lock:
                self._total_exec_rejected += 1
            log.warning(
                f"exec queue full (max={self._exec_queue.maxsize}), command rejected"
            )
            return None

    def exec_wait_timeout(self):
        """Max wait time for one exec (including queue time)."""
        q = self._exec_queue.qsize()
        return self.exec_timeout * (q + 3) + 30

    def _exec_worker_loop(self):
        while self._running:
            try:
                channel, command, done = self._exec_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._run_one_exec(channel, command)
            finally:
                done.set()
                self._exec_queue.task_done()

    def _run_one_exec(self, channel, command):
        if not channel.active:
            raise ConnectionError("channel closed")
        status = 0
        try:
            if isinstance(command, bytes):
                command = command.decode("utf-8", errors="replace")
            log.debug(f"exec run: {command[:120]}")
            self.write((command + "\r\n").encode("utf-8"))

            response = b""
            start = time.time()
            while time.time() - start < self.exec_timeout:
                if self.ser:
                    with self.lock:
                        if self.ser.in_waiting:
                            response += self.ser.read(self.ser.in_waiting)
                time.sleep(0.01)

            if not self.ser:
                response = f"[simulated] command received: {command}\r\n".encode("utf-8")

            if channel.active:
                channel.send(response)
            else:
                raise ConnectionError("channel closed")
        except Exception as e:
            log.error(f"exec error: {e}")
            status = 1
        finally:
            if channel.active:
                try:
                    channel.send_exit_status(status)
                except Exception:
                    pass
            with self._stats_lock:
                self._total_exec_done += 1

    def close(self):
        self._running = False
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass

# ──────────────────────── SSH client session ────────────────────────
class SerialSSHClient:
    def __init__(self, channel, serial_mgr):
        self.channel = channel
        self.serial_mgr = serial_mgr
        self._alive = True

    def send(self, data):
        if self._alive and self.channel.active:
            try:
                self.channel.send(data)
            except Exception:
                self._alive = False

    def recv_loop(self, echo=False):
        """Forward SSH channel bytes to UART until channel closes."""
        log.debug("Shell receive loop started (echo=%s)", echo)
        try:
            self.channel.settimeout(1.0)
            while self._alive and self.channel.active:
                try:
                    data = self.channel.recv(4096)
                    if not data:
                        break
                    if echo:
                        try:
                            self.channel.send(data)
                        except Exception:
                            break
                    try:
                        self.serial_mgr.write(data)
                    except Exception as e:
                        log.error(f"Shell serial write failed (ignored): {e}")
                        time.sleep(0.05)
                except socket.timeout:
                    continue
                except Exception as e:
                    log.error(f"Shell receive error: {e}")
                    break
        finally:
            self._alive = False
            self.serial_mgr.unregister_shell(self)
            try:
                self.channel.close()
            except Exception:
                pass


def _chan_send(chan, text: str) -> None:
    if chan.active:
        chan.send(text.encode("utf-8", errors="replace"))


def _read_interactive_line(chan) -> str | None:
    """Read one line with local echo (PTY-friendly for MCP terminals)."""
    buf = bytearray()
    while chan.active:
        try:
            data = chan.recv(1)
        except socket.timeout:
            continue
        if not data:
            return None
        ch = data[0]
        if ch in (10, 13):
            _chan_send(chan, "\r\n")
            return buf.decode("utf-8", errors="replace").strip()
        if ch in (127, 8):
            if buf:
                buf.pop()
                chan.send(b"\x08 \x08")
            continue
        if ch == 3:
            _chan_send(chan, "^C\r\n")
            return ""
        if ch < 32 and ch not in (9,):
            continue
        buf.extend(data)
        chan.send(data)
    return None


def _bridge_welcome(serial_mgr) -> str:
    lines = [
        "\r\n=== Serial SSH Bridge (management shell) ===\r\n",
    ]
    if serial_mgr.ser:
        lines.append(f"Serial: {serial_mgr.ser.port} @ {serial_mgr.ser.baudrate}\r\n")
    else:
        lines.append("Serial: disabled (test mode)\r\n")
    lines.append(
        "Type 'help' for commands. Use 'attach' to enter raw serial passthrough.\r\n"
        "UART upgrade: run SSH exec 'uart-upgrade' from another session.\r\n\r\n"
    )
    return "".join(lines)


def _run_bridge_shell(chan, serial_mgr, client: SerialSSHClient) -> None:
    """
    Interactive bridge shell for MCP / Claude Code terminals.

    Unlike raw passthrough, this keeps a prompt-driven session alive so clients
    that expect a persistent shell do not treat the SSH session as dead.
    """
    chan.settimeout(1.0)
    _chan_send(chan, _bridge_welcome(serial_mgr))

    try:
        while client._alive and chan.active:
            _chan_send(chan, "bridge> ")
            line = _read_interactive_line(chan)
            if line is None:
                break
            cmd = line.strip().lower()
            if not cmd:
                continue
            if cmd in ("exit", "quit"):
                _chan_send(chan, "bye\r\n")
                break
            if cmd == "help":
                _chan_send(
                    chan,
                    "Commands:\r\n"
                    "  help    - show this help\r\n"
                    "  status  - bridge / serial / shell status\r\n"
                    "  attach  - enter raw serial passthrough (type ~. alone to return)\r\n"
                    "  exit    - close SSH session\r\n"
                    "Upgrade: ssh exec uart-upgrade (separate SSH session)\r\n",
                )
                continue
            if cmd == "status":
                ser = serial_mgr.ser
                if ser:
                    _chan_send(
                        chan,
                        f"serial={ser.port} baud={ser.baudrate} "
                        f"upgrade_active={serial_mgr._upgrade_active.is_set()}\r\n",
                    )
                else:
                    _chan_send(chan, "serial=disabled\r\n")
                _chan_send(chan, f"shells_online={serial_mgr.shell_count()}\r\n")
                continue
            if cmd == "attach":
                _chan_send(
                    chan,
                    "\r\n[serial attach] raw passthrough; type ~. on its own line to return\r\n",
                )
                _serial_attach_until_detach(chan, serial_mgr, client)
                continue
            _chan_send(chan, f"unknown command: {line!r} (type help)\r\n")
    finally:
        client._alive = False
        serial_mgr.unregister_shell(client)
        try:
            chan.close()
        except Exception:
            pass


def _serial_attach_until_detach(chan, serial_mgr, client: SerialSSHClient) -> None:
    """Raw passthrough with optional detach via a lone '~.' line."""
    chan.settimeout(0.2)
    tail = bytearray()
    while client._alive and chan.active:
        try:
            data = chan.recv(4096)
        except socket.timeout:
            continue
        if not data:
            break
        tail.extend(data)
        if len(tail) > 64:
            tail = tail[-64:]
        if (
            tail.endswith(b"~.\r\n")
            or tail.endswith(b"~.\n")
            or tail.endswith(b"~.\r")
        ):
            _chan_send(chan, "\r\n[serial detach]\r\n")
            return
        try:
            serial_mgr.write(data)
        except Exception as e:
            log.error(f"Shell serial write failed (ignored): {e}")

# ──────────────────────── SSH Server ────────────────────────
class SerialSSHServer(paramiko.ServerInterface):
    def __init__(self, serial_mgr, username="admin", password="123456"):
        self.serial_mgr = serial_mgr
        self.event = threading.Event()
        self.username = username
        self.password = password
        self.exec_command = None
        self.want_pty = False

    def check_auth_password(self, username, password):
        if username == self.username and password == self.password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(
        self, channel, term, width, height, pixelwidth, pixelheight, modes
    ):
        self.want_pty = True
        return True

    def check_channel_shell_request(self, channel):
        self.event.set()
        return True

    def check_channel_exec_request(self, channel, command):
        self.exec_command = command
        self.event.set()
        return True


def handle_connection(
    client_sock, addr, host_key, serial_mgr, ssh_user, ssh_pass, upgrade_runner, shell_mode
):
    transport = None
    client = None
    serial_mgr.bump_accepted()
    try:
        transport = paramiko.Transport(client_sock)
        transport.add_server_key(host_key)
        server = SerialSSHServer(serial_mgr, ssh_user, ssh_pass)
        transport.start_server(server=server)

        timeout = 30
        start_time = time.time()
        while not transport.is_active() and time.time() - start_time < timeout:
            time.sleep(0.1)

        if not transport.is_active():
            log.error(f"SSH handshake timeout: {addr}")
            return

        chan = transport.accept(timeout=30)
        if chan is None:
            return

        if not server.event.wait(timeout=10):
            log.warning(f"No shell/exec request received: {addr}")
            return

        if server.exec_command is not None:
            cmd = server.exec_command
            if isinstance(cmd, bytes):
                cmd = cmd.decode("utf-8", errors="replace")
            if is_upgrade_command(cmd):
                started, msg = upgrade_runner.start()
                try:
                    chan.send(msg.encode("utf-8", errors="replace"))
                    chan.send_exit_status(0 if started else 1)
                except Exception:
                    pass
                return
            if cmd.strip() == "__uart_deps__":
                report = check_uart_upgrade_deps(upgrade_runner.uart_dl_dir, None)
                try:
                    chan.send(report.format_report().encode("utf-8"))
                    chan.send_exit_status(0 if report.ok else 1)
                except Exception:
                    pass
                return

            # SCP upload support (sink mode): scp <local> user@host:<remote>
            # The client runs: scp -t <remote_path>
            if cmd.strip().startswith("scp ") and " -t" in (cmd + " "):
                try:
                    status = _handle_scp_sink(chan, cmd)
                    try:
                        chan.send_exit_status(status)
                    except Exception:
                        pass
                finally:
                    try:
                        chan.close()
                    except Exception:
                        pass
                return

            done = serial_mgr.submit_exec(chan, server.exec_command)
            if done is None:
                try:
                    chan.send(
                        b"bridge busy: exec queue full, retry later\r\n"
                    )
                    chan.send_exit_status(1)
                except Exception:
                    pass
            else:
                wait_sec = serial_mgr.exec_wait_timeout()
                if not done.wait(timeout=wait_sec):
                    log.warning(
                        f"exec wait timeout ({wait_sec:.0f}s), queue={serial_mgr._exec_queue.qsize()}"
                    )
            return

        client = SerialSSHClient(chan, serial_mgr)
        if not serial_mgr.register_shell(client):
            try:
                chan.send(
                    f"\r\nshell limit reached ({serial_mgr.max_shell}), retry later\r\n".encode(
                        "utf-8"
                    )
                )
            except Exception:
                pass
            return

        welcome = "\r\n=== Serial passthrough + UART upgrade bridge ===\r\n"
        if serial_mgr.ser:
            welcome += f"Serial: {serial_mgr.ser.port} @ {serial_mgr.ser.baudrate}\r\n"
        else:
            welcome += "Serial: disabled (test mode)\r\n"
        welcome += (
            "Bidirectional data path is active\r\n"
            "Start UART upgrade: SSH exec uart-upgrade (or __uart_upgrade__)\r\n"
            "Check dependencies: SSH exec __uart_deps__\r\n\r\n"
        )

        use_bridge_shell = shell_mode == "bridge" or (
            shell_mode == "auto" and server.want_pty
        )
        if use_bridge_shell:
            log.info("SSH shell mode: bridge (pty=%s)", server.want_pty)
            _run_bridge_shell(chan, serial_mgr, client)
        else:
            log.info("SSH shell mode: passthrough (pty=%s)", server.want_pty)
            chan.send(welcome.encode("utf-8"))
            client.recv_loop(echo=server.want_pty)

    except Exception as e:
        log.error(f"Client {addr} error: {e}")
    finally:
        if client is not None:
            with serial_mgr._clients_lock:
                still = client in serial_mgr._shell_clients
            if still:
                serial_mgr.unregister_shell(client)
        if transport:
            try:
                transport.close()
            except Exception:
                pass
        try:
            client_sock.close()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(
        description="Serial SSH Bridge — serial passthrough over SSH + UART firmware flash"
    )
    parser.add_argument("-p", "--port", default="/dev/ttyUSB0", help="Serial device path")
    parser.add_argument("-b", "--baudrate", type=int, default=115200, help="Baud rate")
    parser.add_argument("--ssh-port", type=int, default=2222, help="SSH listen port")
    parser.add_argument("--ssh-user", default="admin", help="SSH username")
    parser.add_argument("--ssh-pass", default="123456", help="SSH password")
    parser.add_argument("--host-key", default="host_key", help="SSH host key file")
    parser.add_argument("--bind", default="0.0.0.0", help="Bind address")
    parser.add_argument(
        "--max-workers",
        type=int,
        default=128,
        help="SSH connection worker pool size (suggest 64–256)",
    )
    parser.add_argument(
        "--listen-backlog",
        type=int,
        default=512,
        help="Socket listen backlog",
    )
    parser.add_argument(
        "--max-shell",
        type=int,
        default=64,
        help="Max concurrent shell passthrough sessions (exec not counted)",
    )
    parser.add_argument(
        "--exec-queue-size",
        type=int,
        default=2000,
        help="Pending exec command queue capacity",
    )
    parser.add_argument(
        "--exec-timeout",
        type=float,
        default=2.0,
        help="Seconds to wait for serial response per exec",
    )
    parser.add_argument(
        "--max-pending",
        type=int,
        default=0,
        help="Max concurrent handshakes; 0 means max_workers*4",
    )
    parser.add_argument(
        "--uart-dl-dir",
        default=None,
        help="Work dir with fip.bin and ek18 (default: exe/script directory)",
    )
    parser.add_argument(
        "--uart-reboot-timeout",
        type=float,
        default=300.0,
        help="Timeout (s) waiting for device URPL download mode after reboot",
    )
    parser.add_argument(
        "--uboot-baudrate",
        type=int,
        default=None,
        help="U-Boot Kermit baud rate (default 1500000; use 115200 for debug)",
    )
    parser.add_argument(
        "--skip-uboot-update",
        action="store_true",
        help="FIP slices only; skip U-Boot fip.bin phase (stay in U-Boot CLI)",
    )
    parser.add_argument(
        "--uart-debug",
        action="store_true",
        help="Verbose UART/Kermit logs under log/uart_debug.log",
    )
    parser.add_argument(
        "--uart-debug-ssh",
        action="store_true",
        help="Mirror debug lines to SSH shells (requires --uart-debug)",
    )
    parser.add_argument(
        "--shell-mode",
        choices=("passthrough", "bridge", "auto"),
        default="auto",
        help=(
            "SSH shell behavior: passthrough=raw serial immediately; "
            "bridge=management shell (MCP-friendly); "
            "auto=bridge when client requests a PTY (default)"
        ),
    )
    parser.add_argument(
        "--check-deps",
        action="store_true",
        help="Check UART upgrade dependencies and exit",
    )
    args = parser.parse_args()
    if args.max_pending <= 0:
        args.max_pending = args.max_workers * 4

    if args.uart_debug_ssh and not args.uart_debug:
        parser.error("--uart-debug-ssh requires --uart-debug")

    upgrade_runner = UartUpgradeRunner(
        serial_mgr=None,
        uart_dl_dir=args.uart_dl_dir,
        reboot_timeout=args.uart_reboot_timeout,
        uboot_baudrate=args.uboot_baudrate,
        retries=3,
        retry_delay=2.0,
        debug=args.uart_debug,
        debug_ssh=args.uart_debug_ssh,
        skip_uboot_update=args.skip_uboot_update,
    )
    if args.uart_debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("serial-ssh").setLevel(logging.DEBUG)
    if args.check_deps:
        report = check_uart_upgrade_deps(
            args.uart_dl_dir, None
        )
        print(report.format_report())
        sys.exit(0 if report.ok else 1)

    serial_mgr = SerialManager(
        args.port,
        args.baudrate,
        max_shell=args.max_shell,
        exec_queue_size=args.exec_queue_size,
        exec_timeout=args.exec_timeout,
    )
    upgrade_runner.serial_mgr = serial_mgr
    serial_mgr.upgrade_runner = upgrade_runner

    if not os.path.exists(args.host_key):
        log.info("Generating SSH host key...")
        key = paramiko.RSAKey.generate(2048)
        key.write_private_key_file(args.host_key)
    else:
        key = paramiko.RSAKey(filename=args.host_key)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.bind, args.ssh_port))
    sock.listen(args.listen_backlog)

    log.info(f"SSH Server: {args.bind}:{args.ssh_port}")
    log.info(f"SSH shell mode: {args.shell_mode}")
    log.info(f"Credentials: {args.ssh_user} / {args.ssh_pass}")
    log.info(
        f"Concurrency: workers={args.max_workers}, pending={args.max_pending}, "
        f"backlog={args.listen_backlog}, max_shell={args.max_shell}, "
        f"exec_queue={args.exec_queue_size}"
    )
    log.info(f"UART work dir: {upgrade_runner.uart_dl_dir}")
    deps = check_uart_upgrade_deps(args.uart_dl_dir, None)
    if deps.ok:
        log.info("UART upgrade dependency check passed")
    else:
        for err in deps.errors:
            log.warning(f"UART dependency: {err}")
        log.warning("UART upgrade unavailable — fix dependencies and restart bridge")

    if serial_mgr.ser:
        threading.Thread(
            target=serial_mgr.read_loop, name="serial-read", daemon=True
        ).start()

    executor = ThreadPoolExecutor(
        max_workers=args.max_workers, thread_name_prefix="ssh-conn"
    )
    pending_slots = threading.Semaphore(args.max_pending)

    def _connection_task(client_sock, addr):
        try:
            handle_connection(
                client_sock,
                addr,
                key,
                serial_mgr,
                args.ssh_user,
                args.ssh_pass,
                upgrade_runner,
                args.shell_mode,
            )
        finally:
            pending_slots.release()

    try:
        while True:
            client_sock, addr = sock.accept()
            if not pending_slots.acquire(blocking=False):
                log.warning("Handshake concurrency limit reached, connection rejected")
                try:
                    client_sock.close()
                except Exception:
                    pass
                continue
            executor.submit(_connection_task, client_sock, addr)
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        executor.shutdown(wait=False)
        serial_mgr.close()
        sock.close()
        log.info("Stopped")


if __name__ == "__main__":
    main()
