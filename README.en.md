# Serial SSH Bridge

**Serial-over-SSH bridge** — expose a UART over SSH for remote passthrough and one-shot UART Kermit firmware flashing (FIP slices + full `fip.bin` in U-Boot). Useful for board bring-up, production programming, and automation.

> Main program: `serial_ssh_bridge`. Suggested GitHub repo name: **`serial-ssh-bridge`**.

**Languages:** [中文](README.md) | **English**

## Features

| Feature | Description |
|---------|-------------|
| SSH shell passthrough | Multiple clients; bidirectional serial forwarding |
| Claude Code | Use Claude Code as an SSH client (via MCP terminal) |
| Claude skills | Skills guide Claude’s roles and safe operation order |
| UART upgrade | SSH `exec` runs `uart-upgrade` (FIP slices + U-Boot `fip.bin`) |
| Progress | Per-file `[progress] name N% (sent/total)` on SSH shells |
| SCP upload | Upload files to the directory next to the bridge binary |
| Retries | Per-file Kermit retry + full upgrade retry; bridge process stays up |
| Robustness | Serial write errors do not drop SSH; debug callbacks hardened in `ek18` |

> **Note:** This version **does not use an XML partition manifest**. The U-Boot phase sends **only the full `fip.bin`** (not `boot.emmc`, `rootfs.emmc`, etc.).

## Repository layout

```
serial-ssh-bridge/
├── bridge/                 # SSH bridge + upgrade core (recommended)
│   ├── serial_ssh_bridge.py
│   ├── uart_dl_core.py     # UART download state machine
│   ├── uart_upgrade.py     # SSH integration, dependency checks
│   ├── ek18/               # Kermit Cython extension sources
│   ├── make.py             # Build ek18
│   ├── serial_ssh_bridge.spec
│   └── dist/               # PyInstaller output (optional)
├── uart_dl/                # Standalone serial CLI (no SSH)
│   └── uart_dl.py
├── README.md               # Chinese
└── README.en.md            # English (this file)
```

## Upgrade flow (summary)

1. Board enters UART download mode; serial shows **`URPL`**
2. Host sends **`URDL`**; **FIP phase** sends slices from `fip.bin` (`param1.bin`, `bl2.bin`, `p2.bin`, …, `l2.bin`)
3. Board reboots to U-Boot; wait for **`Start UART downloading`** → switch to high baud → **`Ready for binary`**
4. Kermit sends **full `fip.bin`** only (no other images from XML)

## Roles & topology (Target / Jump Host / Client)

This project typically involves three roles:

- **Target hardware (DUT)**: the embedded board connected via **UART** to the jump host
- **Jump host (Bridge)**: runs `serial_ssh_bridge`; exposes **SSH/SCP** to the network and controls **UART upgrade**
- **Client (Server/Dev/CI/Claude)**: connects over the network to the jump host (SSH or Claude Code MCP) to debug and trigger upgrades

Typical topology:

```text
┌────────────────────┐        TCP/SSH/SCP         ┌──────────────────────────┐
│ Client machine      │  ───────────────────────▶ │ Jump Host (this project) │
│ (ssh/scp/Claude)    │                            │ serial_ssh_bridge        │
└────────────────────┘  ◀───────────────────────  └──────────────┬───────────┘
            ▲                 shell + status                      │ UART (COM)
            │                                                     │
            │                                                     ▼
            │                                          ┌──────────────────────┐
            └───────────────────────────────────────── │ Target hardware (DUT) │
                          serial output (broadcast)     │ U-Boot / ROM / Linux  │
                                                        └──────────────────────┘
```

Data/control paths:

- **Interactive passthrough (SSH shell)**: the client opens an SSH shell to the jump host; the jump host writes input to UART and broadcasts UART output back to all shell clients.
- **Upgrade trigger (SSH exec)**: the client runs `ssh ... uart-upgrade`; the jump host pauses shell writes, takes exclusive UART access for flashing, then restores passthrough.
- **File staging (SCP upload)**: the client uploads files to the jump host (saved next to the bridge binary), e.g. for delivering `fip.bin`.
- **Claude Code mode**: Claude acts as the client; skills define roles and safe operation order.

## Requirements

### Windows (exe or source)

- Windows 10+
- Python 3.10+ (for source runs)
- Working serial driver (COM port visible in Device Manager)
- **Visual Studio Build Tools** with C++ toolchain (to build `ek18`)

### Linux / Ubuntu (source only; build on target)

- Python 3.10+
- `pip install pyserial paramiko cython`
- `sudo apt install build-essential` (for `ek18`)
- Serial device usually `/dev/ttyUSB0` or `/dev/ttyACM0`

> **Important:** A Windows PyInstaller `.exe` **cannot** be copied to Ubuntu. On Linux, run `build_ext` and package on that system, or run `python serial_ssh_bridge.py` from source.

### Claude Code setup (on the client machine)

If you plan to operate the jump host using Claude Code, set up an MCP terminal so Claude can reliably SSH into the jump host.

1. Install MCP terminal: use **`mcp-interactive-terminal`** for a stable SSH experience.
2. In your project directory, run:

```bash
claude mcp add mcp-interactive-terminal -- npx -y mcp-interactive-terminal
```

3. Start Claude Code and run `/mcp` to verify the terminal is loaded.
4. Ask Claude to read your Skill first so it understands the workflow and roles before operating the device.

**MCP terminal keeps disconnecting?**  
`mcp-interactive-terminal` expects a **persistent interactive shell** (prompt + echo). Older bridge versions jumped straight into **raw serial passthrough** after the banner; with a quiet UART the session looks dead and MCP may treat it as closed.

The default `--shell-mode auto` uses a **bridge management shell** (`bridge>` prompt) when the client requests a PTY (MCP / interactive SSH). Use `attach` for raw serial, `status` for bridge state; non-PTY `ssh` still gets passthrough. You can also force a mode:

```powershell
.\serial_ssh_bridge.exe -p COM3 --ssh-port 2222 --shell-mode bridge       # always management shell (best for MCP)
.\serial_ssh_bridge.exe -p COM3 --ssh-port 2222 --shell-mode passthrough  # always raw passthrough
```

### Claude skills

1. Put skill files under `.claude/skills/`.
2. Clearly define these roles in the Skill to avoid confusion:
   - **Client machine (Claude)**
   - **Jump host (this bridge)**
   - **Target hardware (UART peer)**
3. Start from a verified Skill template if your team has one.

## Quick start (Windows prebuilt exe)

1. Place `fip.bin` **next to** `serial_ssh_bridge.exe` (e.g. `bridge/dist/`)
2. Start the bridge:

```powershell
cd bridge\dist
.\serial_ssh_bridge.exe -p COM3 -b 115200 --ssh-port 2222
```

3. Choose an operation mode (pick one):

#### Mode A: Operate via Claude

1. Ensure your Skill contains the correct jump host address, SSH port (default `2222`), and credentials.
2. Start Claude Code and load the Skill (see “Claude skills” above).
3. Verify MCP terminal is connected (`/mcp`), then instruct Claude to:
   - read serial output
   - run `uart-upgrade` (SSH exec) when needed

#### Mode B: Manual SSH (no Claude)

**B.1 SSH shell passthrough**

```bash
ssh -p 2222 admin@<bridge_ip>
# Default password: 123456
```

**B.2 Trigger UART upgrade** (separate terminal, SSH exec)

```bash
ssh -p 2222 admin@<bridge_ip> uart-upgrade
```

Progress is broadcast to all connected SSH shells.

## Run from source (Windows / Linux)

```bash
cd bridge
pip install pyserial paramiko cython pyinstaller

# Build Kermit extension ek18
python make.py build_ext
# On Windows you can use: py make.py build_ext

# Put fip.bin in bridge/ (or use --uart-dl-dir)
cp /path/to/fip.bin .

# Start
python serial_ssh_bridge.py -p COM3 -b 115200
# Linux example:
# python serial_ssh_bridge.py -p /dev/ttyUSB0 -b 115200
```

### Dependency check

```bash
python serial_ssh_bridge.py --check-deps
# Or via SSH:
ssh -p 2222 admin@<bridge_ip> __uart_deps__
```

## Building a Windows exe

PyInstaller bundles `serial_ssh_bridge.py` and dependencies (including the Cython module `ek18`) into a **single-file exe**. The result runs **only on Windows**; do not copy it to Linux/Ubuntu.

### Prerequisites

| Item | Requirement |
|------|-------------|
| OS | Windows 10/11 x64 |
| Python | 3.10+ (repo tested with **3.13**; see spec note below) |
| Compiler | [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/) with **Desktop development with C++** |
| Python packages | `pyserial`, `paramiko`, `cython`, `pyinstaller` |

Install dependencies (from `bridge/`):

```powershell
cd bridge
py -m pip install pyserial paramiko cython pyinstaller
```

### Build steps

**1. Stop any running instance**

If `serial_ssh_bridge.exe` is still running, PyInstaller may fail with `PermissionError: Access is denied` when overwriting `dist\serial_ssh_bridge.exe`:

```powershell
Get-Process serial_ssh_bridge -ErrorAction SilentlyContinue | Stop-Process -Force
```

**2. Build Kermit extension `ek18`**

`make.py` copies the built `.pyd` into `bridge/` (same folder as `serial_ssh_bridge.spec`):

```powershell
cd bridge
py make.py build_ext
```

You should get something like: `bridge\ek18.cp313-win_amd64.pyd` (`cp313` changes with your Python version).

**3. Match `serial_ssh_bridge.spec` binary name**

In `serial_ssh_bridge.spec`:

```python
binaries=[('ek18.cp313-win_amd64.pyd', '.')],
```

If you are not on Python 3.13, change this to the actual `ek18.cp3xx-win_amd64.pyd` file in `bridge/`.

**4. Run PyInstaller**

```powershell
cd bridge
py -m PyInstaller serial_ssh_bridge.spec --noconfirm
```

**5. Deployment folder**

Output:

```
bridge/dist/serial_ssh_bridge.exe
```

Place these **next to the exe** before shipping (e.g. copy the whole `dist/` folder):

| File | Description |
|------|-------------|
| `serial_ssh_bridge.exe` | Main program |
| `fip.bin` | Firmware image (required; provide yourself; do not commit) |
| `host_key` | Auto-generated on first run; may be pre-shipped |

Example:

```powershell
cd bridge\dist
.\serial_ssh_bridge.exe -p COM3 -b 115200 --ssh-port 2222
```

Logs (with `--uart-debug`): `dist\log\`

### One-shot build (PowerShell)

Stop the old exe first, then:

```powershell
cd E:\path\to\serial-ssh-bridge\bridge
Get-Process serial_ssh_bridge -ErrorAction SilentlyContinue | Stop-Process -Force
py make.py build_ext
py -m PyInstaller serial_ssh_bridge.spec --noconfirm
```

> PowerShell 5.x does not support `&&`; run commands on separate lines or use `powershell -NoProfile -Command "..."` per step.

### Packaging troubleshooting

| Symptom | Fix |
|---------|-----|
| `PermissionError` writing `dist\serial_ssh_bridge.exe` | Close running exe or release AV lock, retry |
| `ek18` build fails / `cl.exe` not found | Install VS Build Tools; use **x64 Native Tools** prompt for `build_ext` |
| exe cannot load `ek18` | Fix `binaries` name in `.spec`, rebuild |
| exe behavior unchanged after code edit | Re-run `build_ext` + PyInstaller; deploy the new exe |

### Linux note

Windows exe does not run on Linux. On Ubuntu, install the same deps, run `python make.py build_ext` and PyInstaller on that host, or run `python serial_ssh_bridge.py` from source.

## Command-line options

| Option | Default | Description |
|--------|---------|-------------|
| `-p, --port` | `/dev/ttyUSB0` | Serial device (Windows: `COM3`) |
| `-b, --baudrate` | `115200` | Initial / passthrough baud rate |
| `--ssh-port` | `2222` | SSH listen port |
| `--ssh-user` | `admin` | SSH username |
| `--ssh-pass` | `123456` | SSH password |
| `--uart-dl-dir` | exe/script directory | Work dir for `fip.bin` |
| `--uart-reboot-timeout` | `300` | Timeout (s) waiting for URPL |
| `--uboot-baudrate` | `1500000` | U-Boot Kermit baud rate |
| `--skip-uboot-update` | off | FIP slices only; skip U-Boot `fip.bin` (same as `--stop-after fip`) |
| `--stop-after STAGE` | none (full flow) | Stop after a stage and release serial for target CLI (see below) |
| `--uart-debug` | off | Verbose logs under `log/uart_debug.log` |
| `--uart-debug-ssh` | off | Mirror debug to SSH (needs `--uart-debug`) |
| `--check-deps` | - | Check dependencies and exit |

### Example: U-Boot at 115200 for debugging

```powershell
.\serial_ssh_bridge.exe -p COM3 -b 115200 --uboot-baudrate 115200 --uart-debug --uart-debug-ssh
```

### Example: FIP only, stay in U-Boot CLI

```powershell
.\serial_ssh_bridge.exe -p COM3 --skip-uboot-update
# equivalent:
.\serial_ssh_bridge.exe -p COM3 --stop-after fip
```

### Example: stop after BL31 for BL31 CLI

In this FIP layout **BL31 maps to `monitor.bin`**. After that slice finishes, serial is released to SSH passthrough / `attach`:

```powershell
.\serial_ssh_bridge.exe -p COM3 --stop-after bl31
```

Override per upgrade via SSH exec:

```bash
ssh -p 2222 admin@<bridge_ip> uart-upgrade --stop-after bl31
```

**`--stop-after` targets:**

| Value | Meaning |
|-------|---------|
| `bl2`, `p2`, `bl31`/`monitor`, `bl32`, `mcu`, `l2`, `l2h`, `param1` | Stop after that FIP slice |
| `fip` | Stop after all FIP slices (no U-Boot whole-file phase) |
| `fip.bin` | Stop after U-Boot `fip.bin` Kermit completes |
| unset / `full` | Full upgrade (default) |

## SCP upload to the bridge

Files are saved in the **same directory as `serial_ssh_bridge.exe`** (or `bridge/` when running from source).

```bash
scp -P 2222 myfile.bin admin@<bridge_ip>:.
```

> **Upload (sink) only**; download (`scp -f`) and recursive (`scp -r`) are not supported.

## SSH special commands

| Command | Description |
|---------|-------------|
| `uart-upgrade` / `__uart_upgrade__` / `bridge-uart-upgrade` | Start UART upgrade in background |
| `__uart_deps__` | Check `fip.bin`, `ek18`, etc. |

## Log files

With `--uart-debug`, logs are under `<uart-dl-dir>/log/`:

| File | Content |
|------|---------|
| `uart_debug.log` | Combined debug log |
| `uart_tx.log` / `uart_rx.log` | Raw serial TX/RX |
| `uart_hex.log` | Hex summary |

## Standalone serial tool (no SSH)

For local serial-only flashing, use `uart_dl/`:

```bash
cd uart_dl
python make.py build_ext
python uart_dl.py -p COM3 -b 115200
```

## FAQ

### 1. `PermissionError: device does not recognize the command` / shell drops

Usually a Windows serial issue (cable, driver, invalid COM). Recent builds **log the error but keep SSH**; verify the COM port and restart the bridge.

### 2. `returned a result with an exception set`

Often a Kermit debug callback issue; fixed in `ek18` — rebuild `ek18` and the exe.

### 3. U-Boot still tries boot/rootfs images

Use a build **without XML partition logic**; U-Boot phase should only show **`fip.bin`** `[progress]` lines.

### 4. PyInstaller `PermissionError` on build

Close any running `serial_ssh_bridge.exe` and rebuild.

### 5. Can Ubuntu use the Windows exe?

No. Build on Ubuntu from source or produce a Linux binary with PyInstaller there.

## Security

- Default credentials **`admin` / `123456`** are for lab use only — **do not expose to the public Internet**
- `host_key` is created on first run; use strong passwords and protect keys in production

## License

Add your license (e.g. MIT or proprietary). Before pushing to GitHub, ensure `fip.bin`, keys, and logs are in `.gitignore`.

## Issues

Open a GitHub Issue with:

- Bridge command line used
- Last ~100 lines of `log/uart_debug.log`
- Device serial output around URPL / Ready for binary
