#!/usr/bin/env python3
"""
PaperScreen — Kindle Scribe writing companion
Mac browser:    http://localhost:<port>/<token>/   (editor)
Kindle browser: http://<mac-ip>:<port>/<token>/   (display)
"""

import argparse
import asyncio
import json
import secrets
import socket
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

import websockets

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
clients: set = set()
current_content: str = ""
current_filename: str = ""
vault_path: Path = None
TOKEN: str = ""
WS_PORT: int = 8081
HTML_CACHE: str = ""


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


def list_files() -> list[str]:
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
# WebSocket
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

    # Auth: path must start with /<TOKEN>
    path = websocket.request.path
    if not path.startswith(f"/{TOKEN}"):
        await websocket.close(1008, "Unauthorized")
        return

    clients.add(websocket)
    try:
        # Send current state to new client
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
                msg: dict = {"type": "content", "content": current_content, "filename": current_filename}
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
                    await broadcast(json.dumps({
                        "type": "content",
                        "content": current_content,
                        "filename": current_filename,
                    }))

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
# HTTP
# ---------------------------------------------------------------------------
class HTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # suppress access logs
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

    parser = argparse.ArgumentParser(description="PaperScreen — Kindle writing server")
    parser.add_argument("--vault", default="~/Documents/Obsidian", help="Obsidian vault / save folder")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (WS uses port+1)")
    parser.add_argument("--token", default=None, help="URL access token (auto-generated if omitted)")
    args = parser.parse_args()

    if not args.token:
        args.token = secrets.token_urlsafe(10)

    vault_path = Path(args.vault).expanduser()
    TOKEN = args.token
    http_port = args.port
    WS_PORT = args.port + 1
    ip = get_local_ip()

    html_path = Path(__file__).parent / "static" / "index.html"
    HTML_CACHE = html_path.read_text(encoding="utf-8").replace("__WS_PORT__", str(WS_PORT))

    threading.Thread(target=run_http, args=(http_port,), daemon=True).start()

    script = Path(__file__).resolve()
    print(f"""
  PaperScreen
  vault    {vault_path}

  Mac      http://localhost:{http_port}/{TOKEN}/
  Kindle   http://{ip}:{http_port}/{TOKEN}/

  Shell alias (add to ~/.zshrc):
    alias paperscreen="python3 {script}"

  Ctrl+C to stop
""")

    async with websockets.serve(ws_handler, "0.0.0.0", WS_PORT):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  Stopped.")
