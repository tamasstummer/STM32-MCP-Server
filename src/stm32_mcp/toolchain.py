"""Toolchain discovery — find CubeIDE, OpenOCD, st-info, and ARM toolchain.

ARM TOOLCHAIN (arm-none-eabi-nm, arm-none-eabi-gdb, etc.):
  These are found via PATH. Make sure the CubeIDE / CubeCLT bundled toolchain
  bin directory is on PATH.

  Windows examples:
    C:\\ST\\STM32CubeCLT_<ver>\\GNU-tools-for-STM32\\bin
    C:\\ST\\STM32CubeIDE_<ver>\\STM32CubeIDE\\plugins\\com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.*\\tools\\bin
"""

import glob
import os
import platform
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# Cached paths (populated on first call)
_cubeide_path: str | None = None
_openocd_path: str | None = None
_st_info_path: str | None = None
_cubeide_searched = False
_openocd_searched = False
_st_info_searched = False

IS_WINDOWS = sys.platform.startswith("win")


def _env(name: str) -> str | None:
    """Read an env var, return None if empty/unset."""
    val = os.environ.get(name)
    return val if val else None


def find_cubeide() -> str | None:
    """Find STM32CubeIDE headless executable. Caches result after first lookup.

    On Windows the headless binary is `stm32cubeidec.exe` (note the trailing 'c').
    Override with env var STM32_CUBEIDE.
    """
    global _cubeide_path, _cubeide_searched
    if _cubeide_searched:
        return _cubeide_path

    override = _env("STM32_CUBEIDE")
    if override and os.path.isfile(override):
        _cubeide_path = override
        _cubeide_searched = True
        return _cubeide_path

    if IS_WINDOWS:
        patterns = [
            r"C:\ST\STM32CubeIDE_*\STM32CubeIDE\stm32cubeidec.exe",
            r"C:\ST\STM32CubeIDE\stm32cubeidec.exe",
            os.path.expandvars(r"%ProgramFiles%\STMicroelectronics\STM32CubeIDE_*\STM32CubeIDE\stm32cubeidec.exe"),
        ]
    else:
        patterns = [
            "/Applications/STM32CubeIDE.app/Contents/MacOS/stm32cubeide",
            "/opt/st/stm32cubeide_*/stm32cubeide",
        ]

    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            _cubeide_path = sorted(matches)[-1]  # latest version
            break

    _cubeide_searched = True
    return _cubeide_path


def find_openocd() -> str | None:
    """Find OpenOCD executable. Caches result after first lookup.

    Looks on PATH first, then platform-specific common locations.
    Override with env var STM32_OPENOCD.
    """
    global _openocd_path, _openocd_searched
    if _openocd_searched:
        return _openocd_path

    override = _env("STM32_OPENOCD")
    if override and os.path.isfile(override):
        _openocd_path = override
        _openocd_searched = True
        return _openocd_path

    # PATH first — works for xPack OpenOCD, MSYS2, brew, apt, etc.
    found = shutil.which("openocd")
    if found:
        _openocd_path = found
        _openocd_searched = True
        return _openocd_path

    if IS_WINDOWS:
        candidates = glob.glob(r"C:\ST\STM32CubeCLT_*\OpenOCD\bin\openocd.exe")
        candidates += glob.glob(r"C:\xpack-openocd-*\bin\openocd.exe")
        candidates += [r"C:\Program Files\OpenOCD\bin\openocd.exe"]
        # CubeIDE bundles OpenOCD as a plugin
        candidates += glob.glob(
            r"C:\ST\STM32CubeIDE_*\STM32CubeIDE\plugins"
            r"\com.st.stm32cube.ide.mcu.externaltools.openocd.win32_*"
            r"\tools\bin\openocd.exe"
        )
    else:
        candidates = [
            "/opt/homebrew/bin/openocd",
            "/usr/local/bin/openocd",
            "/usr/bin/openocd",
        ]

    for path in sorted(candidates, reverse=True):
        if os.path.isfile(path):
            _openocd_path = path
            break

    _openocd_searched = True
    return _openocd_path


def find_st_info() -> str | None:
    """Find st-info (open-source stlink-org tools). Caches result after first lookup.

    Override with env var STM32_ST_INFO.
    """
    global _st_info_path, _st_info_searched
    if _st_info_searched:
        return _st_info_path

    override = _env("STM32_ST_INFO")
    if override and os.path.isfile(override):
        _st_info_path = override
        _st_info_searched = True
        return _st_info_path

    found = shutil.which("st-info")
    if found:
        _st_info_path = found
        _st_info_searched = True
        return _st_info_path

    if IS_WINDOWS:
        candidates = [
            r"C:\Program Files\stlink\bin\st-info.exe",
            r"C:\stlink\bin\st-info.exe",
        ]
    else:
        candidates = [
            "/opt/homebrew/bin/st-info",
            "/usr/local/bin/st-info",
            "/usr/bin/st-info",
        ]

    for path in candidates:
        if os.path.isfile(path):
            _st_info_path = path
            break

    _st_info_searched = True
    return _st_info_path


_programmer_cli_path: str | None = None
_programmer_cli_searched = False


def find_programmer_cli() -> str | None:
    """Find STM32_Programmer_CLI executable. Caches result after first lookup.

    Override with env var STM32_PROGRAMMER_CLI.
    """
    global _programmer_cli_path, _programmer_cli_searched
    if _programmer_cli_searched:
        return _programmer_cli_path

    override = _env("STM32_PROGRAMMER_CLI")
    if override and os.path.isfile(override):
        _programmer_cli_path = override
        _programmer_cli_searched = True
        return _programmer_cli_path

    found = shutil.which("STM32_Programmer_CLI")
    if found:
        _programmer_cli_path = found
        _programmer_cli_searched = True
        return _programmer_cli_path

    if IS_WINDOWS:
        candidates = glob.glob(r"C:\ST\STM32CubeCLT_*\STM32CubeProgrammer\bin\STM32_Programmer_CLI.exe")
        candidates += glob.glob(r"C:\ST\STM32CubeIDE_*\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.cubeprogrammer.win32_*\tools\bin\STM32_Programmer_CLI.exe")
        candidates += [r"C:\Program Files\STMicroelectronics\STM32Cube\STM32CubeProgrammer\bin\STM32_Programmer_CLI.exe"]
    else:
        candidates = [
            "/opt/st/stm32cubeprogrammer/bin/STM32_Programmer_CLI",
            "/usr/local/bin/STM32_Programmer_CLI",
        ]

    for path in sorted(candidates, reverse=True):
        if os.path.isfile(path):
            _programmer_cli_path = path
            break

    _programmer_cli_searched = True
    return _programmer_cli_path


def find_nm() -> str | None:
    """Find arm-none-eabi-nm on PATH (from CubeIDE / CubeCLT toolchain)."""
    return shutil.which("arm-none-eabi-nm")


def find_gdb() -> str | None:
    """Find arm-none-eabi-gdb on PATH (from CubeIDE / CubeCLT toolchain)."""
    return shutil.which("arm-none-eabi-gdb")


# ---------------------------------------------------------------------------
# OpenOCD target config mapping
# ---------------------------------------------------------------------------

# Chip ID (from st-info --probe) -> OpenOCD target config file
_CHIPID_TO_TARGET: dict[int, str] = {
    # STM32F0
    0x440: "stm32f0x.cfg", 0x442: "stm32f0x.cfg", 0x444: "stm32f0x.cfg",
    0x445: "stm32f0x.cfg", 0x448: "stm32f0x.cfg",
    # STM32F1
    0x410: "stm32f1x.cfg", 0x412: "stm32f1x.cfg", 0x414: "stm32f1x.cfg",
    0x418: "stm32f1x.cfg", 0x420: "stm32f1x.cfg", 0x428: "stm32f1x.cfg",
    0x430: "stm32f1x.cfg",
    # STM32F2
    0x411: "stm32f2x.cfg",
    # STM32F3
    0x422: "stm32f3x.cfg", 0x432: "stm32f3x.cfg", 0x438: "stm32f3x.cfg",
    0x439: "stm32f3x.cfg", 0x446: "stm32f3x.cfg",
    # STM32F4
    0x413: "stm32f4x.cfg", 0x419: "stm32f4x.cfg", 0x421: "stm32f4x.cfg",
    0x423: "stm32f4x.cfg", 0x431: "stm32f4x.cfg", 0x433: "stm32f4x.cfg",
    0x434: "stm32f4x.cfg", 0x441: "stm32f4x.cfg", 0x458: "stm32f4x.cfg",
    # STM32F7
    0x449: "stm32f7x.cfg", 0x451: "stm32f7x.cfg", 0x452: "stm32f7x.cfg",
    # STM32G0
    0x456: "stm32g0x.cfg", 0x460: "stm32g0x.cfg", 0x466: "stm32g0x.cfg",
    0x467: "stm32g0x.cfg",
    # STM32G4
    0x468: "stm32g4x.cfg", 0x469: "stm32g4x.cfg", 0x479: "stm32g4x.cfg",
    # STM32H7
    0x450: "stm32h7x.cfg", 0x480: "stm32h7x.cfg", 0x483: "stm32h7x.cfg",
    # STM32L0
    0x425: "stm32l0.cfg", 0x417: "stm32l0.cfg", 0x447: "stm32l0.cfg",
    0x457: "stm32l0.cfg",
    # STM32L1
    0x416: "stm32l1.cfg", 0x427: "stm32l1.cfg", 0x429: "stm32l1.cfg",
    0x436: "stm32l1.cfg", 0x437: "stm32l1.cfg",
    # STM32L4
    0x415: "stm32l4x.cfg", 0x435: "stm32l4x.cfg", 0x461: "stm32l4x.cfg",
    0x462: "stm32l4x.cfg", 0x464: "stm32l4x.cfg", 0x470: "stm32l4x.cfg",
    0x471: "stm32l4x.cfg", 0x472: "stm32l4x.cfg",
    # STM32L5
    0x472: "stm32l5x.cfg",
    # STM32WB
    0x494: "stm32wbx.cfg", 0x495: "stm32wbx.cfg", 0x496: "stm32wbx.cfg",
    # STM32WL
    0x497: "stm32wlx.cfg",
    # STM32C0
    0x443: "stm32c0x.cfg",
    # STM32U5
    0x455: "stm32u5x.cfg", 0x476: "stm32u5x.cfg", 0x481: "stm32u5x.cfg",
    0x482: "stm32u5x.cfg",
    # STM32H5
    0x474: "stm32h5x.cfg", 0x478: "stm32h5x.cfg", 0x484: "stm32h5x.cfg",
}


def openocd_target_cfg(chipid: int) -> str | None:
    """Map st-info chipid to OpenOCD target config filename."""
    return _CHIPID_TO_TARGET.get(chipid)


# Chips with limited SRAM that need a smaller work area for flash programming.
# Default stm32wbx.cfg sets 64KB but WB15/WB10 only have 12KB SRAM1.
_CHIPID_TO_WORKAREA: dict[int, int] = {
    0x494: 0x2000,  # STM32WB15 — 12KB SRAM1
    0x495: 0x2000,  # STM32WB10 — 12KB SRAM1
}


def openocd_workarea(chipid: int) -> int | None:
    """Return override work area size for chips that need it, or None."""
    return _CHIPID_TO_WORKAREA.get(chipid)


# ---------------------------------------------------------------------------
# OpenOCD runner
# ---------------------------------------------------------------------------

def run_openocd(
    sn: str,
    target_cfg: str,
    commands: list[str],
    timeout: int = 15,
    chipid: int = 0,
) -> str:
    """Run OpenOCD one-shot with the given commands. Returns combined output.

    Builds a command like:
        openocd -f interface/stlink.cfg -c "adapter serial <sn>"
                -c "transport select hla_swd"
                [-c "set WORKAREASIZE <size>"]
                -f target/<target_cfg>
                -c "<cmd1>" -c "<cmd2>" ... -c "shutdown"
    """
    openocd = find_openocd()
    if not openocd:
        raise FileNotFoundError("OpenOCD not found. Install OpenOCD and put it on PATH, or set STM32_OPENOCD env var.")

    cmd = [
        openocd,
        "-f", "interface/stlink.cfg",
        "-c", f"adapter serial {sn}",
        "-c", "transport select hla_swd",
    ]

    # Override work area for chips with limited SRAM (must come before target cfg)
    wa = openocd_workarea(chipid) if chipid else None
    if wa is not None:
        cmd.extend(["-c", f"set WORKAREASIZE 0x{wa:X}"])

    cmd.extend(["-f", f"target/{target_cfg}"])
    for c in commands:
        cmd.extend(["-c", c])
    cmd.extend(["-c", "shutdown"])

    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
    )
    return result.stdout + "\n" + result.stderr


# ---------------------------------------------------------------------------
# Project helpers (unchanged)
# ---------------------------------------------------------------------------

def get_project_name(project_path: str) -> str:
    """Parse .project XML to extract the Eclipse project name."""
    project_file = os.path.join(project_path, ".project")
    if not os.path.isfile(project_file):
        raise FileNotFoundError(f"No .project file found at {project_path}")

    tree = ET.parse(project_file)
    name_elem = tree.find("name")
    if name_elem is None or not name_elem.text:
        raise ValueError(f"Could not find <name> element in {project_file}")
    return name_elem.text


def validate_project_path(project_path: str) -> str:
    """Check that .project and .cproject exist. Returns resolved absolute path."""
    resolved = str(Path(project_path).resolve())

    if not os.path.isdir(resolved):
        raise FileNotFoundError(f"Directory does not exist: {resolved}")

    project_file = os.path.join(resolved, ".project")
    cproject_file = os.path.join(resolved, ".cproject")

    if not os.path.isfile(project_file):
        raise FileNotFoundError(
            f"No .project found at {resolved}. Is this a CubeIDE project?"
        )
    if not os.path.isfile(cproject_file):
        raise FileNotFoundError(
            f"No .cproject found at {resolved}. Is this a CubeIDE project?"
        )

    return resolved
