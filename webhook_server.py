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

# Telegram caps message text at 4096 chars; chunk a little below that.
MAX_MESSAGE_CHARS = 4000


def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def build_bots(config):
    """Build the routing tables from a config dict.

    Returns (bots, bot_names, token_to_agent):
      bots           -- agent_name -> token
      bot_names      -- agent_name -> display_name
      token_to_agent -- token_prefix -> agent_name

    Raises ValueError on malformed config (missing keys, or two bots sharing a
    token prefix) so misconfiguration fails loudly at startup instead of
    silently dropping a bot from the routing table.
    """
    bots = {}
    bot_names = {}
    token_to_agent = {}

    for agent in config.get("agents", []):
        try:
            name = agent["name"]
            token = agent["token"]
        except KeyError as e:
            raise ValueError(f"agent config missing required key: {e}")

        prefix = token.split(":")[0]
        if prefix in token_to_agent:
            raise ValueError(
                f"duplicate bot token prefix {prefix!r}: agents "
                f"{token_to_agent[prefix]!r} and {name!r} would collide in routing"
            )

        bots[name] = token
        bot_names[name] = agent.get("display_name", name)
        token_to_agent[prefix] = name

    return bots, bot_names, token_to_agent


CONFIG = load_config()
PORT = int(os.environ.get("WEBHOOK_PORT", CONFIG.get("port", 8443)))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", CONFIG.get("claude_bin", "claude"))
ALLOWED_CHATS = set(str(x) for x in CONFIG.get("allowed_chat_ids", []))
MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL", CONFIG.get("max_parallel_claude", 2)))

BOTS, BOT_NAMES, TOKEN_TO_AGENT = build_bots(CONFIG)

HISTORY_DIR = Path(CONFIG.get("history_dir", "~/.telegram-claude-bridge/history")).expanduser()

_claude_semaphore = threading.Semaphore(MAX_PARALLEL)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def save_history(agent, role, text, history_dir=None):
    hist_dir = Path(history_dir) if history_dir is not None else HISTORY_DIR
    hist_dir.mkdir(parents=True, exist_ok=True)
    hist_file = hist_dir / f"{agent}.jsonl"
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


def route_agent(path, token_to_agent):
    """Map a request path like '/webhook/<token_prefix>' to an agent name."""
    parts = path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "webhook":
        return token_to_agent.get(parts[1])
    return None


def extract_message(update):
    """Pull (chat_id, text) out of a Telegram update.

    Falls back from message to edited_message, and from text to caption.
    Returns (None, None) when the update carries no message.
    """
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return None, None
    chat_id = str(msg.get("chat", {}).get("id", ""))
    text = msg.get("text") or msg.get("caption") or ""
    return chat_id, text


def is_authorized(chat_id, allowed_chats):
    """Fail-closed authorization: only chats in the allowlist are allowed.

    An empty allowlist denies everyone (the server refuses to start without one
    — see main()), so this never silently allows all.
    """
    return chat_id in allowed_chats


def chunk_response(response, size=MAX_MESSAGE_CHARS):
    """Split a response into Telegram-sized chunks."""
    return [response[i:i + size] for i in range(0, len(response), size)]


def run_claude(text, name):
    """Run the Claude CLI for one message, mapping failures to user text."""
    with _claude_semaphore:
        try:
            result = subprocess.run(
                [CLAUDE_BIN, "-p", "--output-format", "text"],
                input=f"[{name}] {text}",
                capture_output=True, text=True, timeout=120,
                cwd=str(Path.home())
            )
            return result.stdout.strip() or "Could not process. Try again."
        except subprocess.TimeoutExpired:
            return "Request timed out."
        except Exception as e:
            return f"Error: {e}"


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

    try:
        response = run_claude(text, name)
    finally:
        stop_typing.set()

    save_history(agent, "assistant", response)

    for chunk in chunk_response(response):
        tg_call(token, "sendMessage", {"chat_id": chat_id, "text": chunk})

    log(f"OUT [{agent}] {response[:60]}...")


# ---------------------------------------------------------------------------
# Webhook Handler
# ---------------------------------------------------------------------------


class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        agent = route_agent(self.path, TOKEN_TO_AGENT)

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

        chat_id, text = extract_message(update)
        if not chat_id:
            return

        if not is_authorized(chat_id, ALLOWED_CHATS):
            return

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
    if not ALLOWED_CHATS:
        log("FATAL: allowed_chat_ids is empty. Refusing to start — this bridge "
            "runs the Claude CLI on your machine, so an unrestricted bot would let "
            "anyone execute against it. Set allowed_chat_ids in config.json to the "
            "Telegram user ID(s) permitted to use the bots.")
        raise SystemExit(1)

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
