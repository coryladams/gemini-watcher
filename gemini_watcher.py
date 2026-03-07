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
import shutil
import hashlib
import difflib
import docx
import pptx
import openpyxl
from pathlib import Path
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- Vault Configuration (Hardcoded for Cadams) -------------------------------
VAULT_ROOT = "/mnt/c/Users/cadams/Documents/Obsidian/RegenMed"
DEFAULT_WATCH_DIR = "/mnt/c/Users/cadams/Downloads/VaultDrop"
DEFAULT_ARCHIVE_DIR = "/mnt/c/Users/cadams/Downloads/VaultDrop/Archive"
DEFAULT_FAILED_DIR = "/mnt/c/Users/cadams/Downloads/VaultDrop/Failed"
ATTACHMENTS_DIR = "Research/Attachments"

CONFIG_PATH = Path(__file__).parent / "gemini_config.json"

def load_config():
    defaults = {
        "watch_dir": DEFAULT_WATCH_DIR,
        "vault_root": VAULT_ROOT,
        "archive_dir": DEFAULT_ARCHIVE_DIR,
        "failed_dir": DEFAULT_FAILED_DIR,
        "archive_originals": True,
        "smart_routing": True,
        "model": "gemini-3.1-flash",
        "routes": {
            "research": {
                "extensions": ["pdf", "txt", "md", "rtf", "epub"],
                "destination": "Research",
                "instructions": "Summarize this technical document. Identify key concepts, tools, and methodologies. Use callouts for summaries and warnings."
            },
            "code": {
                "extensions": ["py", "cpp", "h", "js", "ts", "ino", "sh", "yaml", "xml", "sql"],
                "destination": "Research/Code",
                "instructions": "Explain this source code. Detail the architecture, key functions, and any dependencies found."
            },
            "data": {
                "extensions": ["csv", "json"],
                "destination": "Research/Data",
                "instructions": "Analyze this dataset. Identify patterns, anomalies, and provide a high-level statistical summary."
            },
            "images": {
                "extensions": ["png", "jpg", "jpeg", "webp"],
                "destination": "Research/Images",
                "instructions": "Analyze this image. If it is a diagram or schematic, explain its components and flow. If it is a screenshot of code or text, transcribe and summarize it. Describe the visual content in detail."
            },
            "media": {
                "extensions": ["mp3", "mp4", "wav", "m4a", "mov"],
                "destination": "Research/Media",
                "instructions": "Analyze this media file. For audio, provide a transcript and summary. For video, describe the visual content, any spoken words, and summarize the key events."
            },
            "webpages": {
                "extensions": ["html", "htm", "mhtml", "mht", "xml", "url", "webloc", "desktop"],
                "destination": "Research/Webclips",
                "instructions": "Extract and summarize the main content of this webpage. Ignore navigation menus, ads, and boilerplate text. Identify the core argument, key facts, and any cited sources. (If provided a URL, fetch it first)."
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

# --- Vault Indexer -----------------------------------------------------------
class VaultIndexer:
    def __init__(self, vault_root: str):
        self.vault_root = resolve_path(vault_root)
        self.cache_file = Path(__file__).parent / ".vault_index.json"
        self.ignore_dirs = {".git", ".obsidian", ".trash", "_attachments"}
        self.cache_expiry = 86400  # 24 hours

    def get_map(self) -> list[str]:
        if not self.vault_root.exists():
            return []
            
        # Check cache validity
        if self.cache_file.exists():
            try:
                with open(self.cache_file, "r") as f:
                    cache = json.load(f)
                    if time.time() - cache.get("timestamp", 0) < self.cache_expiry:
                        return cache.get("folders", [])
            except Exception:
                pass
                
        # Rebuild map
        folders = []
        for root, dirs, files in os.walk(self.vault_root):
            dirs[:] = [d for d in dirs if d not in self.ignore_dirs and not d.startswith(".")]
            try:
                rel_path = Path(root).relative_to(self.vault_root)
                if str(rel_path) != ".":
                    folders.append(str(rel_path).replace("\\", "/"))
            except ValueError:
                pass
                
        # Save cache
        try:
            with open(self.cache_file, "w") as f:
                json.dump({"timestamp": time.time(), "folders": folders}, f)
        except Exception as e:
            logging.warning(f"Could not save vault index cache: {e}")
            
        return folders

    def invalidate_cache(self):
        if self.cache_file.exists():
            try:
                self.cache_file.unlink()
            except Exception:
                pass

    def fuzzy_match_folder(self, target: str, folders: list[str]) -> str | None:
        if not target or target == "Inbox":
            return None
        if target in folders:
            return target
        matches = difflib.get_close_matches(target, folders, n=1, cutoff=0.6)
        return matches[0] if matches else None

# --- Read File (with PDF & URL support) -------------------------------------
def read_file_content(file_path: Path) -> str | None:
    try:
        if file_path.suffix.lower() in [".pdf", ".mp3", ".mp4", ".wav", ".m4a", ".mov"]:
            # Return None to trigger raw binary file processing by Gemini CLI
            return None
        elif file_path.suffix.lower() == ".docx":
            doc = docx.Document(file_path)
            return "\n".join([para.text for para in doc.paragraphs])
        elif file_path.suffix.lower() == ".pptx":
            prs = pptx.Presentation(file_path)
            text = []
            for slide in prs.slides:
                for shape in slide.shapes:
                    if hasattr(shape, "text"):
                        text.append(shape.text)
            return "\n".join(text)
        elif file_path.suffix.lower() == ".xlsx":
            wb = openpyxl.load_workbook(file_path, data_only=True)
            text = []
            for sheet in wb.sheetnames:
                ws = wb[sheet]
                text.append(f"--- Sheet: {sheet} ---")
                for row in ws.iter_rows(values_only=True):
                    row_text = [str(cell) if cell is not None else "" for cell in row]
                    if any(row_text):
                        text.append("\t".join(row_text))
            return "\n".join(text)
        elif file_path.suffix.lower() in [".mhtml", ".mht"]:
            # MHTML files can be processed natively by Gemini
            return None
        elif file_path.suffix.lower() == ".url":
            content = file_path.read_text(encoding="utf-8", errors="replace")
            for line in content.splitlines():
                if line.upper().startswith("URL="):
                    url = line[4:].strip()
                    return f"Please use your web fetch capabilities to read and analyze this URL:\n{url}"
            return content
        else:
            return file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logging.warning(f"  Could not read {file_path.name}: {e}")
        return None

# --- Clean Output -----------------------------------------------------------
def clean_gemini_output(text: str) -> str:
    """Removes CLI warnings, rogue code fences, and conversational filler that break Obsidian."""
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

    # 4. Strip conversational filler before the first frontmatter ---
    lines = text.splitlines()
    if lines and not lines[0].strip().startswith("---"):
        for i, line in enumerate(lines):
            if line.strip() == "---":
                # Found the likely start of frontmatter
                text = "\n".join(lines[i:])
                break
        
    # 5. Enforce strict Obsidian frontmatter (must start with ---)
    if not text.startswith("---"):
        if text.startswith("title:") or text.startswith("create-date:"):
            text = "---\n" + text
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if line.strip() == "" or line.startswith("#") or line.startswith(">"):
                    lines.insert(i, "---")
                    text = "\n".join(lines)
                    break
        else:
            text = f"---\ntitle: \"Processed Document\"\ncreate-date: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n---\n\n" + text

    return text

# --- Process with Gemini CLI -------------------------------------------------
def process_with_gemini(file_path: Path, content: str | None, route: dict, config: dict) -> str:
    instructions = route.get("instructions", "Analyze this file.")
    
    routing_instruction = "8. Use Obsidian callouts (> [!summary], etc.) for key sections."
    if config.get("smart_routing", False):
        indexer = VaultIndexer(config["vault_root"])
        folders = indexer.get_map()
        folder_list = "\n".join(f"- {f}" for f in folders[:500])  # limit to 500
        routing_instruction = f"""8. SMART ROUTING: Choose the most appropriate existing folder for this document from the vault map below.
   Include a `destination` key in the YAML frontmatter with the chosen path (e.g., `destination: Polaris/Schematics`).
   If no specific project folder matches, use `{route.get("destination", "Research")}`.
9. Use Obsidian callouts (> [!summary], etc.) for key sections.

Vault Map (Existing Folders):
{folder_list}
"""
    
    prompt = f"""<system_instruction>
You are an expert Research Archivist. 
Format the output as Obsidian-optimized Markdown.

CRITICAL FORMATTING RULES:
1. OUTPUT ONLY the Markdown file. DO NOT include any conversational filler (e.g. "I will begin by...").
2. You MUST start your response with exactly three dashes `---` on the very first line to begin the YAML frontmatter.
3. You MUST close the frontmatter block with exactly three dashes `---` on its own line.
4. DO NOT wrap the frontmatter in a markdown code block (no ``` or ```yaml).
5. YAML Frontmatter must use this EXACT structure (NO '#' symbols in the tags list):
---
title: "[Descriptive Title]"
create-date: {datetime.now().strftime("%Y-%m-%d %H:%M")}
type: reference
Project: Home
tags:
  - tag1
  - tag2
destination: {route.get("destination", "Research")}
status: complete
---
6. PREFERRED TAGS: Use these existing tags if relevant: [arduino, cpp, python, automation, hardware, schematics, research, data, tutorial, plc]
7. DO NOT create wikilinks (`[[link]]`) to concepts or pages unless you are absolutely certain the page already exists in the user's vault. If in doubt, DO NOT use wikilinks. 
{routing_instruction}
</system_instruction>

Task: {instructions}

"""
    
    cmd_base = ["gemini"]
    
    # Define fallback models (Ordered by Capability vs Cost)
    fallback_models = [
        "gemini-2.5-flash",
        "gemini-3.1-flash-preview",
        "gemini-3.1-flash-lite-preview",
        "gemini-2.0-flash",
        "gemini-2.5-flash-lite",
        "gemini-1.5-pro"
    ]
    
    # Always put the configured model first, followed by the rest
    models_to_try = []
    if "model" in config:
        models_to_try.append(config["model"])
        
    for m in fallback_models:
        if m not in models_to_try:
            models_to_try.append(m)
    
    cmd_args = []
    # If content is None, it means it's a binary/image file.
    if content is None:
        # We copy it locally to the project dir so Gemini CLI can read it without workspace errors
        local_copy = Path(__file__).parent / file_path.name
        shutil.copy2(file_path, local_copy)
        
        cmd_args.append(f"@{local_copy.name}")
        cmd_args.append(prompt)
    else:
        # For text files, we append the content to the prompt
        max_chars = 500000 
        clean_content = content.replace("\u0000", "")
        prompt += f"\n--- FILE: {file_path.name} ---\n{clean_content[:max_chars]}\n--- END ---\n"
        cmd_args.append(prompt)

    try:
        last_error = ""
        for model in models_to_try:
            cmd = cmd_base + ["--model", model] + cmd_args
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
                encoding="utf-8"
            )
            
            if result.returncode == 0:
                if content is None:
                    local_copy.unlink(missing_ok=True)
                return clean_gemini_output(result.stdout.strip())
            
            last_error = result.stderr.strip()
            # If the model is not found, try the next one in the fallback list
            if "ModelNotFoundError" in last_error or "404" in last_error or "not found" in last_error.lower():
                logging.warning(f"  ⚠️ Model '{model}' unavailable, trying next...")
                continue
            else:
                # If it's a different error, fail fast
                if content is None:
                    local_copy.unlink(missing_ok=True)
                raise RuntimeError(f"gemini command failed with {model}: {last_error}")
                
        if content is None:
            local_copy.unlink(missing_ok=True)
        raise RuntimeError(f"All configured models failed. Last error: {last_error}")
        
    except FileNotFoundError:
        if 'local_copy' in locals():
            local_copy.unlink(missing_ok=True)
        raise RuntimeError("Gemini CLI not found. Run 'npm install -g @google/gemini-cli'.")

# --- Routing, Archiving & Embedding ------------------------------------------
def route_output(original_path: Path, output: str, route: dict, config: dict):
    target_dest = route.get("destination", "Inbox")
    
    if config.get("smart_routing", False):
        import re
        match = re.search(r"^destination:\s*(.+)$", output, re.MULTILINE | re.IGNORECASE)
        if match:
            suggested_dest = match.group(1).strip().strip("'\"")
            indexer = VaultIndexer(config["vault_root"])
            folders = indexer.get_map()
            matched_dest = indexer.fuzzy_match_folder(suggested_dest, folders)
            if matched_dest:
                target_dest = matched_dest
            else:
                logging.info(f"  ⚠️ Could not match suggested destination '{suggested_dest}', using default '{target_dest}'")

    # 1. Write the markdown note
    dest_dir = resolve_path(config["vault_root"]) / target_dest
    if not dest_dir.exists():
        dest_dir.mkdir(parents=True, exist_ok=True)
        if config.get("smart_routing", False):
            VaultIndexer(config["vault_root"]).invalidate_cache()
            logging.info(f"  🔄 Created new folder '{target_dest}', invalidated index cache")

    dest_file = dest_dir / f"{original_path.stem}.md"
    if dest_file.exists():
        timestamp = datetime.now().strftime("%H%M%S")
        dest_file = dest_dir / f"{original_path.stem}-{timestamp}.md"

    # Append the source link if it's an internet shortcut
    if original_path.suffix.lower() in [".url", ".webloc"]:
        try:
            url_content = original_path.read_text(encoding="utf-8", errors="replace")
            for line in url_content.splitlines():
                if line.upper().startswith("URL="):
                    url = line[4:].strip()
                    output += f"\n\n---\n## Source Link\n**[🔗 View Original Webpage]({url})**\n"
                    break
        except Exception:
            pass
    else:
        # For all other files, append an embed link
        # Use ![[file]] for images/pdfs, and [[file]] for others like code
        embed_prefix = "!" if original_path.suffix.lower() in [".png", ".jpg", ".jpeg", ".webp", ".pdf", ".mp3", ".mp4", ".wav", ".m4a", ".mov"] else ""
        output += f"\n\n---\n## Source File\n{embed_prefix}[[{original_path.name}]]\n"

    dest_file.write_text(output, encoding="utf-8")
    print(f"  ✅ Note → {wsl_to_windows(str(dest_file))}")
    
    # 2. Always move the original file to the Obsidian attachments folder (except shortcuts)
    if original_path.suffix.lower() not in [".url", ".webloc"]:
        attach_dir = resolve_path(config["vault_root"]) / ATTACHMENTS_DIR
        attach_dir.mkdir(parents=True, exist_ok=True)
        attach_file = attach_dir / original_path.name
        
        if attach_file.exists():
            attach_file = attach_dir / f"{original_path.stem}-{int(time.time())}{original_path.suffix}"
            
        shutil.copy2(original_path, attach_file)
        print(f"  ✅ Attachment → {wsl_to_windows(str(attach_file))}")
        
    sys.stdout.flush()

def move_to_failed(file_path: Path, config: dict):
    failed_dir = resolve_path(config["failed_dir"])
    failed_dir.mkdir(parents=True, exist_ok=True)
    dest = failed_dir / file_path.name
    # Append timestamp if it exists to avoid overwriting failed files
    if dest.exists():
         dest = failed_dir / f"{file_path.stem}-{int(time.time())}{file_path.suffix}"
    file_path.rename(dest)
    logging.info(f"  ❌ Moved to Failed: {file_path.name}")

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
        time.sleep(2) # Give Windows extra time to finish writing the file
        
        try:
            content = None
            # Do not extract text for natively handled types (images, pdfs, media, mhtml)
            if route["name"] not in ["images", "media"] and file_path.suffix.lower() not in [".pdf", ".mhtml", ".mht"]:
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
            # Move the file to the 'Failed' folder
            try:
                move_to_failed(file_path, self.config)
            except Exception as move_e:
                logging.error(f"  Failed to move {file_path.name} to Failed folder: {move_e}")
        finally:
            if file_path in self.processing:
                self.processing.remove(file_path)

# --- Main --------------------------------------------------------------------
def main():
    config = load_config()
    
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
    watch_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"👁  Gemini Vault Watcher Active")
    logging.info(f"STARTED: Monitoring {wsl_to_windows(str(watch_dir))}")
    print(f"   Watching: {wsl_to_windows(str(watch_dir))}")
    print(f"   Vault:    {wsl_to_windows(config['vault_root'])}")
    
    handler = GeminiHandler(config)
    observer = Observer()
    observer.schedule(handler, str(watch_dir), recursive=False)
    observer.start()

    print("   Status: Monitoring active (with 10s poll fallback)")
    sys.stdout.flush()

    try:
        while True:
            for item in watch_dir.iterdir():
                if item.is_file() and not item.name.startswith("."):
                    handler.process(item)
            time.sleep(10)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
