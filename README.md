# Serial SSH Bridge

**串口 SSH 桥接器** — 通过 SSH 远程透传串口，并一键触发 UART Kermit 固件烧录（FIP 分段 + U-Boot 整包 `fip.bin`）。适用搭配claude code去远程调试开发板、产线烧录与自动化测试。

> 主程序：`serial_ssh_bridge`（原工程目录名 `uart_ssh_dl` 可改为 GitHub 仓库名 `**serial-ssh-bridge`**）。

**语言 / Languages：** **中文** | [English](README.en.md)

## 功能概览


| 功能           | 说明                                                    |
| ------------ | ----------------------------------------------------- |
| SSH Shell 透传 | 多客户端同时连接，串口数据双向转发                                     |
| claude code  | claude code使用ssh 链接                                   |
| claude skill | skill 指导claude去开发调试                                   |
| UART 升级      | SSH `exec` 触发 `uart-upgrade`，自动完成 FIP 分段与 U-Boot 阶段下载 |
| 进度显示         | 每个固件传输时输出 `[progress] xxx N% (sent/total)`            |
| SCP 上传       | 支持 `scp` 将文件上传到 bridge 程序同级目录                         |
| 异常重试         | Kermit 单文件重试 + 整次升级流程重试，尽量不中断 bridge 进程               |
| 健壮性          | 串口写入失败不踢 SSH；调试回调不导致 Cython 异常崩溃                      |


> **说明**：当前版本 **不再依赖 XML 分区清单**，U-Boot 阶段仅发送 `**fip.bin` 整包**（不再下载 `boot.emmc` / `rootfs.emmc` 等镜像）。

## 目录结构

```
serial-ssh-bridge/          # 建议 GitHub 仓库名
├── bridge/                 # SSH 桥接 + 升级核心（推荐）
│   ├── serial_ssh_bridge.py
│   ├── uart_dl_core.py     # UART 下载状态机
│   ├── uart_upgrade.py     # SSH 集成、依赖检查
│   ├── ek18/               # Kermit Cython 扩展源码
│   ├── make.py             # 编译 ek18
│   ├── serial_ssh_bridge.spec
│   └── dist/               # PyInstaller 输出（可选）
├── uart_dl/                # 独立串口直连 CLI（无 SSH）
│   └── uart_dl.py
├── README.md               # 中文（本文件）
└── README.en.md            # English
```

## 升级流程简述

1. 设备进入 UART 下载模式，串口出现 `**URPL**`
2. PC 发送 `**URDL**`，开始 FIP 阶段（按 `fip.bin` 内偏移发送多个切片：`param1.bin`、`bl2.bin`、`p2.bin`、…、`l2.bin`）
3. 设备重启进入 U-Boot，等待 `**Start UART downloading**` → 切换高速波特率 → `**Ready for binary**`
4. Kermit 发送 **整包 `fip.bin`**（仅此文件，无 XML 其它镜像）

## 角色与拓扑关系（硬件设备 / 跳板机 / 服务器）

本项目里常见有三类角色：

- **硬件设备（Target / DUT）**：嵌入式板卡，通过 **UART 串口**与跳板机相连
- **跳板机（Jump Host / Bridge）**：运行 `serial_ssh_bridge`，对外提供 **SSH/SCP**，对内控制 **串口/UART 升级**
- **服务器/开发机（Client / Operator / Claude）**：通过网络访问跳板机（SSH 或 Claude Code MCP），进行透传调试与触发升级

典型拓扑如下：

```text
┌────────────────────┐        TCP/SSH/SCP         ┌──────────────────────────┐
│ Server / Dev / CI   │  ───────────────────────▶ │ Jump Host (this project) │
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

数据流/控制流说明：

- **SSH shell（交互透传）**：Server/Dev 端 `ssh` 连接到 Jump Host；Jump Host 将 SSH 输入写到 UART，并把 UART 输出广播回所有 shell 客户端。
- **SSH exec（触发升级）**：Server/Dev 端执行 `ssh ... uart-upgrade`；Jump Host 暂停 shell 写入，独占串口完成升级，再恢复透传。
- **SCP 上传**：Server/Dev 端通过 `scp` 上传文件到 Jump Host（保存到 `serial_ssh_bridge.exe` 同级目录），便于下发 `fip.bin` 或日志采集。
- **Claude Code 模式**：Claude 作为“客户端”，通过 MCP terminal 使用 SSH 访问 Jump Host，Skill 负责约束角色与操作顺序。

## 环境要求

### Windows（exe 或源码）

- Windows 10+
- Python 3.10+（源码运行时）
- 串口驱动正常（设备管理器中 COM 口可用）
- 编译 `ek18` 需 **Visual Studio Build Tools**（含 C++ 编译器）

### Linux / Ubuntu（仅源码，需本机编译）

- Python 3.10+
- `pip install pyserial paramiko cython`
- `sudo apt install build-essential`（编译 ek18）
- 串口一般为 `/dev/ttyUSB0` 或 `/dev/ttyACM0`

> **注意**：在 Windows 上用 PyInstaller 生成的 `.exe` **不能**直接拷贝到 Ubuntu 运行；Linux 需在目标系统上重新 `build_ext` 并打包（或直接 `python serial_ssh_bridge.py` 运行）。

### Claude Code 环境配置（在开发机 / 服务器端）

1. 安装 MCP：使用 `**mcp-interactive-terminal`**，便于 Claude 稳定连接 SSH。
2. 在工程目录执行：
  ```bash
   claude mcp add mcp-interactive-terminal -- npx -y mcp-interactive-terminal
  ```
3. 启动 Claude，执行 `/mcp`，确认 terminal 已正常加载。
4. 让 Claude 先读取 Skill，理解完整工作流后再操作设备。

**MCP 终端频繁断开？**  
`mcp-interactive-terminal` 期望 SSH 会话里有一个**持久的交互式 shell**（有提示符、有回显）。旧版 bridge 在 banner 打印后直接进入**裸串口透传**，串口无输出时终端像“死掉”一样，MCP 会误判会话结束。

默认已启用 `--shell-mode auto`：客户端申请 PTY（MCP/交互式 SSH）时进入 **bridge 管理 shell**（`bridge>` 提示符），可用 `attach` 进入串口透传、`status` 查看状态；普通 `ssh` 非 PTY 仍保持直通模式。也可显式指定：

```powershell
.\serial_ssh_bridge.exe -p COM3 --ssh-port 2222 --shell-mode bridge   # 始终管理 shell（推荐给 MCP）
.\serial_ssh_bridge.exe -p COM3 --ssh-port 2222 --shell-mode passthrough  # 始终裸透传
```

### Claude Skill 准备

1. Skill 文件可放在 `.claude/skills/` 目录。
2. Skill 中应明确三类角色：**开发机（Claude）**、**跳板机（本 bridge）**、**嵌入式设备（串口对端）**，避免 Claude 混淆身份。
3. 可使用团队内已验证的 Skill 作为模板参考。

## 快速开始（Windows 预编译 exe）

1. 将 `fip.bin` 放到与 `serial_ssh_bridge.exe` **同级目录**（例如 `bridge/dist/`）
2. 启动 bridge：

```powershell
cd bridge\dist
.\serial_ssh_bridge.exe -p COM3 -b 115200 --ssh-port 2222
```

1. 选择对接方式（二选一）：

#### 方式 A：通过 Claude 操作

1. 确认 Skill 中配置的跳板机地址、SSH 端口（默认 `2222`）与账号正确。
2. 启动 Claude，加载对应 Skill 并理解工作流（参见上文「Claude Skill 准备」）。
3. 在 Claude 中确认 MCP `mcp-interactive-terminal` 已连接（`/mcp` 检查）。
4. 向 Claude 下达指令，例如读取设备串口输出、执行 `uart-upgrade` 等。
5. 可以使用Deepseek-V4-Flash，消耗费用较少

#### 方式 B：手动 SSH（不使用 Claude）

**B.1 SSH Shell 透传串口**

```bash
ssh -p 2222 admin@<bridge_ip>
# 默认密码: 123456
```

**B.2 触发 UART 升级**（另开终端，SSH exec）

```bash
ssh -p 2222 admin@<bridge_ip> uart-upgrade
```

升级进度会广播到所有已连接的 SSH Shell。

## 从源码运行（Windows / Linux）

```bash
cd bridge
pip install pyserial paramiko cython pyinstaller

# 编译 Kermit 扩展 ek18
python make.py build_ext
# Windows 上也可用: py make.py build_ext

# 将 fip.bin 放到 bridge 目录（或 --uart-dl-dir 指定目录）
cp /path/to/fip.bin .

# 启动
python serial_ssh_bridge.py -p COM3 -b 115200
# Linux 示例:
# python serial_ssh_bridge.py -p /dev/ttyUSB0 -b 115200
```

### 依赖自检

```bash
python serial_ssh_bridge.py --check-deps
# 或 SSH:
ssh -p 2222 admin@<bridge_ip> __uart_deps__
```

## 打包为 Windows exe

PyInstaller 将 `serial_ssh_bridge.py` 及依赖（含 Cython 模块 `ek18`）打成**单文件 exe**。生成的 exe **仅能在 Windows 上运行**，不能拷贝到 Linux/Ubuntu 使用。

### 环境准备


| 项目       | 要求                                                                                                            |
| -------- | ------------------------------------------------------------------------------------------------------------- |
| 操作系统     | Windows 10/11 x64                                                                                             |
| Python   | 3.10+（仓库当前按 **3.13** 测试；见下方 spec 说明）                                                                          |
| 编译器      | [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/)，勾选 **“使用 C++ 的桌面开发”** |
| Python 包 | `pyserial`、`paramiko`、`cython`、`pyinstaller`                                                                  |


安装依赖（在 `bridge/` 目录）：

```powershell
cd bridge
py -m pip install pyserial paramiko cython pyinstaller
```

### 打包步骤

**1. 关闭正在运行的程序**

若 `serial_ssh_bridge.exe` 仍在运行，PyInstaller 会因无法覆盖 `dist\serial_ssh_bridge.exe` 而报 `PermissionError: 拒绝访问`。请先结束进程：

```powershell
Get-Process serial_ssh_bridge -ErrorAction SilentlyContinue | Stop-Process -Force
```

**2. 编译 Kermit 扩展 `ek18`**

`make.py` 会把编译好的 `.pyd` 复制到 `bridge/` 目录（与 `serial_ssh_bridge.spec` 同级）：

```powershell
cd bridge
py make.py build_ext
```

成功后应出现类似：`bridge\ek18.cp313-win_amd64.pyd`（文件名中的 `cp313` 随 Python 版本变化）。

**3. 确认 `serial_ssh_bridge.spec` 中的二进制名**

`serial_ssh_bridge.spec` 里有一行：

```python
binaries=[('ek18.cp313-win_amd64.pyd', '.')],
```

若你使用的不是 Python 3.13，请把文件名改成实际生成的 `ek18.cp3xx-win_amd64.pyd`，与 `bridge/` 目录下的文件一致。

**4. 执行 PyInstaller**

```powershell
cd bridge
py -m PyInstaller serial_ssh_bridge.spec --noconfirm
```

**5. 准备发布目录**

输出路径：

```
bridge/dist/serial_ssh_bridge.exe
```

建议将以下文件放在 **与 exe 同级** 的目录后再部署（例如整个 `dist/` 文件夹拷贝到目标 PC）：


| 文件                      | 说明                   |
| ----------------------- | -------------------- |
| `serial_ssh_bridge.exe` | 主程序                  |
| `fip.bin`               | 固件包（必需，自行准备，勿提交 Git） |
| `host_key`              | 首次运行可自动生成；也可预置       |


运行示例：

```powershell
cd bridge\dist
.\serial_ssh_bridge.exe -p COM3 -b 115200 --ssh-port 2222
```

日志目录（启用 `--uart-debug` 时）：`dist\log\`

### 一键打包（复制粘贴）

在 **PowerShell** 中（先关闭旧 exe）：

```powershell
cd E:\path\to\serial-ssh-bridge\bridge
Get-Process serial_ssh_bridge -ErrorAction SilentlyContinue | Stop-Process -Force
py make.py build_ext
py -m PyInstaller serial_ssh_bridge.spec --noconfirm
```

> PowerShell 5.x 不支持 `&&` 链式命令，请分行执行，或使用 `powershell -NoProfile -Command "..."` 逐条运行。

### 打包常见问题


| 现象                                                  | 处理                                                         |
| --------------------------------------------------- | ---------------------------------------------------------- |
| `PermissionError` 无法写入 `dist\serial_ssh_bridge.exe` | 关闭正在运行的 exe 或杀毒占用后重试                                       |
| `ek18` 编译失败 / 找不到 cl.exe                            | 安装 VS Build Tools 并打开 “x64 Native Tools” 环境再执行 `build_ext` |
| 运行 exe 报找不到 `ek18`                                  | 确认 spec 中 `binaries` 的 `.pyd` 文件名与 `bridge/` 下文件一致，并重新打包   |
| 修改代码后 exe 行为未变                                      | 重新执行 `build_ext` + PyInstaller，确保用的是新 exe                  |


### Linux 说明

Linux 不能使用 Windows 的 exe。若需在 Ubuntu 上分发，请在目标系统安装相同依赖后执行 `python make.py build_ext` 与 PyInstaller（生成无 `.exe` 后缀的可执行文件），或直接使用 `python serial_ssh_bridge.py` 运行源码。

## 常用命令行参数


| 参数                      | 默认值            | 说明                              |
| ----------------------- | -------------- | ------------------------------- |
| `-p, --port`            | `/dev/ttyUSB0` | 串口设备（Windows: `COM3`）           |
| `-b, --baudrate`        | `115200`       | 初始/透传波特率                        |
| `--ssh-port`            | `2222`         | SSH 监听端口                        |
| `--ssh-user`            | `admin`        | SSH 用户名                         |
| `--ssh-pass`            | `123456`       | SSH 密码                          |
| `--uart-dl-dir`         | exe/脚本所在目录     | 存放 `fip.bin` 的工作目录              |
| `--uart-reboot-timeout` | `300`          | 等待 URPL 超时（秒）                   |
| `--uboot-baudrate`      | `1500000`      | U-Boot 阶段 Kermit 波特率            |
| `--skip-uboot-update`   | 关闭             | 仅 FIP 分段，跳过 U-Boot `fip.bin`（等同 `--stop-after fip`） |
| `--stop-after STAGE`    | 无（烧录全流程）       | 在指定阶段后停止并释放串口供目标 CLI 使用（见下表）          |
| `--uart-debug`          | 关闭             | 写 `log/uart_debug.log` 等详细日志    |
| `--uart-debug-ssh`      | 关闭             | 调试信息同步到 SSH（需配合 `--uart-debug`） |
| `--check-deps`          | -              | 检查依赖后退出                         |


### 示例：U-Boot 阶段用 115200 排查

```powershell
.\serial_ssh_bridge.exe -p COM3 -b 115200 --uboot-baudrate 115200 --uart-debug --uart-debug-ssh
```

### 示例：只烧 FIP 分段，进入 U-Boot 命令行

```powershell
.\serial_ssh_bridge.exe -p COM3 --skip-uboot-update
# 或等价：
.\serial_ssh_bridge.exe -p COM3 --stop-after fip
```

### 示例：烧到 BL31 后停止，与 BL31 CLI 交互

本仓库 FIP 布局中 **BL31 对应 `monitor.bin`**。烧录完成后串口交还给 SSH 透传 / `attach`：

```powershell
.\serial_ssh_bridge.exe -p COM3 --stop-after bl31
```

也可在 SSH exec 中临时指定（覆盖启动参数）：

```bash
ssh -p 2222 admin@<bridge_ip> uart-upgrade --stop-after bl31
```

**`--stop-after` 可选阶段：**

| 参数值 | 含义 |
|--------|------|
| `bl2`, `p2`, `bl31`/`monitor`, `bl32`, `mcu`, `l2`, `l2h`, `param1` | 对应 FIP 切片烧录完成后停止 |
| `fip` | 全部 FIP 切片烧完即停（不进 U-Boot 整包阶段） |
| `fip.bin` | U-Boot 整包 `fip.bin` 发完后停止 |
| 不指定 / `full` | 完整流程（默认） |

## SCP 上传文件到 bridge

上传的文件保存在 `**serial_ssh_bridge.exe` 同级目录**（或源码运行时 `bridge/` 目录）。

```bash
scp -P 2222 myfile.bin admin@<bridge_ip>:.
```

> 当前实现支持 **上传（sink）**；不支持从 bridge **下载（scp -f）** 或 **递归目录（scp -r）**。

## SSH 特殊命令


| 命令                                                          | 说明                      |
| ----------------------------------------------------------- | ----------------------- |
| `uart-upgrade` / `__uart_upgrade__` / `bridge-uart-upgrade` | 后台启动 UART 升级            |
| `__uart_deps__`                                             | 检查 `fip.bin`、`ek18` 等依赖 |


## 日志位置

启用 `--uart-debug` 后，日志目录为 `<uart-dl-dir>/log/`：


| 文件                            | 内容       |
| ----------------------------- | -------- |
| `uart_debug.log`              | 综合调试日志   |
| `uart_tx.log` / `uart_rx.log` | 串口收发原始数据 |
| `uart_hex.log`                | 十六进制摘要   |


## 独立串口工具（无 SSH）

若只需本机串口直连烧录，可使用 `uart_dl/`：

```bash
cd uart_dl
python make.py build_ext
python uart_dl.py -p COM3 -b 115200
```

## 常见问题

### 1. `PermissionError: 设备不识别此命令` / Shell 掉线

多为 Windows 串口异常（拔线、驱动、COM 口失效）。新版本会 **记录错误但保持 SSH 连接**；请检查 COM 口并重启 bridge。

### 2. `returned a result with an exception set`

多为 Kermit 调试回调异常，已在 `ek18` 中加固；请使用最新编译的 `ek18` 与 exe。

### 3. U-Boot 阶段仍尝试下载 boot/rootfs 等

请确认使用 **已移除 XML 逻辑** 的版本；U-Boot 阶段应只看到 `**fip.bin`** 的 `[progress]`。

### 4. PyInstaller 打包失败 `PermissionError` 拒绝访问

请先关闭正在运行的 `serial_ssh_bridge.exe` 后重新打包。

### 5. Ubuntu 能否用 Windows 的 exe？

不能。请在 Ubuntu 上按「从源码运行」编译 `ek18`，或在该系统上执行 PyInstaller 生成 Linux 可执行文件。

## 安全提示

- 默认 SSH 账号密码为 `**admin` / `123456`**，仅供内网调试，**请勿暴露到公网**
- 首次连接会生成 `host_key`；生产环境请妥善保管并考虑更换密钥与强密码

## 许可证

请根据贵司政策补充 License（如 MIT / 专有许可）。仓库上传前请确认 `fip.bin`、密钥等敏感文件已加入 `.gitignore`。

## 贡献与问题反馈

欢迎通过 GitHub Issues 提交问题；请附上：

- bridge 启动命令行
- `log/uart_debug.log` 末尾约 100 行
- 设备串口关键输出（URPL / Ready for binary 等）

