"""stm32-serial — readline CLI client for the serial bridge."""

import readline  # noqa: F401 — enables arrow keys and history
import socket
import sys


def main():
    host = "127.0.0.1"
    port = 8765

    try:
        sock = socket.create_connection((host, port), timeout=3)
    except (ConnectionRefusedError, OSError) as e:
        print(f"Could not connect to bridge at {host}:{port}: {e}")
        print("Is the MCP server running?")
        sys.exit(1)

    sock.settimeout(None)  # blocking mode — timeout was only for connect
    f = sock.makefile("rb")

    # Read welcome banner (everything up to and including the first "\n> ")
    banner = b""
    while not banner.endswith(b"\n> "):
        chunk = f.read(1)
        if not chunk:
            break
        banner += chunk
    # Print banner without the trailing "\n> " — input() will show the prompt
    banner_text = banner.decode("utf-8", errors="replace")
    if banner_text.endswith("\n> "):
        banner_text = banner_text[:-3]
    print(banner_text, flush=True)

    # If a port path or nickname was given on the command line, send a connect command
    # Supports: stm32-serial /dev/cu.usbmodem1234
    #           stm32-serial "dev ccb"
    #           stm32-serial yellow
    if len(sys.argv) > 1:
        target = " ".join(sys.argv[1:])  # handles multi-word nicknames
        sock.sendall(f"connect {target}\n".encode())
        resp = _read_until_prompt(f)
        print(_strip_prompt(resp), flush=True)

    try:
        while True:
            try:
                line = input("> ")
            except EOFError:
                break

            sock.sendall((line + "\n").encode())

            if line.strip().lower() in ("quit", "exit"):
                resp = _read_until_prompt(f)
                print(_strip_prompt(resp), flush=True)
                break

            resp = _read_until_prompt(f)
            print(_strip_prompt(resp), flush=True)
    except KeyboardInterrupt:
        print()
    except (ConnectionResetError, BrokenPipeError):
        print("\nBridge connection lost.")
    finally:
        sock.close()


def _strip_prompt(text: str) -> str:
    """Remove the trailing '\\n> ' prompt so input() can show it instead."""
    if text.endswith("\n> "):
        return text[:-3]
    return text


def _read_until_prompt(f) -> str:
    """Read from the socket file until we see '\\n> ' prompt or EOF."""
    buf = b""
    while not buf.endswith(b"\n> "):
        chunk = f.read(1)
        if not chunk:
            break
        buf += chunk
    return buf.decode("utf-8", errors="replace")


if __name__ == "__main__":
    main()
