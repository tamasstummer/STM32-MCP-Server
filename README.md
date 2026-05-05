# stm32-mcp on Windows — Setup Guide

This guide walks through setting up [shieldyguy/stm32-mcp](https://github.com/shieldyguy/stm32-mcp) on **native Windows**. It covers the patches needed for cross-platform path handling, registration with Claude Code or Claude Desktop, a tool reference, and a prompt template for autonomous bugfix loops.

---

## What this MCP server does

Once set up, Claude can do all of the following autonomously:

### Build & Flash
- **Build** — compile firmware using the CubeIDE headless builder
- **Flash** — upload `.elf` / `.bin` / `.hex` to the board via ST-Link SWD
- **Build + flash in one step** — the most common combined operation

### Board management
- **Board info** — query ST-Link and MCU data (device ID, flash size, voltage)
- **Probe list** — list connected boards with nicknames and MCU IDs
- **Set nickname** — give boards or ST-Link probes friendly names (useful for multi-board setups)

### Serial communication (USB VCP)
- **List ports** — enumerate available serial ports
- **Connect / disconnect** — open and close serial ports
- **Send / receive** — send data and read responses
- **Serial sequence** — run multi-step tests in a single call (timing-sensitive operations)

### Memory read/write over SWD
- **Read memory** — by address or ELF symbol name
- **Write memory** — by address or variable name
- **Live memory monitoring** — start/read/stop continuous background memory polling

**Typical workflow:** edit source → build+flash → open serial connection → test firmware → disconnect.

---

## Prerequisites

Install the following on Windows. The recommended versions in parentheses are what was tested.

| Component | What it provides | Where to get it |
|---|---|---|
| **STM32CubeCLT** (1.21+) | `stm32cubeidec.exe` headless builder, `arm-none-eabi-*` toolchain, ST drivers | <https://www.st.com/en/development-tools/stm32cubeclt.html> |
| **xPack OpenOCD** (0.12+) | `openocd.exe` for flash + live memory + SWD | <https://github.com/xpack-dev-tools/openocd-xpack/releases> |
| **stlink-org tools** (latest) | `st-info.exe` for probe enumeration | <https://github.com/stlink-org/stlink/releases> |
| **Python** (3.10+) | runtime for the MCP server | <https://www.python.org/downloads/> (check **Add to PATH**) |
| **Git** | clone the repo | <https://git-scm.com/download/win> |
| **ST-Link USB driver** | recognized by Windows | bundled with CubeCLT installer |

### Important: CubeCLT does NOT include OpenOCD or st-info

A common gotcha. STMicro's CubeCLT ships their proprietary tools (`STM32_Programmer_CLI`, `STLink-gdb-server`) but the MCP server uses the open-source **OpenOCD** and **stlink-org** toolchain. Install both separately as listed above.

### Add to PATH

In Windows → *Edit the system environment variables* → *Environment Variables* → *Path*, add:

```
C:\ST\STM32CubeCLT_1.21.0\GNU-tools-for-STM32\bin
C:\xpack-openocd-0.12.0-6\bin
C:\Program Files\stlink\bin
```

(Adjust the version numbers and install paths to match your machine.) Open a fresh terminal and verify:

```cmd
arm-none-eabi-nm --version
openocd --version
st-info --version
```

All three should print version info. If any fails with "not recognized", the PATH entry is wrong.

---

## Setup

### 1. Clone the repo

```cmd
cd C:\Work\tools
git clone https://github.com/tamasstummer/STM32-MCP-Server.git
cd STM32-MCP-Server
```

### 2. Create venv and install the package

From the repo root:

```cmd
python -m venv .venv
.venv\Scripts\activate
pip install --upgrade pip
pip install -e .
```

The `-e .` (editable install) is **essential** — without it, `python -m stm32_mcp.server` will fail with `ModuleNotFoundError: No module named 'stm32_mcp'`. Editable mode also means future source edits take effect without reinstalling.

Verify the venv has the package:

```cmd
python -m stm32_mcp.server
```

This should hang silently waiting on stdin (because MCP speaks JSON-RPC over stdio). Press Ctrl+C to exit. If you see `ModuleNotFoundError`, the install didn't complete — re-run `pip install -e .` and check for errors.

### 4. Register with Claude

Choose one of the two clients.

#### Option A — Claude Code (CLI)

```cmd
claude mcp add stm32 -- "C:\Work\tools\stm32-mcp\.venv\Scripts\python.exe" -m stm32_mcp.server
```

The quotes around the Python path are required because of the backslashes. Verify:

```cmd
claude mcp list
```

`stm32` should appear with status *running*.

#### Option B — Claude Desktop

Edit `%APPDATA%\Claude\claude_desktop_config.json` (create it if missing):

```json
{
  "mcpServers": {
    "stm32": {
      "command": "C:\\Work\\tools\\stm32-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "stm32_mcp.server"],
      "env": {
        "STM32_CUBEIDE": "C:\\ST\\STM32CubeCLT_1.21.0\\stm32cubeidec.exe",
        "STM32_OPENOCD": "C:\\xpack-openocd-0.12.0-6\\bin\\openocd.exe",
        "STM32_ST_INFO": "C:\\Program Files\\stlink\\bin\\st-info.exe"
      }
    }
  }
}
```

Notes:
- Backslashes must be **doubled** (`\\`) in JSON, or use forward slashes (`/`) — Windows accepts both.
- The `env` block is optional if everything is on PATH and in default locations.
- After saving, **fully exit** Claude Desktop (including from the system tray) and restart it. The config is only re-read on startup.

### 5. Verify

In Claude (Code or Desktop), try:

> List the connected ST-Link probes.

Claude should call `stm32_list_probes` and return your boards. If it errors:
- Claude Desktop logs: `%APPDATA%\Claude\logs\mcp-server-stm32.log`
- Claude Code: pass `--verbose` to see the stderr output of the Python process

---

## Tool reference

The MCP server exposes 17 tools, grouped by purpose. Most have descriptive names; the one worth knowing in detail is `serial_sequence`.

### `serial_sequence` — multi-step hardware tests in one call

This is the most powerful tool — it combines serial I/O, SWD memory operations, and camera captures into a single timing-precise sequence.

#### Step types

**Send** — send a command on a serial connection and verify the response:
```json
{"send": "PING", "to": "COM3", "expect": "OK", "read_timeout": 2.0, "line_ending": "lf"}
```

**Delay** — wait the given number of milliseconds:
```json
{"delay_ms": 500}
```

**MemWrite** — write a memory address or ELF symbol over SWD:
```json
{"mem_write": true, "address": "0x48000418", "value": "0x40", "probe": "yellow", "width": 32}
{"mem_write": true, "symbol": "blink", "elf_path": "C:/proj/build/firmware.elf", "value": "1", "probe": "yellow"}
```

**MemRead** — read memory over SWD:
```json
{"mem_read": true, "address": "0x48000400", "count": 2, "probe": "yellow", "label": "gpio_pre"}
```

**Capture** — take a camera frame and save it as PNG (saved under your temp directory in `stm32-captures/`):
```json
{"capture": true, "label": "led_on", "device_index": 0}
```

#### Combined sequence example

A test that verifies an LED responds to a memory-poked variable, with camera proof on each side:

```json
[
  {"mem_write": true, "symbol": "blink", "elf_path": "C:/proj/build/firmware.elf", "value": "1", "probe": "yellow"},
  {"delay_ms": 100},
  {"capture": true, "label": "led_on"},
  {"send": "STATUS", "to": "COM3", "expect": "BLINK=1"},
  {"mem_write": true, "symbol": "blink", "elf_path": "C:/proj/build/firmware.elf", "value": "0", "probe": "yellow"},
  {"delay_ms": 100},
  {"capture": true, "label": "led_off"}
]
```

#### Sequence-level parameters

| Parameter | Description |
|---|---|
| `on_failure` | `"continue"` (default) or `"stop"` — whether to abort on first failed step |
| `filter_responses` | If `true`, `expect` only matches lines starting with `>` |

**Result:** per-step report plus a summary line (e.g. `3/3 sends OK, 2/2 assertions PASS`).

---

## Prompt template for autonomous bugfix loops

The point of integrating Claude with this MCP is letting it **iterate**: change the code, flash, observe the serial output, and decide whether the fix worked. The trick is being explicit about the loop, the success criterion, and the stop conditions. Without these, Claude either gives up too early or hammers the hardware indefinitely.

### Template

```
Task: <concrete bugfix or feature description, 1–3 sentences>

Project: C:\path\to\my_project
Board: <nickname, e.g. "doorbell"> — if unknown, run stm32_list_probes first
VCP port: <e.g. "COM7"> — if unknown, serial_list_ports

Definition of done (the success criterion that must be true):
  <e.g. "the >BLINK_STATE: output toggles between ON and OFF every 500ms,
   and the counter increases monotonically since reset">

Iteration loop, max 5 rounds:
  1. Read the relevant source files. State a HYPOTHESIS for the cause
     of the bug, or how the feature should fit in.
  2. Implement the change. Add temporary printf debug messages over VCP
     to verify the hypothesis if needed. Prefix all debug prints with "[DBG]".
  3. stm32_build_and_flash. If the build fails, fix it and go back to step 2.
  4. serial_connect, then serial_read for 3–5 seconds, OR use serial_sequence
     to send a command and capture the response.
  5. Compare actual output to the Definition of done.
     If PASS → go to Cleanup.
     If FAIL:
       - in one sentence, describe what you saw vs what you expected
       - update the hypothesis (do NOT repeat the same change)
       - go back to step 2

Cleanup on PASS:
  - Remove all "[DBG]" prefixed prints
  - Rebuild to confirm cleanup didn't break anything
  - Briefly summarize what you changed and why

Stop conditions (stop and ask):
  - 5 iterations without PASS
  - same failure observed twice in a row (loop)
  - hardware error: probe not responding, "Error: init mode failed", etc.

Constraints:
  - Do NOT touch functionality unrelated to the task
  - If a live_memory session is active, stop it before flashing
  - Never flash if you are unsure whether the build succeeded
```

### Worked example

```
Task: On the doorbell board, the heartbeat LED (PA5) sometimes freezes
after 10–30 seconds (stays solid on or solid off). It never resets itself.
Find the root cause and fix it.

Project: C:\dev\h-ion\H1_submodule_monolith
Board: doorbell
VCP port: COM7

Definition of done:
  - Serial output prints ">HB:<counter>" every 500ms
  - Counter increases monotonically with no gaps over 60 seconds
  - LED state matches the parity of the counter (even=ON, odd=OFF)
  - Verification: use serial_sequence to read for 60 seconds and count
    the number of >HB: lines — must be between 110 and 130

Iteration loop, max 5 rounds: [...as above...]

Note: the heartbeat task runs from the SysTick callback. I suspect an
interrupt priority issue, but PROVE OR DISPROVE it with debug prints
before assuming.
```

The last note (a hypothesis hint) is optional. Share what you suspect if you have a hunch — otherwise Claude probes blind. But don't fabricate a hint, because it'll force-fit the wrong hypothesis.
