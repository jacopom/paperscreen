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
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

import websockets

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
TOKEN: str = ""
WS_PORT: int = 8081
HTML_CACHE: str = ""

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

# Strip ANSI/VT100 escape sequences, leaving plain text + \t \n \r
ANSI_RE = re.compile(
    r"\x1b"
    r"(?:"
    r"\[\??[0-9;]*[A-Za-z]"                 # CSI  ESC [ ... letter
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"       # OSC  ESC ] ... BEL/ST
    r"|\([A-B012]"                           # charset
    r"|[^[\]()]"                             # other ESC + single char
    r")"
    r"|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"   # non-printable (keep \t \n \r)
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_local_ip() -> str:
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
            term_buffer[-1] = ""          # carriage return: clear current line
        else:
            term_buffer[-1] += ch
    if len(term_buffer) > MAX_TERM_LINES:
        term_buffer = term_buffer[-MAX_TERM_LINES:]


def start_pty(command: list, cols: int, rows: int) -> None:
    """Open a PTY and launch *command* inside it."""
    global PTY_FD, PTY_PROC
    import pty

    master_fd, slave_fd = pty.openpty()

    # Set terminal dimensions
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0))

    env = os.environ.copy()
    env.update({"TERM": "xterm-256color",
                "COLUMNS": str(cols),
                "LINES": str(rows)})

    def set_ctty() -> None:
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)  # fd 0 = slave_fd = controlling tty

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

    if not websocket.request.path.startswith(f"/{TOKEN}"):
        await websocket.close(1008, "Unauthorized")
        return

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
        # PTY closed — process exited
        asyncio.get_event_loop().remove_reader(PTY_FD)
        asyncio.get_event_loop().create_task(
            broadcast_term(json.dumps({"type": "exit"}))
        )
        return

    if not data:
        return

    text = strip_ansi(data.decode("utf-8", errors="replace"))
    update_term_buffer(text)
    msg = json.dumps({"type": "output", "text": "\n".join(term_buffer)})
    asyncio.get_event_loop().create_task(broadcast_term(msg))


async def ws_term_handler(websocket) -> None:
    if not websocket.request.path.startswith(f"/{TOKEN}"):
        await websocket.close(1008, "Unauthorized")
        return

    term_clients.add(websocket)
    try:
        # Send current buffer so late-joiners see existing output
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
# HTTP
# ---------------------------------------------------------------------------
class HTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if not path.startswith(f"/{TOKEN}"):
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Forbidden")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML_CACHE.encode("utf-8"))


def run_http(port: int) -> None:
    HTTPServer(("0.0.0.0", port), HTTPHandler).serve_forever()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def main() -> None:
    global vault_path, TOKEN, WS_PORT, HTML_CACHE

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
                        help="HTTP port (WebSocket uses port+1)")
    parser.add_argument("--token", default=None,
                        help="URL access token (auto-generated if omitted)")
    args = parser.parse_args()

    if not args.token:
        args.token = secrets.token_urlsafe(10)

    TOKEN = args.token
    http_port = args.port
    WS_PORT = args.port + 1
    ip = get_local_ip()
    script = Path(__file__).resolve()

    threading.Thread(target=run_http, args=(http_port,), daemon=True).start()

    if args.mode == "term":
        cmd = shlex.split(args.command)
        start_pty(cmd, args.cols, args.rows)

        html_path = Path(__file__).parent / "static" / "terminal.html"
        HTML_CACHE = html_path.read_text(encoding="utf-8").replace("__WS_PORT__", str(WS_PORT))

        loop = asyncio.get_running_loop()
        loop.add_reader(PTY_FD, on_pty_readable)

        print(f"""
  PaperScreen — terminal mode
  command  {args.command}

  Mac      http://localhost:{http_port}/{TOKEN}/
  Kindle   http://{ip}:{http_port}/{TOKEN}/

  Ctrl+C to stop
""")
        async with websockets.serve(ws_term_handler, "0.0.0.0", WS_PORT):
            await asyncio.Future()

    else:
        vault_path = Path(args.vault).expanduser()

        html_path = Path(__file__).parent / "static" / "index.html"
        HTML_CACHE = html_path.read_text(encoding="utf-8").replace("__WS_PORT__", str(WS_PORT))

        print(f"""
  PaperScreen — editor mode
  vault    {vault_path}

  Mac      http://localhost:{http_port}/{TOKEN}/
  Kindle   http://{ip}:{http_port}/{TOKEN}/

  Shell alias (add to ~/.zshrc):
    alias paperscreen="python3 {script}"
    alias paperterm="python3 {script} --mode term --command"

  Ctrl+C to stop
""")
        async with websockets.serve(ws_handler, "0.0.0.0", WS_PORT):
            await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  Stopped.")
