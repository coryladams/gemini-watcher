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
import pypdf
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

# --- Read File (with PDF support) -------------------------------------------
def read_file_content(file_path: Path) -> str | None:
    try:
        if file_path.suffix.lower() == ".pdf":
            reader = pypdf.PdfReader(file_path)
            text = ""
            for page in reader.pages:
                text += page.extract_text() + "\n"
            return text
        else:
            return file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logging.warning(f"  Could not read {file_path.name}: {e}")
        return None

# --- Clean Output -----------------------------------------------------------
def clean_gemini_output(text: str) -> str:
    """Removes CLI warnings and rogue code fences that break Obsidian."""
    lines = text.splitlines()
    
    # 1. Filter out known CLI warnings
    filtered = [l for l in lines if "MCP issues detected" not in l and "Run /mcp list" not in l]
    text = "\n".join(filtered).strip()

    # 2. Remove a single leading/trailing code block if the model wrapped everything
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # 3. Fix yaml code blocks mistakenly used for frontmatter
    if text.startswith("yaml\n---"):
        text = text[5:]
    elif text.startswith("yaml\n"):
        text = "---\n" + text[5:]
        
    # 4. Enforce strict Obsidian frontmatter (must start with ---)
    if not text.startswith("---"):
        # If it doesn't start with --- but looks like it has properties, wrap it
        if text.startswith("title:") or text.startswith("create-date:"):
            text = "---\n" + text
            
            # Find where to put the closing ---
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if line.strip() == "" or line.startswith("#") or line.startswith(">"):
                    lines.insert(i, "---")
                    text = "\n".join(lines)
                    break
        else:
            # Fallback if the model completely failed the frontmatter
            text = f"---\ntitle: \"Processed Document\"\ncreate-date: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n---\n\n" + text

    return text

# --- Process with Gemini CLI -------------------------------------------------
def process_with_gemini(file_path: Path, content: str | None, route: dict, config: dict) -> str:
    instructions = route.get("instructions", "Analyze this file.")
    
    prompt = f"""<system_instruction>
You are an expert Research Archivist. 
Format the output as Obsidian-optimized Markdown.

CRITICAL FORMATTING RULES:
1. You MUST start your response with exactly three dashes `---` on the very first line.
2. You MUST close the frontmatter block with exactly three dashes `---` on its own line.
3. DO NOT wrap the frontmatter in a markdown code block (no ``` or ```yaml).
4. YAML Frontmatter must use this EXACT structure:
---
title: "[Descriptive Title]"
create-date: {datetime.now().strftime("%Y-%m-%d %H:%M")}
type: reference
Project: Home
tags:
  - tag1
  - tag2
status: complete
---
5. PREFERRED TAGS: Use these existing tags if relevant: [arduino, cpp, python, automation, hardware, schematics, research, data, tutorial, plc]
6. Use [[wikilinks]] for technical concepts throughout the body.
7. Use Obsidian callouts (> [!summary], etc.) for key sections.
</system_instruction>

Task: {instructions}

"""
    
    cmd = ["gemini"]
    
    # If content is None, it means it's a binary/image file, so we use the @ syntax
    if content is None:
        cmd.append(f"@{file_path}")
        cmd.append(prompt)
    else:
        # For text files, we append the content to the prompt to ensure it's processed fully
        max_chars = 500000 
        clean_content = content.replace("\u0000", "")
        prompt += f"\n--- FILE: {file_path.name} ---\n{clean_content[:max_chars]}\n--- END ---\n"
        cmd.append(prompt)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            encoding="utf-8"
        )
        if result.returncode != 0:
            raise RuntimeError(f"gemini command failed: {result.stderr.strip()}")
        
        raw_output = result.stdout.strip()
        return clean_gemini_output(raw_output)
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
    sys.stdout.flush()

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
        self.processing = set()

    def on_created(self, event):
        if event.is_directory: return
        self.process(Path(event.src_path))

    def process(self, file_path: Path):
        if file_path in self.processing or not file_path.exists():
            return
        
        ext = file_path.suffix.lower().lstrip(".")
        route = None
        for r_name, r_data in self.config["routes"].items():
            if ext in r_data["extensions"]:
                route = r_data
                route["name"] = r_name
                break
        
        if not route: return

        self.processing.add(file_path)
        logging.info(f"DETECTED: {file_path.name}")
        print(f"\n📥 {file_path.name}")
        sys.stdout.flush()
        time.sleep(1) 
        
        try:
            # Determine if we should read text or pass the file path directly for multimodal
            content = None
            if route["name"] not in ["images"]:
                content = read_file_content(file_path)
                if not content:
                    logging.warning(f"SKIPPING: {file_path.name} (no content extracted)")
                    return

            logging.info(f"PROCESSING: {file_path.name} via Gemini ({route['name']})")
            print(f"  🤖 Processing via Gemini ({route['name']})...")
            sys.stdout.flush()
            
            output = process_with_gemini(file_path, content, route, self.config)
            route_output(file_path, output, route, self.config)
            if self.config["archive_originals"]:
                archive_original(file_path, self.config)
        except Exception as e:
            logging.error(f"ERROR: {file_path.name}: {e}")
            print(f"  ❌ Error: {e}")
            sys.stdout.flush()
        finally:
            if file_path in self.processing:
                self.processing.remove(file_path)

# --- Main --------------------------------------------------------------------
def main():
    config = load_config()
    
    # Setup Logging to both file and console
    log_path = Path(__file__).parent / "watcher.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        handlers=[
            logging.FileHandler(log_path),
            logging.StreamHandler(sys.stdout)
        ],
        force=True
    )
    
    watch_dir = resolve_path(config["watch_dir"])
    
    print(f"👁  Gemini Vault Watcher Active")
    logging.info(f"STARTED: Monitoring {wsl_to_windows(str(watch_dir))}")
    print(f"   Watching: {wsl_to_windows(str(watch_dir))}")
    print(f"   Vault:    {wsl_to_windows(config['vault_root'])}")
    
    # Start the observer
    handler = GeminiHandler(config)
    observer = Observer()
    observer.schedule(handler, str(watch_dir), recursive=False)
    observer.start()

    print("   Status: Monitoring active (with 10s poll fallback)")
    sys.stdout.flush()

    try:
        while True:
            # Fallback: Manual poll for files every 10 seconds in case events miss
            for item in watch_dir.iterdir():
                if item.is_file() and not item.name.startswith("."):
                    handler.process(item)
            time.sleep(10)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
