# Telegram → Claude Code Bridge

Route Telegram bot messages to Claude Code CLI. **$0 cost** with Claude Max subscription.

Multiple bots → one webhook server → `claude -p` → response back to Telegram.

## Why?

| Approach | Cost per message | Memory |
|----------|-----------------|--------|
| Anthropic API (direct) | $0.01-0.05 | Separate per session |
| OpenClaw / AI agents | $0.01-0.05 | Separate per agent |
| **This bridge + Claude Max** | **$0** | **Shared (Claude Code memory)** |

## How it works

```
Telegram message
    ↓
Tailscale Funnel (or ngrok/Cloudflare Tunnel)
    ↓
webhook_server.py (port 8443)
    ↓ routes by bot token
claude -p "[Agent Name] message" --output-format text
    ↓
Response sent back via Telegram Bot API
```

- **Instant typing indicator** — appears as soon as message is received
- **Parallel processing** — configurable concurrent Claude sessions
- **Multi-bot routing** — each bot maps to a different agent context
- **Conversation history** — saved as JSONL per agent
- **Zero dependencies** — Python stdlib only

## Quick Start

### 1. Prerequisites

- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- Claude Max subscription ($20/mo for unlimited CLI usage)
- Python 3.9+
- One or more Telegram bots (create via [@BotFather](https://t.me/BotFather))

### 2. Configure

```bash
cp config.example.json config.json
# Edit config.json with your bot tokens and Telegram user ID
```

To find your Telegram user ID, message [@userinfobot](https://t.me/userinfobot).

### 3. Start the server

```bash
python3 webhook_server.py
```

### 4. Expose to internet

Telegram needs a public HTTPS URL to send webhooks. Choose one:

**Option A: Tailscale Funnel (recommended — stable, free, no account needed beyond Tailscale)**

```bash
# Enable Funnel in Tailscale Admin Console first
tailscale funnel 8443
```

Your URL: `https://your-machine.tail12345.ts.net`

**Option B: Cloudflare Tunnel**

```bash
cloudflared tunnel --url http://localhost:8443
```

**Option C: ngrok**

```bash
ngrok http 8443
```

### 5. Register webhooks

```bash
chmod +x setup_webhooks.sh
./setup_webhooks.sh https://your-public-url
```

### 6. Test

Send a message to any of your bots in Telegram. You should see:
1. Typing indicator appears
2. Response arrives in ~5-10 seconds

## Running as a service (macOS)

```bash
# Create LaunchAgent for the webhook server
cp launchd/com.telegram-claude-bridge.plist ~/Library/LaunchAgents/
# Edit the plist to match your paths
launchctl load ~/Library/LaunchAgents/com.telegram-claude-bridge.plist

# Create LaunchAgent for Tailscale Funnel
cp launchd/com.telegram-claude-funnel.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.telegram-claude-funnel.plist
```

## Running as a service (Linux)

```bash
# Copy and edit systemd service files
sudo cp systemd/telegram-claude-bridge.service /etc/systemd/system/
sudo systemctl enable telegram-claude-bridge
sudo systemctl start telegram-claude-bridge
```

## Configuration

### config.json

| Field | Description | Default |
|-------|------------|---------|
| `port` | Webhook server port | `8443` |
| `claude_bin` | Path to Claude CLI binary | `claude` |
| `max_parallel_claude` | Max concurrent Claude sessions | `2` |
| `history_dir` | Conversation history directory | `~/.telegram-claude-bridge/history` |
| `allowed_chat_ids` | Telegram user IDs allowed to use bots (**required, non-empty**) | — |
| `agents` | Array of bot configurations | required |

### Agent config

| Field | Description |
|-------|------------|
| `name` | Agent identifier (used in routing and history) |
| `display_name` | Name shown to Claude in the prompt prefix |
| `token` | Telegram bot token from BotFather |

> **Security:** `allowed_chat_ids` must list the Telegram user IDs permitted to
> use the bots, and must not be empty. This bridge runs the Claude CLI on your
> machine, so the server **refuses to start** without an allowlist rather than
> exposing an unrestricted bot to anyone who finds it.

## Architecture

```
┌─────────────┐    webhook     ┌──────────────────┐
│  Telegram    │ ──────────→   │  webhook_server   │
│  Bot API     │ ←──────────   │  (port 8443)      │
└─────────────┘   response     └────────┬─────────┘
                                        │
                          ┌─────────────┼─────────────┐
                          ▼             ▼             ▼
                    ┌──────────┐  ┌──────────┐  ┌──────────┐
                    │ claude -p │  │ claude -p │  │ claude -p │
                    │ Agent 1   │  │ Agent 2   │  │ Agent 3   │
                    └──────────┘  └──────────┘  └──────────┘
                         │             │             │
                         ▼             ▼             ▼
                    ┌─────────────────────────────────────┐
                    │  Claude Code memory / files / tools  │
                    │  (shared across all agents)          │
                    └─────────────────────────────────────┘
```

## Comparison with alternatives

| Feature | This project | Claude Channels | cc-connect | Praktor |
|---------|-------------|----------------|------------|---------|
| Cost | $0 (Max sub) | $0 (Max sub) | $0 (Max sub) | $0 (Max sub) |
| Multi-bot | ✅ | ❌ | ❌ | ✅ |
| No Docker | ✅ | ✅ | ✅ | ❌ |
| No dependencies | ✅ | ❌ (Bun) | ❌ (Go) | ❌ (Go+Docker) |
| Webhook (instant) | ✅ | ❌ (polling) | ✅ | ✅ |
| Shared memory | ✅ | ✅ | ✅ | ❌ (isolated) |
| Tailscale Funnel | ✅ | ❌ | ❌ | ❌ |
| Typing indicator | ✅ | ❌ | ❌ | ❌ |
| History per agent | ✅ | ❌ | ❌ | ✅ |

## License

MIT
