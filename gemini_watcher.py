#!/usr/bin/env python3
"""
gemini_watcher.py
Watches a folder for new files, processes them via `gemini` CLI,
and routes the output directly to your Obsidian vault.

Optimized for: Gemini CLI + Obsidian + WSL
"""

import os
import sys
import time
import json
import logging
import subprocess
from pathlib import Path
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- Vault Configuration (Hardcoded for Cadams) -------------------------------
VAULT_ROOT = "/mnt/c/Users/cadams/Documents/Obsidian/RegenMed/Coding 1s & 0s"
DEFAULT_WATCH_DIR = "/mnt/c/Users/cadams/Downloads/VaultDrop"
DEFAULT_ARCHIVE_DIR = "/mnt/c/Users/cadams/Downloads/VaultDrop/Archive"

CONFIG_PATH = Path(__file__).parent / "gemini_config.json"

def load_config():
    defaults = {
        "watch_dir": DEFAULT_WATCH_DIR,
        "vault_root": VAULT_ROOT,
        "archive_dir": DEFAULT_ARCHIVE_DIR,
        "archive_originals": True,
        "model": "gemini-2.0-flash", # Fast & high-quality for summaries
        "routes": {
            "research": {
                "extensions": ["pdf", "txt", "md"],
                "destination": "Research",
                "instructions": "Summarize this technical document. Identify key concepts, tools, and methodologies. Use callouts for summaries and warnings."
            },
            "code": {
                "extensions": ["py", "cpp", "h", "js", "ts"],
                "destination": "Research/Code",
                "instructions": "Explain this source code. Detail the architecture, key functions, and any dependencies found."
            },
            "data": {
                "extensions": ["csv", "json"],
                "destination": "Research/Data",
                "instructions": "Analyze this dataset. Identify patterns, anomalies, and provide a high-level statistical summary."
            }
        }
    }
    
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            user_config = json.load(f)
            defaults.update(user_config)
    return defaults

# --- WSL ↔ Windows Path Translation -----------------------------------------
def wsl_to_windows(path: str) -> str:
    p = Path(path).parts
    if len(p) >= 3 and p[0] == "/" and p[1] == "mnt":
        drive = p[2].upper() + ":\\"
        rest = "\\".join(p[3:])
        return drive + rest
    return path

def resolve_path(path: str) -> Path:
    if path.startswith("/"):
        return Path(path)
    elif len(path) >= 2 and path[1] == ":":
        drive = path[0].lower()
        rest = path[3:].replace("\\", "/")
        return Path(f"/mnt/{drive}/{rest}")
    return Path(path).expanduser()

# --- Process with Gemini CLI -------------------------------------------------
def process_with_gemini(file_path: Path, content: str, route: dict, config: dict) -> str:
    instructions = route.get("instructions", "Analyze this file.")
    
    # Gemini's large context window means we don't need to truncate as much.
    max_chars = 500000 
    
    prompt = f"""<system_instruction>
You are an expert Research Archivist. 
Format the output as Obsidian-optimized Markdown.
- Use YAML frontmatter (title, date, tags).
- Use [[wikilinks]] for technical concepts.
- Use Obsidian callouts (> [!summary], etc.)
</system_instruction>

Task: {instructions}

--- FILE: {file_path.name} ---
{content[:max_chars]}
--- END ---"""

    try:
        result = subprocess.run(
            ["gemini", "ask", prompt],
            capture_output=True,
            text=True,
            timeout=300,
            encoding="utf-8"
        )
        if result.returncode != 0:
            raise RuntimeError(f"gemini ask failed: {result.stderr.strip()}")
        return result.stdout.strip()
    except FileNotFoundError:
        raise RuntimeError("Gemini CLI not found. Run 'npm install -g @google/gemini-cli'.")

# --- Routing & Archiving -----------------------------------------------------
def route_output(original_path: Path, output: str, route: dict, config: dict):
    dest_dir = resolve_path(config["vault_root"]) / route.get("destination", "Inbox")
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_file = dest_dir / f"{original_path.stem}.md"
    if dest_file.exists():
        timestamp = datetime.now().strftime("%H%M%S")
        dest_file = dest_dir / f"{original_path.stem}-{timestamp}.md"

    dest_file.write_text(output, encoding="utf-8")
    print(f"  ✅ → {wsl_to_windows(str(dest_file))}")

def archive_original(file_path: Path, config: dict):
    archive_dir = resolve_path(config["archive_dir"])
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / file_path.name
    file_path.rename(dest)
    logging.info(f"  📦 Archived: {file_path.name}")

# --- Event Handler -----------------------------------------------------------
class GeminiHandler(FileSystemEventHandler):
    def __init__(self, config):
        self.config = config

    def on_created(self, event):
        if event.is_directory: return
        file_path = Path(event.src_path)
        
        # Determine route based on extension
        ext = file_path.suffix.lower().lstrip(".")
        route = None
        for r_name, r_data in self.config["routes"].items():
            if ext in r_data["extensions"]:
                route = r_data
                route["name"] = r_name
                break
        
        if not route: return

        print(f"\n📥 {file_path.name}")
        time.sleep(1) 
        
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            print(f"  🤖 Processing via Gemini ({route['name']})...")
            output = process_with_gemini(file_path, content, route, self.config)
            route_output(file_path, output, route, self.config)
            if self.config["archive_originals"]:
                archive_original(file_path, self.config)
        except Exception as e:
            print(f"  ❌ Error: {e}")

# --- Main --------------------------------------------------------------------
def main():
    config = load_config()
    watch_dir = resolve_path(config["watch_dir"])
    watch_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    
    print(f"👁  Gemini Vault Watcher Active")
    print(f"   Watching: {wsl_to_windows(str(watch_dir))}")
    print(f"   Vault:    {wsl_to_windows(config['vault_root'])}")
    
    handler = GeminiHandler(config)
    observer = Observer()
    observer.schedule(handler, str(watch_dir), recursive=False)
    observer.start()

    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

if __name__ == "__main__":
    main()
