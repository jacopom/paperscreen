# PaperScreen

Use a **Kindle Scribe** (or any e-ink device with a browser) as a second monitor for writing. A lightweight local server syncs your markdown in real time — you type on your Mac, the Kindle displays the rendered output.

![PaperScreen diagram](https://github.com/user-attachments/assets/placeholder)

## How it works

- **Mac browser** (`localhost:8080/<token>/`) — the editor: full textarea, format bar, file management
- **Kindle browser** (`<mac-ip>:8080/<token>/`) — the display: rendered markdown, touch-to-position cursor, optional keyboard editing
- Both are connected via WebSocket; changes appear on the Kindle within ~150 ms

## Requirements

- Python 3.10+
- [`websockets`](https://pypi.org/project/websockets/) library

```bash
pip install websockets
```

## Quick start

```bash
git clone https://github.com/jacopom/paperscreen.git
cd paperscreen
python server.py
```

The server prints two URLs on startup:

```
  PaperScreen
  vault    ~/Documents/Obsidian

  Mac      http://localhost:8080/abc123XYZ/
  Kindle   http://192.168.1.42:8080/abc123XYZ/

  Shell alias (add to ~/.zshrc):
    alias paperscreen="python3 /path/to/server.py"
```

A random token is generated each run. Pass `--token` to use a fixed one:

```bash
python server.py --token mytoken
```

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--vault` | `~/Documents/Obsidian` | Folder where `.md` files are saved |
| `--port` | `8080` | HTTP port (WebSocket uses port+1) |
| `--token` | *(auto-generated)* | URL access token |

## Features

**Mac editor**
- Markdown textarea with live preview on Kindle
- Format bar: Bold · Italic · H1 · H2 · Code · Blockquote · List · HR
- Keyboard shortcuts: `⌘S` save · `⌘B` bold · `⌘I` italic
- Auto-saves to vault after 2 s of inactivity
- File browser (open / new / save)

**Kindle display**
- Rendered markdown, e-ink optimised (no animations, high contrast, large font)
- Visible cursor synced from Mac
- Tap anywhere on text to reposition the cursor
- ⌨ button opens a full-screen editor with the Kindle virtual keyboard — format bar included, auto-saves

**Network**
- Works on local LAN; Tailscale support planned

## File format

Files are saved as plain `.md` with date-based names (`March 24th, 2026.md`) for easy import into Obsidian or any markdown tool.

## License

MIT
