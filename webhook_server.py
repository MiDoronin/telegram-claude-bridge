#!/usr/bin/env python3
"""
Telegram Webhook Server — receives instant pushes from Telegram.
Routes messages to Claude Code CLI ($0 with Max subscription).
Responds via the same bot that received the message.

No external dependencies — uses only Python stdlib.
"""

import json
import time
import urllib.request
import urllib.parse
import os
import threading
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration — edit config.json or set environment variables
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


CONFIG = load_config()
PORT = int(os.environ.get("WEBHOOK_PORT", CONFIG.get("port", 8443)))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", CONFIG.get("claude_bin", "claude"))
ALLOWED_CHATS = set(str(x) for x in CONFIG.get("allowed_chat_ids", []))
MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL", CONFIG.get("max_parallel_claude", 2)))

# Build bot mappings from config
BOTS = {}  # agent_name -> token
BOT_NAMES = {}  # agent_name -> display_name
TOKEN_TO_AGENT = {}  # token_prefix -> agent_name

for agent in CONFIG.get("agents", []):
    name = agent["name"]
    token = agent["token"]
    BOTS[name] = token
    BOT_NAMES[name] = agent.get("display_name", name)
    TOKEN_TO_AGENT[token.split(":")[0]] = name

HISTORY_DIR = Path(CONFIG.get("history_dir", "~/.telegram-claude-bridge/history")).expanduser()
HISTORY_DIR.mkdir(parents=True, exist_ok=True)

_claude_semaphore = threading.Semaphore(MAX_PARALLEL)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def save_history(agent, role, text):
    hist_file = HISTORY_DIR / f"{agent}.jsonl"
    entry = json.dumps({"role": role, "text": text, "ts": time.time()}, ensure_ascii=False)
    with open(hist_file, "a") as f:
        f.write(entry + "\n")


def tg_call(token, method, data):
    url = f"https://api.telegram.org/bot{token}/{method}"
    encoded = urllib.parse.urlencode(data).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=encoded), timeout=20)
    except Exception:
        pass


def process_message(agent, text, chat_id):
    """Typing → Claude Code → Response."""
    token = BOTS[agent]
    name = BOT_NAMES.get(agent, agent)

    # Typing loop in background
    stop_typing = threading.Event()

    def typing_loop():
        while not stop_typing.is_set():
            tg_call(token, "sendChatAction", {"chat_id": chat_id, "action": "typing"})
            stop_typing.wait(5)

    threading.Thread(target=typing_loop, daemon=True).start()

    save_history(agent, "user", text)

    with _claude_semaphore:
        try:
            result = subprocess.run(
                [CLAUDE_BIN, "-p", "--output-format", "text"],
                input=f"[{name}] {text}",
                capture_output=True, text=True, timeout=120,
                cwd=str(Path.home())
            )
            response = result.stdout.strip() or "Could not process. Try again."
        except subprocess.TimeoutExpired:
            response = "Request timed out."
        except Exception as e:
            response = f"Error: {e}"
        finally:
            stop_typing.set()

    save_history(agent, "assistant", response)

    # Send response
    chunks = [response[i:i + 4000] for i in range(0, len(response), 4000)]
    for chunk in chunks:
        tg_call(token, "sendMessage", {"chat_id": chat_id, "text": chunk})

    log(f"OUT [{agent}] {response[:60]}...")


# ---------------------------------------------------------------------------
# Webhook Handler
# ---------------------------------------------------------------------------


class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        path = self.path.strip("/")
        parts = path.split("/")

        agent = None
        if len(parts) >= 2 and parts[0] == "webhook":
            token_prefix = parts[1]
            agent = TOKEN_TO_AGENT.get(token_prefix)

        # Always respond 200 immediately
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

        if not agent:
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            update = json.loads(body)
        except Exception:
            return

        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return

        chat_id = str(msg.get("chat", {}).get("id", ""))
        if ALLOWED_CHATS and chat_id not in ALLOWED_CHATS:
            return

        text = msg.get("text") or msg.get("caption") or ""
        if not text:
            return

        log(f"IN  [{agent}] {text[:60]}...")

        # Typing immediately
        token = BOTS[agent]
        threading.Thread(
            target=tg_call,
            args=(token, "sendChatAction", {"chat_id": chat_id, "action": "typing"}),
            daemon=True
        ).start()

        # Process in background
        threading.Thread(
            target=process_message, args=(agent, text, chat_id), daemon=True
        ).start()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({
            "status": "ok",
            "agents": list(BOTS.keys()),
            "uptime": "running"
        }).encode())

    def log_message(self, format, *args):
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    log(f"Telegram → Claude Code Bridge")
    log(f"Port: {PORT} | Agents: {', '.join(BOTS.keys())}")
    log(f"Claude: {CLAUDE_BIN} | Max parallel: {MAX_PARALLEL}")

    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    log(f"Listening on 0.0.0.0:{PORT}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
