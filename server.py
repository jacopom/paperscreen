#!/usr/bin/env python3
"""
PaperScreen — Kindle Scribe writing companion

Editor mode (default):
  python server.py
  Mac:    http://localhost:<port>/<token>/
  Kindle: http://<mac-ip>:<port>/<token>/

Terminal mode:
  python server.py --mode term --command claude
  python server.py --mode term --command bash
"""

import argparse
import asyncio
import fcntl
import json
import os
import re
import secrets
import shlex
import signal
import socket
import struct
import subprocess
import termios
from datetime import datetime
from pathlib import Path

import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
TOKEN: str = ""
HTML_CACHE: bytes = b""

# ---------------------------------------------------------------------------
# Editor state
# ---------------------------------------------------------------------------
clients: set = set()
current_content: str = ""
current_filename: str = ""
vault_path: Path = None

# ---------------------------------------------------------------------------
# Terminal state
# ---------------------------------------------------------------------------
term_clients: set = set()
term_buffer: list = [""]
PTY_FD: int = -1
PTY_PROC = None
MAX_TERM_LINES: int = 500
LOOP = None  # set in main(), used by on_pty_readable

# Strip ANSI/VT100 escape sequences, leaving plain text + \t \n \r
ANSI_RE = re.compile(
    r"\x1b"
    r"(?:"
    r"\[[\?><\!]?[0-9;]*[A-Za-z]"           # CSI  ESC [ [?><] ... letter
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"        # OSC  ESC ] ... BEL/ST
    r"|\([A-B012]"                            # charset
    r"|[^[\]()]"                              # other ESC + single char
    r")"
    r"|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"    # non-printable (keep \t \n \r)
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_local_ip() -> str:
    """Return LAN IP, preferring 192.168.x.x over VPN/Tailscale addresses."""
    candidates = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith('127.'):
                candidates.append(ip)
    except Exception:
        pass

    for ip in candidates:
        if ip.startswith('192.168.'):
            return ip
    for ip in candidates:
        if ip.startswith('172.') or (ip.startswith('10.') and not ip.startswith('10.0.')):
            return ip

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def date_filename() -> str:
    now = datetime.now()
    months = ["January","February","March","April","May","June",
              "July","August","September","October","November","December"]
    d = now.day
    suffix = "th" if 11 <= d <= 13 else {1:"st",2:"nd",3:"rd"}.get(d % 10, "th")
    return f"{months[now.month - 1]} {d}{suffix}, {now.year}"


def list_files() -> list:
    if vault_path and vault_path.exists():
        files = sorted(vault_path.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
        return [f.name for f in files]
    return []


def save_file(filename: str, content: str) -> None:
    vault_path.mkdir(parents=True, exist_ok=True)
    (vault_path / filename).write_text(content, encoding="utf-8")


def load_file(filename: str) -> str | None:
    p = vault_path / filename
    return p.read_text(encoding="utf-8") if p.exists() else None


# ---------------------------------------------------------------------------
# HTTP via websockets process_request
# ---------------------------------------------------------------------------
def http_response(status: int, reason: str, body: bytes,
                  content_type: str = "text/plain") -> Response:
    return Response(
        status, reason,
        Headers([
            ("Content-Type", content_type),
            ("Content-Length", str(len(body))),
        ]),
        body,
    )


def process_request(connection, request):
    """Serve HTML for plain HTTP GET; let WebSocket upgrades pass through."""
    if not request.path.startswith(f"/{TOKEN}"):
        return http_response(403, "Forbidden", b"Forbidden")

    # WebSocket upgrade — auth passed, let the handler run
    if request.headers.get("upgrade", "").lower() == "websocket":
        return None

    # Plain HTTP GET — serve the cached HTML
    return http_response(200, "OK", HTML_CACHE, "text/html; charset=utf-8")


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------
def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def update_term_buffer(text: str) -> None:
    """Feed text into the terminal line buffer, honouring \\r and \\n."""
    global term_buffer
    text = text.replace("\r\n", "\n")
    for ch in text:
        if ch == "\n":
            term_buffer.append("")
        elif ch == "\r":
            # Treat bare \r as a new line so TUI redraws don't overwrite content
            term_buffer.append("")
        else:
            term_buffer[-1] += ch
    if len(term_buffer) > MAX_TERM_LINES:
        term_buffer = term_buffer[-MAX_TERM_LINES:]


def start_pty(command: list, cols: int, rows: int) -> None:
    """Open a PTY and launch *command* inside it."""
    global PTY_FD, PTY_PROC
    import pty

    master_fd, slave_fd = pty.openpty()

    fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0))

    env = os.environ.copy()
    env.update({"TERM": "xterm-256color",
                "COLUMNS": str(cols),
                "LINES": str(rows)})

    def set_ctty() -> None:
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    PTY_PROC = subprocess.Popen(
        command,
        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        close_fds=True,
        preexec_fn=set_ctty,
        env=env,
    )
    os.close(slave_fd)
    PTY_FD = master_fd


# ---------------------------------------------------------------------------
# Editor WebSocket
# ---------------------------------------------------------------------------
async def broadcast(message: str, exclude=None) -> None:
    dead = set()
    for client in clients:
        if client is exclude:
            continue
        try:
            await client.send(message)
        except Exception:
            dead.add(client)
    clients.difference_update(dead)


async def ws_handler(websocket) -> None:
    global current_content, current_filename

    clients.add(websocket)
    try:
        await websocket.send(json.dumps({
            "type": "state",
            "content": current_content,
            "filename": current_filename,
            "files": list_files(),
        }))

        async for raw in websocket:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            t = data.get("type")

            if t == "content":
                current_content = data.get("content", "")
                current_filename = data.get("filename", current_filename)
                msg: dict = {"type": "content", "content": current_content,
                             "filename": current_filename}
                if "cursor" in data:
                    msg["cursor"] = data["cursor"]
                await broadcast(json.dumps(msg), exclude=websocket)

            elif t == "save":
                fn = data.get("filename") or current_filename
                content = data.get("content", current_content)
                if fn:
                    save_file(fn, content)
                    current_filename = fn
                    await websocket.send(json.dumps({"type": "saved", "filename": fn}))

            elif t == "load":
                fn = data.get("filename", "")
                content = load_file(fn)
                if content is not None:
                    current_content = content
                    current_filename = fn
                    await broadcast(json.dumps({"type": "content",
                                                "content": current_content,
                                                "filename": current_filename}))

            elif t == "new":
                fn = data.get("filename") or f"{date_filename()}.md"
                current_content = ""
                current_filename = fn
                await broadcast(json.dumps({"type": "content", "content": "", "filename": fn}))

            elif t == "list":
                await websocket.send(json.dumps({"type": "files", "files": list_files()}))

    finally:
        clients.discard(websocket)


# ---------------------------------------------------------------------------
# Terminal WebSocket
# ---------------------------------------------------------------------------
async def broadcast_term(msg: str) -> None:
    dead = set()
    for client in term_clients:
        try:
            await client.send(msg)
        except Exception:
            dead.add(client)
    term_clients.difference_update(dead)


def on_pty_readable() -> None:
    """Called by the event loop when PTY master has data to read."""
    global PTY_FD
    try:
        data = os.read(PTY_FD, 4096)
    except OSError:
        LOOP.remove_reader(PTY_FD)
        LOOP.create_task(broadcast_term(json.dumps({"type": "exit"})))
        return

    if not data:
        return

    text = strip_ansi(data.decode("utf-8", errors="replace"))
    update_term_buffer(text)
    msg = json.dumps({"type": "output", "text": "\n".join(term_buffer)})
    LOOP.create_task(broadcast_term(msg))


async def ws_term_handler(websocket) -> None:
    term_clients.add(websocket)
    try:
        await websocket.send(json.dumps({
            "type": "output",
            "text": "\n".join(term_buffer),
        }))

        async for raw in websocket:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            t = data.get("type")

            if t == "input" and PTY_FD >= 0:
                try:
                    os.write(PTY_FD, data.get("data", "").encode("utf-8"))
                except OSError:
                    pass

            elif t == "resize" and PTY_FD >= 0:
                cols = int(data.get("cols", 120))
                rows = int(data.get("rows", 40))
                try:
                    fcntl.ioctl(PTY_FD, termios.TIOCSWINSZ,
                                struct.pack("HHHH", rows, cols, 0, 0))
                    if PTY_PROC:
                        os.killpg(os.getpgid(PTY_PROC.pid), signal.SIGWINCH)
                except OSError:
                    pass

    finally:
        term_clients.discard(websocket)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def main() -> None:
    global vault_path, TOKEN, HTML_CACHE

    parser = argparse.ArgumentParser(description="PaperScreen — Kindle companion")
    parser.add_argument("--mode", choices=["editor", "term"], default="editor",
                        help="editor (default) or term")
    parser.add_argument("--command", default=os.environ.get("SHELL", "bash"),
                        help="Command to run in terminal mode (default: $SHELL)")
    parser.add_argument("--cols", type=int, default=120,
                        help="Terminal width in columns (default: 120)")
    parser.add_argument("--rows", type=int, default=40,
                        help="Terminal height in rows (default: 40)")
    parser.add_argument("--vault", default="~/Documents/Obsidian",
                        help="Obsidian vault / save folder (editor mode)")
    parser.add_argument("--port", type=int, default=8080,
                        help="Port for HTTP and WebSocket (single port)")
    parser.add_argument("--token", default=None,
                        help="URL access token (auto-generated if omitted)")
    args = parser.parse_args()

    if not args.token:
        args.token = secrets.token_urlsafe(10)

    TOKEN = args.token
    port = args.port
    ip = get_local_ip()
    script = Path(__file__).resolve()

    if args.mode == "term":
        cmd = shlex.split(args.command)
        start_pty(cmd, args.cols, args.rows)

        html_path = Path(__file__).parent / "static" / "terminal.html"
        html_str = html_path.read_text(encoding="utf-8").replace("__WS_PORT__", str(port))
        HTML_CACHE = html_str.encode("utf-8")

        global LOOP
        LOOP = asyncio.get_running_loop()
        LOOP.add_reader(PTY_FD, on_pty_readable)

        print(f"""
  PaperScreen — terminal mode
  command  {args.command}

  Mac      http://localhost:{port}/{TOKEN}/
  Kindle   http://{ip}:{port}/{TOKEN}/

  Ctrl+C to stop
""")
        async with websockets.serve(ws_term_handler, "0.0.0.0", port,
                                    process_request=process_request):
            await asyncio.Future()

    else:
        vault_path = Path(args.vault).expanduser()

        html_path = Path(__file__).parent / "static" / "index.html"
        html_str = html_path.read_text(encoding="utf-8").replace("__WS_PORT__", str(port))
        HTML_CACHE = html_str.encode("utf-8")

        print(f"""
  PaperScreen — editor mode
  vault    {vault_path}

  Mac      http://localhost:{port}/{TOKEN}/
  Kindle   http://{ip}:{port}/{TOKEN}/

  Shell alias (add to ~/.zshrc):
    alias paperscreen="python3 {script}"
    alias paperterm="python3 {script} --mode term --command"

  Ctrl+C to stop
""")
        async with websockets.serve(ws_handler, "0.0.0.0", port,
                                    process_request=process_request):
            await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  Stopped.")
