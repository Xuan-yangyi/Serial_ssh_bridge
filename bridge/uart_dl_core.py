#!/usr/bin/env python3
"""UART firmware download core (shared by CLI and SSH bridge)."""

import logging
import os
import re
import sys
import time
from array import array
from os.path import join, isfile, abspath, dirname

import ek18


def _runtime_dir() -> str:
    """Return exe directory when frozen, else script directory."""
    if getattr(sys, "frozen", False):
        return dirname(abspath(sys.executable))
    return dirname(abspath(__file__))

log = logging.getLogger("uart-dl")

# FIP slice download order and CLI aliases (BL31 -> monitor.bin in this FIP layout).
FIP_STAGE_ALIASES = (
    ("param1.bin", ("param1", "p1")),
    ("blcp.bin", ("blcp",)),
    ("bl2.bin", ("bl2",)),
    ("p2.bin", ("p2",)),
    ("monitor.bin", ("monitor", "bl31")),
    ("bl32.bin", ("bl32",)),
    ("mcu.bin", ("mcu",)),
    ("l2h.bin", ("l2h",)),
    ("l2.bin", ("l2",)),
)
STOP_AFTER_FIP_DONE = "__fip_done__"
STOP_AFTER_FULL = "__full__"


def normalize_stop_after(name: str | None) -> str | None:
    """
    Resolve user input to a stop token:
    - None / '' / full / uboot / all -> None (run complete upgrade)
    - fip -> STOP_AFTER_FIP_DONE (after last FIP slice)
    - fip.bin -> 'fip.bin' (after U-Boot whole-fip Kermit)
    - bl31, monitor, bl2, ... -> canonical slice basename e.g. monitor.bin
    """
    if not name or not str(name).strip():
        return None
    key = str(name).strip().lower().replace("-", "").replace("_", "")
    if key in ("full", "uboot", "all", "complete"):
        return None
    if key in ("fip", "fipslices", "fipdownload"):
        return STOP_AFTER_FIP_DONE
    if key in ("fipbin",):
        return "fip.bin"
    for basename, aliases in FIP_STAGE_ALIASES:
        base_key = basename.replace(".bin", "")
        if key in (base_key, basename.replace(".", "")) or key in aliases:
            return basename
    known = sorted(
        {
            *("fip", "fip.bin", "full"),
            *(a for _, aliases in FIP_STAGE_ALIASES for a in aliases),
            *(b.replace(".bin", "") for b, _ in FIP_STAGE_ALIASES),
        }
    )
    raise ValueError(
        f"Unknown stop-after target {name!r}; known: {', '.join(known)}"
    )


def format_stop_after_help() -> str:
    stages = ", ".join(f"{aliases[0]}({basename})" if aliases else basename
                       for basename, aliases in FIP_STAGE_ALIASES)
    return (
        f"FIP stages: {stages}; "
        "also: fip (all FIP slices), fip.bin (after U-Boot fip.bin), full (default)"
    )


class DevNull:
    def write(self, *_):
        pass

    def flush(self, *_):
        pass


class UartDownload:
    baudrate = 115200
    dl_baudrate = 115200
    # U-Boot phase Kermit baud rate
    uboot_baudrate = 1500000
    # Shorter inter-file delay at 1.5M; longer at 115200 for device flush
    uboot_inter_file_delay = 0.2
    uboot_inter_file_delay_115200 = 1.0
    # Kermit getc: max idle wait for next byte (device may pause while writing flash)
    kermit_byte_timeout = 10.0
    kermit_byte_timeout_uboot = 60.0
    kermit_read_timeout = 0.5
    kermit_read_timeout_uboot = 2.0

    def __init__(
        self,
        port,
        work_dir=None,
        logdir=None,
        skip_miniterm=False,
        on_status=None,
        uboot_baudrate=None,
        skip_uboot_update=False,
        stop_after=None,
        debug=False,
        debug_ssh=False,
    ):
        """
        port: pyserial.Serial or any object with read/write/flush and baudrate.
        work_dir: directory containing fip.bin and partition images (default: xml dir).
        on_status: optional callable(str) for progress (e.g. SSH broadcast).
        """
        self._port = port
        self.skip_miniterm = skip_miniterm
        self.on_status = on_status
        self.debug = debug
        self.debug_ssh = debug_ssh
        self._io_tx = 0
        self._io_rx = 0
        self._io_last_report = 0.0
        self._xfer_total = 0
        self._xfer_sent = 0
        self._current_file = ""
        self._phase = ""
        self._progress_last_pct = -1
        self.skip_uboot_update = skip_uboot_update
        self.stop_after = normalize_stop_after(stop_after)
        # Retry policy: keep the bridge process alive on transient failures.
        self.kermit_retries = 3
        self.kermit_retry_delay = 1.0
        if uboot_baudrate is not None:
            self.uboot_baudrate = uboot_baudrate
        self.work_dir = abspath(work_dir or _runtime_dir())

        fip_path = join(self.work_dir, "fip.bin")
        if not isfile(fip_path):
            raise FileNotFoundError(f"fip.bin not found in {self.work_dir}")
        with open(fip_path, "rb") as fp:
            self.fip_bin = fp.read()
        self._status(
            f"Loaded fip.bin: {fip_path} ({len(self.fip_bin)} bytes)"
        )

        self.rx_log = DevNull()
        self.tx_log = DevNull()
        self.update_filelist = []

        if logdir or debug:
            logdir = logdir or join(self.work_dir, "log")
            os.makedirs(logdir, exist_ok=True)
            self.rx_log = open(join(logdir, "uart_rx.log"), "wb", buffering=0)
            self.hex_log = open(join(logdir, "uart_hex.log"), "w", buffering=1)
            if debug:
                self.tx_log = open(join(logdir, "uart_tx.log"), "wb", buffering=0)
        else:
            self.hex_log = DevNull()

        self.parse_fip()
        self.parse_fip_file()
        self._getc_byte_timeout = self.kermit_byte_timeout

    def _status(self, msg):
        log.info(msg)
        if self.on_status:
            try:
                self.on_status(msg)
            except Exception:
                log.exception("on_status callback failed")

    def _debug(self, msg, *args, to_ssh=False):
        if args:
            log.debug(msg, *args)
            text = msg % args
        else:
            log.debug("%s", msg)
            text = str(msg)
        if self.debug and (to_ssh or self.debug_ssh):
            self._status(f"[debug] {text}")

    def _hex_log_line(self, direction, data):
        if not self.debug or isinstance(self.hex_log, DevNull):
            return
        ts = time.strftime("%H:%M:%S")
        ms = int((time.time() % 1) * 1000)
        if isinstance(data, int):
            data = bytes([data & 0xFF])
        elif not isinstance(data, (bytes, bytearray)):
            data = bytes(data)
        preview = data.hex() if len(data) <= 128 else data[:128].hex() + f"...+{len(data)-128}"
        self.hex_log.write(f"{ts}.{ms:03d} {direction} {len(data)} {preview}\n")

    def _begin_phase(self, phase, label=""):
        """Reset per-phase xfer counters so debug I/O lines are not misleading."""
        self._phase = phase
        self._current_file = label
        self._xfer_sent = 0
        self._xfer_total = 0
        self._io_last_report = 0

    def _kermit_start_with_retry(self, filename: str):
        """
        Run a single Kermit send with retry so transient UART glitches
        don't kill the whole upgrade + SSH bridge.
        """
        last = None
        for attempt in range(1, self.kermit_retries + 1):
            try:
                if attempt > 1:
                    self._status(
                        f"Retrying Kermit send: {filename} (attempt {attempt}/{self.kermit_retries})"
                    )
                    if hasattr(self._port, "reset_input_buffer"):
                        try:
                            self._port.reset_input_buffer()
                        except Exception:
                            pass
                    time.sleep(self.kermit_retry_delay)
                ek18.start(self, filename)
                return
            except Exception as e:
                last = e
                self._status(f"Kermit send failed: {filename} err={e!r}")
                # keep looping; raise after final attempt
        raise last

    def _handover_serial(self, reason: str) -> None:
        """Reset link and return UART to SSH passthrough / CLI."""
        self._port.baudrate = self.baudrate
        self._drain_rx(idle_sec=0.05, max_total=0.3)
        self._status(reason)
        self._status(
            "UART I/O released to SSH shells — use attach or passthrough for target CLI."
        )

    def _should_stop_after_fip_slice(self, filename: str) -> bool:
        if not self.stop_after or self.stop_after == STOP_AFTER_FULL:
            return False
        if self.stop_after == STOP_AFTER_FIP_DONE:
            return filename == self.fip_filelist[-1]
        return filename == self.stop_after

    def _fip_stop_label(self) -> str:
        if self.stop_after == STOP_AFTER_FIP_DONE:
            return "FIP download phase"
        return self.stop_after or ""

    def _maybe_report_io(self, force=False):
        if not self.debug:
            return
        now = time.time()
        if not force and now - self._io_last_report < 2.0:
            return
        self._io_last_report = now
        baud = getattr(self._port, "baudrate", "?")
        phase = f" phase={self._phase}" if self._phase else ""
        xfer = ""
        if self._xfer_total > 0:
            pct = 100.0 * self._xfer_sent / self._xfer_total
            xfer = (
                f" xfer={self._current_file!r} "
                f"{self._xfer_sent}/{self._xfer_total} ({pct:.1f}%)"
            )
        elif self._current_file:
            xfer = f" ctx={self._current_file!r}"
        msg = f"I/O TX={self._io_tx} RX={self._io_rx} baud={baud}{phase}{xfer}"
        log.debug(msg)
        if self.debug_ssh:
            self._status(f"[debug] {msg}")

    def _drain_rx(self, idle_sec=0.05, max_total=0.5):
        """Discard stale RX after baud switch (garbage / tail packets)."""
        deadline = time.time() + max_total
        last_data = time.time()
        drained = 0
        while time.time() < deadline:
            chunk = self._port.read(256)
            if chunk:
                drained += len(chunk)
                last_data = time.time()
                if self.debug:
                    self._hex_log_line("DRAIN-RX", chunk)
            elif time.time() - last_data >= idle_sec:
                break
            else:
                time.sleep(0.005)
        if self.debug and drained:
            self._debug("drain_rx discarded %d bytes", drained)

    def _set_port_read_timeout(self, timeout):
        """Tune per-read wait on port (BridgeSerialAdapter or pyserial)."""
        if hasattr(self._port, "set_read_timeout"):
            self._port.set_read_timeout(timeout)
        elif hasattr(self._port, "timeout"):
            self._port.timeout = timeout

    def _prepare_uboot_kermit(self):
        """Wait for U-Boot download prompt, then match baud rate before Kermit."""
        self._status("Waiting for U-Boot 'Start UART downloading' prompt...")
        self.expect("Start UART downloading")

        high_speed = self.uboot_baudrate > self.baudrate
        if high_speed:
            self._status("Waiting for baudrate line (115200)...")
            try:
                self.expect("1500000", timeout=15.0)
            except TimeoutError:
                self._status("Warning: '1500000' not seen, switch PC baud anyway")
            self._port.baudrate = self.uboot_baudrate
            self._status(f"PC baudrate -> {self.uboot_baudrate}")
            # Flush 115200-rate residue before reading at 1.5M;
            # no Kermit traffic exists yet (device hasn't sent "Ready for binary").
            if hasattr(self._port, "reset_input_buffer"):
                self._port.reset_input_buffer()
            self._status("Waiting for 'Ready for binary' at 1.5M...")
            self.expect("Ready for binary", timeout=60.0)
            time.sleep(0.5)
        else:
            self._status("Waiting for 'Ready for binary' at 115200...")
            self.expect("Ready for binary", timeout=60.0)
            if hasattr(self._port, "reset_input_buffer"):
                self._port.reset_input_buffer()
            time.sleep(0.2)

    def parse_fip(self):
        fip = array("L")
        fip.frombytes(self.fip_bin)

        self.p1_start = 0x0
        self.p1_size = 0x1000
        self.fip_filelist = ["param1.bin"]
        self._status(f"p1_start={self.p1_start:#010x} p1_size={self.p1_size:#010x}")

        self.blcp_start = self.p1_start + self.p1_size
        self.blcp_size = 0x0
        if self.blcp_size:
            self.fip_filelist.append("blcp.bin")

        self.bl2_start = self.blcp_start + self.blcp_size
        self.bl2_size = fip[0xD8 // 4]
        if self.bl2_size:
            self.fip_filelist.append("bl2.bin")
            self._status(
                f"bl2_start={self.bl2_start:#010x} bl2_size={self.bl2_size:#010x}"
            )

        self.p2_start = fip[0xE0 // 4]
        self.p2_size = 0x1000
        if self.p2_size:
            self.fip_filelist.append("p2.bin")
            self._status(
                f"p2_start={self.p2_start:#010x} p2_size={self.p2_size:#010x}"
            )

        self.monitor_start = fip[(self.p2_start + 0x34) // 4]
        self.monitor_size = fip[(self.p2_start + 0x3C) // 4]
        if self.monitor_size:
            self.fip_filelist.append("monitor.bin")

        self.bl32_start = fip[(self.p2_start + 0x4C) // 4]
        self.bl32_size = fip[(self.p2_start + 0x54) // 4]
        if self.bl32_size:
            self.fip_filelist.append("bl32.bin")

        self.mcu_start = fip[(self.p2_start + 0x64) // 4]
        self.mcu_size = fip[(self.p2_start + 0x6C) // 4]
        if self.mcu_size:
            self.fip_filelist.append("mcu.bin")

        self.l2h_start = fip[(self.p2_start + 0x7C) // 4]
        self.l2h_size = 0x200
        if self.l2h_size:
            self.fip_filelist.append("l2h.bin")

        self.l2_start = self.l2h_start
        self.l2_size = fip[(self.l2h_start + 0xC) // 4]
        if self.l2h_size:
            self.fip_filelist.append("l2.bin")

        self._status(f"FIP stages: {self.fip_filelist}")

    def parse_fip_file(self):
        fip_file = join(self.work_dir, "fip.bin")
        if isfile(fip_file):
            with open(fip_file, "rb") as f:
                self.fip_bin = f.read()
            self.update_filelist.append("fip.bin")
            self._status(f"Found FIP: {fip_file}")
        else:
            raise FileNotFoundError(f"{fip_file} does not exist.")

    def start(self, reboot_timeout=300.0):
        """Wait for URPL after reboot, then run full UART download."""
        self._status("Waiting for device UART download mode (URPL)...")
        self._status(
            "Please reboot the board into UART download mode if not already."
        )

        deadline = time.time() + reboot_timeout
        while time.time() < deadline:
            try:
                self.expect("URPL", timeout=1.0)
                break
            except TimeoutError:
                continue
        else:
            raise TimeoutError(
                f"Timed out ({reboot_timeout:.0f}s) waiting for URPL after reboot"
            )

        self._status("URPL detected, sending URDL...")
        self.write("URDL")

        r = self.expect(re.compile(rb"EK/(\d+)/(\d+)/UL_START"))
        self.addr = (int(r.group(1)), int(r.group(2)))
        self._status(f"Download session addr={self.addr}")

        if (
            self.stop_after
            and self.stop_after not in (STOP_AFTER_FIP_DONE, "fip.bin")
            and self.stop_after not in self.fip_filelist
        ):
            raise ValueError(
                f"stop-after target {self.stop_after!r} not in this fip.bin "
                f"(available: {self.fip_filelist})"
            )

        self._getc_byte_timeout = self.kermit_byte_timeout
        self._set_port_read_timeout(self.kermit_read_timeout)

        self._begin_phase("fip-download", "FIP slices from fip.bin")
        self._status(
            f"=== FIP download phase: {len(self.fip_filelist)} slices from fip.bin ==="
        )
        for filename in self.fip_filelist:
            self._current_file = f"{filename}@fip.bin"
            self._xfer_sent = 0
            self._xfer_total = 0
            if filename == "p2.bin":
                self._port.baudrate = self.dl_baudrate
                time.sleep(0.5)
            self._status(f"=== FIP stage: {filename} (slice from fip.bin, not a disk file) ===")
            if self.debug:
                self._debug("ek18.start FIP slice %r", filename)
            self._kermit_start_with_retry(filename)
            if self._should_stop_after_fip_slice(filename):
                self._handover_serial(
                    f"Stopped after {self._fip_stop_label()} — serial ready for target CLI."
                )
                return

        self._status("FIP download phase completed.")

        if self.stop_after == STOP_AFTER_FIP_DONE:
            self._handover_serial(
                "Stopped after FIP download phase — serial ready for target CLI."
            )
            return

        # After FIP stages, either continue into U-Boot update phase or hand over to shell.
        self._port.baudrate = self.baudrate
        if self.skip_uboot_update:
            self._handover_serial(
                "Skip U-Boot update phase — serial ready for U-Boot CLI."
            )
            return

        self._begin_phase("uboot-wait", "wait U-Boot prompt")
        self._prepare_uboot_kermit()

        rate = self.uboot_baudrate
        high_speed = rate > self.baudrate
        self._status(f"=== U-Boot / partition update phase ({rate}) ===")
        inter_delay = (
            self.uboot_inter_file_delay
            if high_speed
            else self.uboot_inter_file_delay_115200
        )
        # Do not drain RX here — U-Boot may already have sent Kermit 'C' / handshake.
        if high_speed:
            self._getc_byte_timeout = self.kermit_byte_timeout_uboot
            self._set_port_read_timeout(self.kermit_read_timeout_uboot)
            self._status(
                f"Kermit timeouts: byte={self._getc_byte_timeout:.0f}s "
                f"read={self.kermit_read_timeout_uboot}s"
            )
        else:
            self._getc_byte_timeout = self.kermit_byte_timeout_uboot
            self._set_port_read_timeout(self.kermit_read_timeout)
        try:
            for filename in self.update_filelist:
                if filename == "fip.bin":
                    label = f"fip.bin (whole file, {len(self.fip_bin)} bytes)"
                    self._status(
                        f"=== U-Boot update: {label} @ {rate} baud ==="
                    )
                else:
                    label = filename
                    self._status(f"=== U-Boot update: {filename} @ {rate} baud ===")
                self._begin_phase("uboot-kermit", label)
                self._debug("ek18.start %r baud=%s", filename, self._port.baudrate)
                t0 = time.time()
                try:
                    self._kermit_start_with_retry(filename)
                finally:
                    self._maybe_report_io(force=True)
                    self._debug(
                        "ek18.done %r elapsed=%.1fs tx=%d rx=%d",
                        filename,
                        time.time() - t0,
                        self._io_tx,
                        self._io_rx,
                    )
                if inter_delay > 0:
                    time.sleep(inter_delay)
                if self.stop_after == "fip.bin" and filename == "fip.bin":
                    self._handover_serial(
                        "Stopped after U-Boot fip.bin — serial ready for U-Boot CLI."
                    )
                    return
        finally:
            self._getc_byte_timeout = self.kermit_byte_timeout
            self._set_port_read_timeout(self.kermit_read_timeout)

        self._port.baudrate = self.baudrate

        self._status("UART download finished successfully.")
        if not self.skip_miniterm:
            import miniterm

            self._status("Opening miniterm...")
            miniterm.main(self._port)

    def getc(self):
        deadline = time.time() + self._getc_byte_timeout
        while True:
            if time.time() > deadline:
                self._maybe_report_io(force=True)
                pending = getattr(self._port, "_rx_pending", None)
                pending_len = len(pending) if pending is not None else 0
                raise TimeoutError(
                    f"getc timeout ({self._getc_byte_timeout:.0f}s) waiting for serial byte "
                    f"(file={self._current_file!r} tx={self._io_tx} rx={self._io_rx} "
                    f"baud={getattr(self._port, 'baudrate', '?')} adapter_pending={pending_len})"
                )
            c = self._port.read(1)
            if len(c):
                self.rx_log.write(c)
                self._io_rx += 1
                if self.debug and self._io_rx % 4096 == 0:
                    self._hex_log_line("RX", c)
                    self._maybe_report_io()
                elif self.debug and self._io_rx % 512 == 0:
                    self._maybe_report_io()
                return c[0]

    def write(self, buf):
        if isinstance(buf, str):
            buf = buf.encode("ascii")

        # NOTE: ek18(tx_data) expects 0 on success (status code), not bytes written.
        # But we MUST ensure the whole buffer is written, otherwise a partial packet
        # can deadlock the Kermit session (device waits for remainder; host waits for ACK).
        write_limit = 30.0 if self._port.baudrate > self.baudrate else 5.0
        deadline = time.time() + write_limit
        idle = 0
        total = len(buf)
        if self.debug:
            self._hex_log_line("TX", buf)
        while len(buf):
            r = self._port.write(buf)
            if r is None:
                r = 0
            if r > 0:
                if self.debug:
                    self.tx_log.write(buf[:r])
                self._io_tx += r
                buf = buf[r:]
                idle = 0
                self._maybe_report_io()
                continue

            idle += 1
            if time.time() > deadline:
                self._maybe_report_io(force=True)
                raise TimeoutError(
                    f"serial write stalled (0 bytes written) "
                    f"file={self._current_file!r} remaining={len(buf)}/{total} "
                    f"baud={getattr(self._port, 'baudrate', '?')}"
                )
            # Yield a tiny bit; Windows drivers can transiently return 0.
            if idle % 50 == 0:
                time.sleep(0.001)

        self._port.flush()
        return 0

    def expect(self, msg, timeout=None):
        rx = None
        if isinstance(msg, str):
            msg = msg.encode("ascii")
        if isinstance(msg, re.Pattern):
            rx = msg

        buf = b""
        deadline = None if timeout is None else time.time() + timeout

        while True:
            if deadline is not None and time.time() > deadline:
                raise TimeoutError(f"expect timeout: {msg!r}")

            buf += bytes([self.getc()])

            if len(buf) >= 128:
                buf = buf[-128:]

            if rx:
                m = rx.search(buf)
                if m:
                    return m
            elif msg in buf:
                return True

    def _load_fip_slice(self, basename, start, size, mode):
        self.buf = array("B", self.fip_bin[start : start + size])
        log.info(
            "openfile: %r from fip.bin offset=%#x size=%#x (%d bytes) mode=%r",
            basename,
            start,
            size,
            size,
            mode,
        )

    def openfile(self, path, mode):
        path = path.decode("u8")
        basename = path.replace("install\\", "").replace("install/", "")
        self._current_file = basename
        self._progress_last_pct = -1

        if basename == "param1.bin":
            self._load_fip_slice(basename, self.p1_start, self.p1_size, mode)
        elif basename == "blcp.bin":
            self._load_fip_slice(basename, self.blcp_start, self.blcp_size, mode)
        elif basename == "bl2.bin":
            self._load_fip_slice(basename, self.bl2_start, self.bl2_size, mode)
        elif basename == "p2.bin":
            self._load_fip_slice(basename, self.p2_start, self.p2_size, mode)
        elif basename == "monitor.bin":
            self._load_fip_slice(
                basename, self.monitor_start, self.monitor_size, mode
            )
        elif basename == "bl32.bin":
            self._load_fip_slice(basename, self.bl32_start, self.bl32_size, mode)
        elif basename == "mcu.bin":
            self._load_fip_slice(basename, self.mcu_start, self.mcu_size, mode)
        elif basename == "l2h.bin":
            self._load_fip_slice(basename, self.l2h_start, self.l2h_size, mode)
        elif basename == "l2.bin":
            self._load_fip_slice(basename, self.l2_start, self.l2_size, mode)
        elif basename == "fip.bin":
            self.buf = array("B", self.fip_bin[:])
            log.info(
                "openfile: %r whole fip.bin (%d bytes) mode=%r",
                basename,
                len(self.fip_bin),
                mode,
            )
        else:
            full = join(self.work_dir, basename)
            if isfile(full):
                with open(full, "rb") as fp:
                    file_data = fp.read()
                self.buf = array("B", file_data[:])
                log.info(
                    "openfile: %r from disk %s (%d bytes) mode=%r",
                    basename,
                    full,
                    len(file_data),
                    mode,
                )
            elif isfile(basename):
                with open(basename, "rb") as fp:
                    file_data = fp.read()
                self.buf = array("B", file_data[:])
                log.info(
                    "openfile: %r from disk %s (%d bytes) mode=%r",
                    basename,
                    basename,
                    len(file_data),
                    mode,
                )
            else:
                log.info("file %s not exist, skip it!", basename)
                self.buf = array("B", [79])

        self.buf.reverse()
        self._xfer_total = len(self.buf)
        self._xfer_sent = 0
        if self.debug:
            self._debug(
                "openfile ready %r %d bytes (mode=%r)",
                basename,
                self._xfer_total,
                mode,
                to_ssh=True,
            )
        # Always show an initial progress line (helps SSH users see a new file started).
        if self._xfer_total > 0:
            self._status(
                f"[progress] {self._current_file} 0% (0/{self._xfer_total})"
            )
        return 0

    def fileinfo(self, filename):
        log.info("fileinfo: %r size=%d", filename, len(self.buf))
        return len(self.buf)

    def readfile(self):
        try:
            c = self.buf.pop()
        except IndexError:
            c = -1
        if c >= 0:
            self._xfer_sent += 1
            if self._xfer_total > 0:
                # Report every 5% (and always at 100%) to avoid spamming.
                pct_i = int(100 * self._xfer_sent / self._xfer_total)
                if (
                    pct_i != self._progress_last_pct
                    and (pct_i == 100 or pct_i % 5 == 0)
                ):
                    self._progress_last_pct = pct_i
                    self._status(
                        f"[progress] {self._current_file} {pct_i}% "
                        f"({self._xfer_sent}/{self._xfer_total})"
                    )
                if self.debug:
                    step = max(self._xfer_total // 20, 32768)
                    if self._xfer_sent == 1 or self._xfer_sent % step == 0:
                        pct = 100.0 * self._xfer_sent / self._xfer_total
                        self._debug(
                            "kermit send %r %d/%d (%.1f%%)",
                            self._current_file,
                            self._xfer_sent,
                            self._xfer_total,
                            pct,
                            to_ssh=True,
                        )
        return c
