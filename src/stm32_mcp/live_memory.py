"""live_memory — continuous background memory monitoring via persistent OpenOCD TCL."""

import asyncio
import collections
import json
import os
import tempfile
import re
import socket
import struct
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .board_map import resolve_probe_full
from .debug_tools import _resolve_symbol, _width_from_size, _ocd_read_cmd
from .struct_layout import expand_struct
from .toolchain import find_openocd, openocd_workarea, openocd_target_cfg

_executor = ThreadPoolExecutor(max_workers=2)

TCL_PORT = 6666
TCL_TERMINATOR = b"\x1a"
CONNECT_TIMEOUT = 5.0
RING_BUFFER_SIZE = 100
MIN_INTERVAL_MS = 250


@dataclass
class ResolvedVariable:
    name: str
    address: int
    width: int  # 8, 16, 32
    is_float: bool = False


@dataclass
class LiveMemorySession:
    session_id: str
    variables: list[ResolvedVariable]
    sn: str
    target_cfg: str
    chipid: int
    interval_ms: int
    output_path: str
    # Runtime state
    process: subprocess.Popen | None = None
    tcl_sock: socket.socket | None = None
    thread: threading.Thread | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    ring_buffer: collections.deque = field(
        default_factory=lambda: collections.deque(maxlen=RING_BUFFER_SIZE)
    )
    start_time: float = 0.0
    read_count: int = 0
    error_count: int = 0


_sessions: dict[str, LiveMemorySession] = {}


# ---------------------------------------------------------------------------
# OpenOCD TCL protocol
# ---------------------------------------------------------------------------


def _start_openocd(sn: str, target_cfg: str, chipid: int) -> tuple[subprocess.Popen, socket.socket]:
    """Start OpenOCD as a persistent subprocess and connect to its TCL port."""
    openocd = find_openocd()
    if not openocd:
        raise FileNotFoundError("OpenOCD not found. Install via: brew install open-ocd")

    cmd = [
        openocd,
        "-f", "interface/stlink.cfg",
        "-c", f"adapter serial {sn}",
        "-c", "transport select hla_swd",
    ]

    wa = openocd_workarea(chipid) if chipid else None
    if wa is not None:
        cmd.extend(["-c", f"set WORKAREASIZE 0x{wa:X}"])

    cmd.extend(["-f", f"target/{target_cfg}"])
    cmd.extend(["-c", "init"])  # init but don't shutdown

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Retry-connect to TCL socket (new socket each attempt — connect() is one-shot)
    deadline = time.monotonic() + CONNECT_TIMEOUT
    sock = None

    while time.monotonic() < deadline:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            s.connect(("127.0.0.1", TCL_PORT))
            sock = s
            break
        except (ConnectionRefusedError, OSError):
            s.close()
            if proc.poll() is not None:
                _, stderr = proc.communicate(timeout=2)
                raise RuntimeError(
                    f"OpenOCD exited immediately (code {proc.returncode}):\n{stderr.decode(errors='replace').strip()}"
                )
            time.sleep(0.2)

    if sock is None:
        proc.terminate()
        proc.wait(timeout=5)
        raise RuntimeError(f"Could not connect to OpenOCD TCL port {TCL_PORT} within {CONNECT_TIMEOUT}s")

    return proc, sock


def _tcl_cmd(sock: socket.socket, cmd: str) -> str:
    """Send a command to OpenOCD's TCL interface and return the response."""
    sock.sendall((cmd + "\x1a").encode())

    data = bytearray()
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("OpenOCD TCL connection closed")
        data.extend(chunk)
        if TCL_TERMINATOR in data:
            # Strip the terminator
            idx = data.index(TCL_TERMINATOR[0])
            return data[:idx].decode(errors="replace")


# ---------------------------------------------------------------------------
# Variable resolution
# ---------------------------------------------------------------------------


def _resolve_variables(variables_json: str, elf_path: str) -> list[ResolvedVariable]:
    """Parse variable specs and resolve symbols to addresses."""
    try:
        specs = json.loads(variables_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in variables: {e}")

    if not isinstance(specs, list):
        raise ValueError("variables must be a JSON array")

    resolved = []
    for spec in specs:
        if isinstance(spec, str):
            # Symbol name — resolve from ELF
            if not elf_path:
                raise ValueError(f'elf_path required to resolve symbol "{spec}"')
            result = _resolve_symbol(elf_path, spec)
            if result is None:
                raise ValueError(f'Symbol "{spec}" not found in {os.path.basename(elf_path)}')
            addr, size = result

            # Auto-expand structs (size > 4 bytes)
            if size > 4:
                fields = expand_struct(elf_path, spec)
                if fields:
                    for f in fields:
                        resolved.append(ResolvedVariable(
                            name=f"{spec}.{f.name}",
                            address=addr + f.offset,
                            width=_width_from_size(f.size),
                        ))
                    continue

            resolved.append(ResolvedVariable(
                name=spec,
                address=addr,
                width=_width_from_size(size),
            ))
        elif isinstance(spec, dict):
            # Dict form — raw address or symbol with options
            is_float = spec.get("type", "").lower() == "float"

            if "symbol" in spec:
                sym_name = spec["symbol"]
                if not elf_path:
                    raise ValueError(f'elf_path required to resolve symbol "{sym_name}"')
                result = _resolve_symbol(elf_path, sym_name)
                if result is None:
                    raise ValueError(f'Symbol "{sym_name}" not found in {os.path.basename(elf_path)}')
                addr, size = result
                name = spec.get("name", sym_name)
                width = spec.get("width", _width_from_size(size))

                # Auto-expand structs unless expand=false
                if size > 4 and spec.get("expand", True):
                    fields = expand_struct(elf_path, sym_name)
                    if fields:
                        for f in fields:
                            resolved.append(ResolvedVariable(
                                name=f"{name}.{f.name}",
                                address=addr + f.offset,
                                width=_width_from_size(f.size),
                            ))
                        continue

                resolved.append(ResolvedVariable(
                    name=name, address=addr, width=width, is_float=is_float,
                ))
            elif "address" in spec:
                addr = int(spec["address"], 0)
                name = spec.get("name", f"0x{addr:08X}")
                width = spec.get("width", 32)
                resolved.append(ResolvedVariable(
                    name=name, address=addr, width=width, is_float=is_float,
                ))
            else:
                raise ValueError(f"Variable dict must have 'symbol' or 'address': {spec}")
        else:
            raise ValueError(f"Variable spec must be string or dict, got {type(spec).__name__}")

    return resolved


# ---------------------------------------------------------------------------
# Background session loop
# ---------------------------------------------------------------------------


def _parse_mdw_value(response: str) -> int | None:
    """Parse a hex value from OpenOCD mdw/mdh/mdb output.

    Format: 0x20000304: 00000001
    """
    m = re.search(r'0x[0-9a-fA-F]+:\s+([0-9a-fA-F]+)', response)
    if m:
        return int(m.group(1), 16)
    return None


def _try_reconnect(session: LiveMemorySession, max_retries: int = 3) -> bool:
    """Try to restart OpenOCD and reconnect TCL socket. Returns True on success."""
    # Clean up dead connection
    if session.tcl_sock:
        try:
            session.tcl_sock.close()
        except OSError:
            pass
        session.tcl_sock = None
    if session.process:
        try:
            session.process.terminate()
            session.process.wait(timeout=3)
        except Exception:
            try:
                session.process.kill()
            except Exception:
                pass
        session.process = None

    for attempt in range(max_retries):
        if session.stop_event.is_set():
            return False
        delay = (1 << attempt)  # 1s, 2s, 4s
        session.stop_event.wait(delay)
        if session.stop_event.is_set():
            return False

        try:
            proc, sock = _start_openocd(session.sn, session.target_cfg, session.chipid)
            session.process = proc
            session.tcl_sock = sock
            return True
        except (FileNotFoundError, RuntimeError):
            session.error_count += 1

    return False


def _run_session(session: LiveMemorySession) -> None:
    """Background daemon thread: poll variables and write JSONL."""
    try:
        output_file = open(session.output_path, "a")
    except OSError as e:
        session.error_count += 1
        return

    try:
        while not session.stop_event.is_set():
            loop_start = time.monotonic()
            elapsed_s = loop_start - session.start_time

            values: dict[str, int | float | str] = {}
            had_error = False

            for var in session.variables:
                read_cmd = _ocd_read_cmd(var.width)
                try:
                    response = _tcl_cmd(
                        session.tcl_sock,
                        f"{read_cmd} 0x{var.address:08X} 1",
                    )
                    raw_val = _parse_mdw_value(response)
                    if raw_val is not None:
                        if var.is_float and var.width == 32:
                            # Interpret as IEEE 754 float (little-endian)
                            float_val = struct.unpack(
                                "<f", struct.pack("<I", raw_val)
                            )[0]
                            values[var.name] = round(float_val, 6)
                        else:
                            values[var.name] = raw_val
                    else:
                        values[var.name] = f"ERROR: parse failed: {response.strip()}"
                        had_error = True
                except (OSError, ConnectionError):
                    # OpenOCD died or socket error — try to reconnect
                    session.error_count += 1
                    if not _try_reconnect(session):
                        return
                    break  # restart the variable loop with fresh connection
            else:
                # Only record entry if we read all variables (no break)
                entry = {
                    "t": round(time.time(), 3),
                    "elapsed_s": round(elapsed_s, 3),
                    "values": values,
                }

                session.ring_buffer.append(entry)
                session.read_count += 1
                if had_error:
                    session.error_count += 1

                try:
                    output_file.write(json.dumps(entry) + "\n")
                    output_file.flush()
                except OSError:
                    pass

                # Interruptible sleep for remaining interval
                loop_elapsed = time.monotonic() - loop_start
                remaining = (session.interval_ms / 1000.0) - loop_elapsed
                if remaining > 0:
                    session.stop_event.wait(remaining)

    except (OSError, ConnectionError):
        session.error_count += 1
    finally:
        output_file.close()
        # Clean up OpenOCD
        if session.tcl_sock:
            try:
                session.tcl_sock.close()
            except OSError:
                pass
        if session.process:
            try:
                session.process.terminate()
                session.process.wait(timeout=5)
            except Exception:
                try:
                    session.process.kill()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Async tool wrappers
# ---------------------------------------------------------------------------


async def live_memory_start(
    variables: str,
    elf_path: str = "",
    probe: str = "",
    interval_ms: int = 1000,
    output_path: str = "",
) -> str:
    """Start continuous background memory monitoring via SWD.

    Launches a persistent OpenOCD connection and polls the specified
    variables at the given interval. Results are written to a JSONL file
    and kept in a ring buffer accessible via live_memory_read.

    IMPORTANT: Only one live_memory session per probe at a time. Stop any
    existing session before starting a new one. Stop live_memory before
    flashing or using stm32_read_memory/stm32_write_memory.

    Args:
        variables: JSON array of variables to monitor. Elements can be:
            - String: symbol name resolved from ELF (e.g. "blink").
              Structs (size > 4 bytes) are auto-expanded into individual fields
              with dotted names (e.g. "blink.state", "blink.prev_output.changed").
            - Dict with "symbol": {"symbol": "temp", "type": "float"}
              Add "expand": false to disable struct auto-expansion.
            - Dict with "address": {"address": "0x20000304", "name": "x", "width": 32}
        elf_path: Path to .elf file for symbol resolution.
        probe: Board nickname, probe nickname, or ST-Link SN.
        interval_ms: Polling interval in milliseconds (minimum 250).
        output_path: JSONL output file path. Default: /tmp/live_memory_<id>.jsonl

    Returns:
        Session ID, output path, and resolved variable list.
    """
    interval_ms = max(interval_ms, MIN_INTERVAL_MS)

    sn, target_cfg, chipid = resolve_probe_full(probe)
    if not sn or not target_cfg:
        return "ERROR: Could not resolve probe. Use stm32_list_probes to see connected boards."

    try:
        resolved = _resolve_variables(variables, elf_path)
    except ValueError as e:
        return f"ERROR: {e}"

    if not resolved:
        return "ERROR: No variables specified."

    session_id = uuid.uuid4().hex[:8]
    if not output_path:
        output_path = os.path.join(tempfile.gettempdir(), f"live_memory_{session_id}.jsonl")

    session = LiveMemorySession(
        session_id=session_id,
        variables=resolved,
        sn=sn,
        target_cfg=target_cfg,
        chipid=chipid,
        interval_ms=interval_ms,
        output_path=output_path,
    )

    # Start OpenOCD (blocking — runs in executor)
    loop = asyncio.get_event_loop()
    try:
        proc, sock = await loop.run_in_executor(
            _executor,
            lambda: _start_openocd(sn, target_cfg, chipid),
        )
    except (FileNotFoundError, RuntimeError) as e:
        return f"ERROR: {e}"

    session.process = proc
    session.tcl_sock = sock
    session.start_time = time.monotonic()

    # Start background polling thread
    thread = threading.Thread(
        target=_run_session,
        args=(session,),
        daemon=True,
        name=f"live-memory-{session_id}",
    )
    session.thread = thread
    _sessions[session_id] = session
    thread.start()

    # Format response
    var_lines = []
    for v in resolved:
        type_str = "float" if v.is_float else f"{v.width}-bit"
        var_lines.append(f"  {v.name}: 0x{v.address:08X} ({type_str})")

    return (
        f"Live memory session started.\n"
        f"Session ID: {session_id}\n"
        f"Output: {output_path}\n"
        f"Interval: {interval_ms}ms\n"
        f"Variables:\n" + "\n".join(var_lines)
    )


async def live_memory_stop(session_id: str) -> str:
    """Stop a live memory monitoring session.

    Stops the background polling thread, kills the OpenOCD process, and
    returns session statistics.

    Args:
        session_id: Session ID from live_memory_start.

    Returns:
        Session statistics: duration, read count, error count, output path.
    """
    session = _sessions.pop(session_id, None)
    if session is None:
        available = list(_sessions.keys())
        if available:
            return f"ERROR: Session '{session_id}' not found. Active sessions: {', '.join(available)}"
        return f"ERROR: Session '{session_id}' not found. No active sessions."

    session.stop_event.set()

    if session.thread and session.thread.is_alive():
        session.thread.join(timeout=5)

    duration = time.monotonic() - session.start_time

    return (
        f"Session {session_id} stopped.\n"
        f"Duration: {duration:.1f}s\n"
        f"Reads: {session.read_count}\n"
        f"Errors: {session.error_count}\n"
        f"Output: {session.output_path}"
    )


async def live_memory_read(session_id: str, last_n: int = 10) -> str:
    """Read recent entries from a live memory session.

    Returns the last N entries from the in-memory ring buffer without
    touching the JSONL file.

    Args:
        session_id: Session ID from live_memory_start.
        last_n: Number of recent entries to return (default 10, max 100).

    Returns:
        Recent memory readings formatted as text.
    """
    session = _sessions.get(session_id)
    if session is None:
        available = list(_sessions.keys())
        if available:
            return f"ERROR: Session '{session_id}' not found. Active sessions: {', '.join(available)}"
        return f"ERROR: Session '{session_id}' not found. No active sessions."

    last_n = min(max(last_n, 1), RING_BUFFER_SIZE)

    entries = list(session.ring_buffer)[-last_n:]
    if not entries:
        alive = session.thread and session.thread.is_alive()
        status = "running" if alive else "stopped"
        return f"No data yet (session {status}, {session.read_count} reads so far)."

    lines = [f"Session {session_id} — last {len(entries)} of {session.read_count} reads:\n"]

    for entry in entries:
        elapsed = entry.get("elapsed_s", 0)
        values = entry.get("values", {})
        val_strs = []
        for name, val in values.items():
            if isinstance(val, float):
                val_strs.append(f"{name}={val:.4f}")
            elif isinstance(val, int):
                val_strs.append(f"{name}={val}")
            else:
                val_strs.append(f"{name}={val}")
        lines.append(f"  [{elapsed:7.3f}s] {', '.join(val_strs)}")

    alive = session.thread and session.thread.is_alive()
    status = "running" if alive else "stopped"
    lines.append(f"\nStatus: {status}, {session.error_count} errors")

    return "\n".join(lines)
