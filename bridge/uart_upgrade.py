# -*- coding: utf-8 -*-
"""UART firmware upgrade integration for serial-ssh bridge."""

import importlib.util
import logging
import os
import shlex
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

log = logging.getLogger("serial-ssh.upgrade")

# SSH exec / shell magic commands to start UART upgrade
UPGRADE_COMMANDS = frozenset(
    {
        "__uart_upgrade__",
        "uart-upgrade",
        "bridge-uart-upgrade",
    }
)

DEFAULT_REBOOT_TIMEOUT = 300.0
DEFAULT_UPGRADE_TIMEOUT = 3600.0

_UART_DEBUG_CONFIGURED = False


def configure_uart_logging(debug: bool, logdir: str) -> str:
    """
    Enable verbose UART / Kermit logging. Returns path to uart_debug.log (if debug).
    """
    global _UART_DEBUG_CONFIGURED
    os.makedirs(logdir, exist_ok=True)
    debug_log = os.path.join(logdir, "uart_debug.log")

    if not debug:
        return debug_log

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    fh = logging.FileHandler(debug_log, encoding="utf-8", mode="w")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    if not _UART_DEBUG_CONFIGURED:
        for name in ("uart-dl", "serial-ssh.upgrade", ""):
            lg = logging.getLogger(name) if name else logging.getLogger()
            lg.setLevel(logging.DEBUG)
            lg.addHandler(fh)
        _UART_DEBUG_CONFIGURED = True
        log.info("UART debug logging -> %s", debug_log)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    return debug_log


@dataclass
class DepCheckResult:
    ok: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def format_report(self) -> str:
        lines = []
        if self.ok:
            lines.append("[deps] OK — all required dependencies present")
        else:
            lines.append("[deps] FAILED — upgrade cannot start")
        for e in self.errors:
            lines.append(f"  ERROR: {e}")
        for w in self.warnings:
            lines.append(f"  WARN:  {w}")
        return "\n".join(lines) + "\r\n"


def _exe_dir() -> str:
    """Runtime directory of the executable (not PyInstaller _MEIPASS)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.abspath(os.path.dirname(__file__))


def _uart_dl_dir(uart_dl_dir: Optional[str]) -> str:
    if uart_dl_dir:
        return os.path.abspath(uart_dl_dir)
    return _exe_dir()


def ensure_uart_dl_path(uart_dl_dir: Optional[str] = None) -> str:
    root = _uart_dl_dir(uart_dl_dir)
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


def is_upgrade_command(command: str) -> bool:
    cmd = (command or "").strip()
    if cmd in UPGRADE_COMMANDS:
        return True
    # allow: uart-upgrade [--reboot-timeout 120] [--stop-after bl31]
    return cmd.split()[0] in UPGRADE_COMMANDS if cmd else False


def effective_stop_after(
    stop_after: Optional[str], skip_uboot_update: bool
) -> Optional[str]:
    """CLI --stop-after wins; --skip-uboot-update is shorthand for stop-after fip."""
    if stop_after:
        return stop_after
    if skip_uboot_update:
        return "fip"
    return None


def parse_upgrade_command(command: str) -> dict:
    """
    Parse SSH exec upgrade command options.
    Example: uart-upgrade --stop-after bl31 --reboot-timeout 120
    """
    opts: dict = {"stop_after": None, "reboot_timeout": None}
    cmd = (command or "").strip()
    if not cmd:
        return opts
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return opts
    if not tokens or tokens[0] not in UPGRADE_COMMANDS:
        return opts
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("--stop-after", "--stop-at"):
            if i + 1 < len(tokens):
                opts["stop_after"] = tokens[i + 1]
                i += 2
                continue
        if tok.startswith("--stop-after=") or tok.startswith("--stop-at="):
            opts["stop_after"] = tok.split("=", 1)[1]
            i += 1
            continue
        if tok == "--reboot-timeout" and i + 1 < len(tokens):
            opts["reboot_timeout"] = float(tokens[i + 1])
            i += 2
            continue
        if tok.startswith("--reboot-timeout="):
            opts["reboot_timeout"] = float(tok.split("=", 1)[1])
            i += 1
            continue
        i += 1
    return opts


def _bundled_data_dir() -> str:
    """PyInstaller bundled data extraction directory."""
    return getattr(sys, "_MEIPASS", "")


def check_uart_upgrade_deps(
    uart_dl_dir: Optional[str] = None,
    xmlfile: Optional[str] = None,
) -> DepCheckResult:
    """Verify Python modules and firmware files required for UART upgrade."""
    result = DepCheckResult(ok=True)
    root = _uart_dl_dir(uart_dl_dir)
    frozen = getattr(sys, "frozen", False)

    # Python packages (frozen exe already bundles them)
    if not frozen:
        for mod_name, pip_hint in (
            ("serial", "pyserial"),
            ("paramiko", "paramiko"),
        ):
            spec = importlib.util.find_spec(mod_name)
            if spec is None:
                result.ok = False
                result.errors.append(
                    f"Python module '{mod_name}' missing (pip install {pip_hint})"
                )

    # ek18 Cython extension
    if not frozen:
        ek18_so = False
        if os.path.isdir(root):
            ek18_so = any(
                name.startswith("ek18")
                and (name.endswith((".so", ".pyd")) or ".cp" in name)
                for name in os.listdir(root)
            )
        if not ek18_so:
            try:
                ensure_uart_dl_path(root)
                importlib.import_module("ek18")
                ek18_so = True
            except ImportError:
                ek18_so = False
        if not ek18_so:
            result.ok = False
            result.errors.append(
                f"ek18 extension not built in {root} — run: cd bridge && python make.py build_ext"
            )

    # fip.bin — always required on filesystem (user-provided)
    fip = os.path.join(root, "fip.bin")
    if not os.path.isfile(fip):
        result.ok = False
        result.errors.append(f"fip.bin not found: {fip}")
    else:
        size = os.path.getsize(fip)
        if size < 0x1000:
            result.warnings.append(f"fip.bin unusually small ({size} bytes)")

    # uart_dl_core — already bundled in frozen mode
    if not frozen:
        core = os.path.join(root, "uart_dl_core.py")
        if not os.path.isfile(core):
            result.ok = False
            result.errors.append(f"uart_dl_core.py not found: {core}")

    return result


class BridgeSerialAdapter:
    """pyserial-like wrapper over SerialManager for UartDownload."""

    def __init__(self, serial_mgr, timeout=0.5, debug=False):
        self._mgr = serial_mgr
        self.timeout = timeout
        self.debug = debug
        self._rx_pending = bytearray()
        self._baudrate = serial_mgr.ser.baudrate if serial_mgr.ser else 115200
        if serial_mgr.ser:
            serial_mgr.ser.timeout = min(timeout, 0.1)
            if hasattr(serial_mgr.ser, "set_buffer_size"):
                try:
                    serial_mgr.ser.set_buffer_size(rx_size=65536, tx_size=65536)
                except Exception:
                    log.debug("set_buffer_size not supported on this port")

    @property
    def baudrate(self):
        return self._baudrate

    @baudrate.setter
    def baudrate(self, value):
        self._baudrate = value
        if self._mgr.ser:
            with self._mgr.lock:
                self._mgr.ser.baudrate = value

    def read(self, size):
        """Read with RX cache — bulk-drain FIFO so 1.5M Kermit does not lose bytes."""
        if not self._mgr.ser:
            time.sleep(self.timeout)
            return b""

        deadline = time.time() + self.timeout
        out = bytearray()

        while len(out) < size:
            if self._rx_pending:
                take = min(size - len(out), len(self._rx_pending))
                out += self._rx_pending[:take]
                del self._rx_pending[:take]
            if len(out) >= size:
                break
            if time.time() > deadline:
                break

            with self._mgr.lock:
                waiting = self._mgr.ser.in_waiting or 0
                if waiting > 0:
                    to_read = min(max(waiting, size - len(out)), 65536)
                    chunk = self._mgr.ser.read(to_read)
                else:
                    chunk = b""

            if chunk:
                need = size - len(out)
                if len(chunk) <= need:
                    out += chunk
                else:
                    out += chunk[:need]
                    self._rx_pending += chunk[need:]
            else:
                time.sleep(0.001)

        result = bytes(out)
        if result:
            if self.debug and (len(result) > 1 or size > 1):
                log.debug(
                    "adapter read(%d) -> %d bytes (pending=%d): %s",
                    size,
                    len(result),
                    len(self._rx_pending),
                    result[:64].hex() if len(result) > 64 else result.hex(),
                )
            self._mgr._broadcast(result)
        elif self.debug and size > 0:
            log.debug("adapter read(%d) -> timeout (0 bytes)", size)
        return result

    def write(self, data):
        if not self._mgr.ser:
            return None
        with self._mgr.lock:
            n = self._mgr.ser.write(data)
        if self.debug:
            chunk = data if isinstance(data, (bytes, bytearray)) else bytes(data)
            log.debug(
                "adapter write %d/%d bytes: %s",
                n if n is not None else len(chunk),
                len(chunk),
                chunk[:64].hex() if len(chunk) > 64 else chunk.hex(),
            )
        return n

    def flush(self):
        if self._mgr.ser:
            with self._mgr.lock:
                self._mgr.ser.flush()

    def reset_input_buffer(self):
        self._rx_pending.clear()
        if self._mgr.ser:
            with self._mgr.lock:
                self._mgr.ser.reset_input_buffer()

    @property
    def in_waiting(self):
        if not self._mgr.ser:
            return 0
        with self._mgr.lock:
            return self._mgr.ser.in_waiting

    def set_read_timeout(self, timeout):
        """Match pyserial timeout with adapter read deadline (used during Kermit)."""
        self.timeout = timeout
        if self._mgr.ser:
            with self._mgr.lock:
                self._mgr.ser.timeout = min(timeout, 0.1)


class UartUpgradeRunner:
    def __init__(
        self,
        serial_mgr,
        uart_dl_dir: Optional[str] = None,
        reboot_timeout: float = DEFAULT_REBOOT_TIMEOUT,
        uboot_baudrate: Optional[int] = None,
        skip_uboot_update: bool = False,
        stop_after: Optional[str] = None,
        retries: int = 3,
        retry_delay: float = 2.0,
        debug: bool = False,
        debug_ssh: bool = False,
    ):
        self.serial_mgr = serial_mgr
        self.uart_dl_dir = _uart_dl_dir(uart_dl_dir)
        self.reboot_timeout = reboot_timeout
        self.uboot_baudrate = uboot_baudrate
        self.skip_uboot_update = skip_uboot_update
        self.stop_after = effective_stop_after(stop_after, skip_uboot_update)
        self.retries = max(1, int(retries))
        self.retry_delay = float(retry_delay)
        self.debug = debug
        self.debug_ssh = debug_ssh
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._running = False
        self._last_error: Optional[str] = None
        self._last_ok: Optional[bool] = None

    @property
    def is_running(self) -> bool:
        return self._running

    def start(
        self,
        on_done: Optional[Callable[[bool, str], None]] = None,
        stop_after: Optional[str] = None,
        reboot_timeout: Optional[float] = None,
    ) -> tuple:
        """
        Start upgrade in background. Returns (started: bool, message: str).
        Per-invocation stop_after / reboot_timeout override runner defaults.
        """
        with self._lock:
            if self._running:
                return False, "UART upgrade already in progress\r\n"

            deps = check_uart_upgrade_deps(self.uart_dl_dir, None)
            if not deps.ok:
                return False, deps.format_report()

            if not self.serial_mgr or not self.serial_mgr.ser:
                return False, "Serial port not available — cannot run UART upgrade\r\n"

            run_stop = stop_after if stop_after is not None else self.stop_after
            run_reboot = (
                reboot_timeout if reboot_timeout is not None else self.reboot_timeout
            )

            self._running = True
            self._last_error = None
            self._last_ok = None

            def _target():
                ok, msg = self._run_upgrade(
                    stop_after=run_stop, reboot_timeout=run_reboot
                )
                self._last_ok = ok
                self._last_error = None if ok else msg
                self._running = False
                if on_done:
                    on_done(ok, msg)

            self._thread = threading.Thread(
                target=_target, name="uart-upgrade", daemon=True
            )
            self._thread.start()
            report = deps.format_report()
            stop_note = ""
            if run_stop:
                stop_note = f"[upgrade] stop-after: {run_stop}\r\n"
            return True, (
                report
                + stop_note
                + "\r\n[upgrade] Started — waiting for device reboot (URPL)...\r\n"
                + "Reboot the board into UART download mode. Progress streams to all SSH shells.\r\n"
            )

    def _run_upgrade(
        self,
        stop_after: Optional[str] = None,
        reboot_timeout: Optional[float] = None,
    ) -> tuple:
        ensure_uart_dl_path(self.uart_dl_dir)
        from uart_dl_core import UartDownload, format_stop_after_help, normalize_stop_after

        reboot_timeout = reboot_timeout if reboot_timeout is not None else self.reboot_timeout
        stop_after = stop_after if stop_after is not None else self.stop_after

        def on_status(msg: str):
            line = f"[upgrade] {msg}\r\n"
            self.serial_mgr.broadcast_text(line)

        logdir = os.path.join(self.uart_dl_dir, "log")
        os.makedirs(logdir, exist_ok=True)
        on_status(f"Log dir: {logdir}")
        if self.debug:
            debug_log = configure_uart_logging(True, logdir)
            on_status(f"Debug log: {debug_log}")
            if self.debug_ssh:
                on_status("Debug lines will also appear on SSH shells")

        self.serial_mgr.begin_upgrade()
        try:
            try:
                resolved_stop = normalize_stop_after(stop_after)
            except ValueError as e:
                on_status(f"ERROR: {e}")
                on_status(format_stop_after_help())
                return False, f"UART upgrade aborted: {e}\r\n"

            last_err: Optional[Exception] = None
            for attempt in range(1, self.retries + 1):
                try:
                    on_status(f"Upgrade attempt {attempt}/{self.retries}")
                    if resolved_stop:
                        on_status(f"Will stop after: {resolved_stop}")
                    adapter = BridgeSerialAdapter(self.serial_mgr, debug=self.debug)
                    dl = UartDownload(
                        adapter,
                        work_dir=self.uart_dl_dir,
                        logdir=logdir,
                        skip_miniterm=True,
                        on_status=on_status,
                        uboot_baudrate=self.uboot_baudrate,
                        skip_uboot_update=self.skip_uboot_update,
                        stop_after=stop_after,
                        debug=self.debug,
                        debug_ssh=self.debug_ssh,
                    )
                    dl.start(reboot_timeout=reboot_timeout)
                    if resolved_stop:
                        on_status("Stopped at configured stage; serial released.")
                        return True, "UART upgrade stopped at configured stage; serial released\r\n"
                    on_status("All stages completed.")
                    return True, "UART upgrade completed successfully\r\n"
                except Exception as e:
                    last_err = e
                    log.exception("UART upgrade failed (attempt %d/%d)", attempt, self.retries)
                    on_status(f"ERROR: {type(e).__name__}: {e}")
                    if attempt < self.retries:
                        on_status(f"Retrying in {self.retry_delay:.1f}s ...")
                        time.sleep(max(0.2, self.retry_delay))
                        continue
                    break
            msg = f"UART upgrade failed after {self.retries} attempts: {last_err}\r\n"
            return False, msg
        finally:
            self.serial_mgr.end_upgrade()
