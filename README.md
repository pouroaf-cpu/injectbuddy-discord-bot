# injectbuddy-discord-bot

A generic, config-driven Discord server sync bot. Define your server structure in `config/server.yaml` and the bot brings Discord in line with it — idempotently, on every push to `main`.

The only InjectBuddy-specific thing is the config file. Everything else is reusable for any server.

---

## How it works

On each run the bot:
1. Connects to Discord and reads the live guild state
2. Compares it against `config/server.yaml`
3. Prints a diff (`+` create, `~` modify, `-` delete, `=` no change)
4. Applies only the changes needed (roles first, then categories + channels)

Deletes are **never applied** unless `--allow-delete` is explicitly passed, so extra channels or roles you create manually in Discord are left alone.

---

## Prerequisites

### 1. Create a Discord application and bot

1. Go to <https://discord.com/developers/applications> → **New Application**
2. **Bot** tab → **Add Bot** → copy the token (you'll need it later)
3. Under **Privileged Gateway Intents**, enable **Server Members Intent** if you need member-level permission overwrites (not required for the default config)

### 2. Invite the bot to your server

Under **OAuth2 → URL Generator**, select scopes:

- `bot`

Select bot permissions:

- **Manage Roles**
- **Manage Channels**
- **View Channels**

Open the generated URL and invite the bot to your server.

### 3. Get your server (guild) ID

Enable Developer Mode in Discord (**Settings → Advanced → Developer Mode**), then right-click your server → **Copy Server ID**.

---

## Local dev

```bash
# 1. Clone and install
git clone https://github.com/pouroaf-cpu/injectbuddy-discord-bot
cd injectbuddy-discord-bot
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env — set DISCORD_BOT_TOKEN=your_token_here

# 3. Set your server ID
# Edit config/server.yaml — replace PLACEHOLDER_SERVER_ID with your guild ID

# 4. Dry run (safe — prints changes only)
python -m src.sync --dry-run

# 5. Live sync
python -m src.sync

# 6. Live sync with deletes enabled
python -m src.sync --allow-delete
```

---

## Editing the server config

All server structure is defined in `config/server.yaml`:

```yaml
server:
  id: 123456789012345678   # your guild ID

roles:
  - name: My Role
    color: "#FF6B35"
    hoist: true            # show separately in member list
    mentionable: true

categories:
  - name: General
    position: 0
    channels:
      - name: welcome
        type: text
        topic: "Welcome!"
        permissions:
          "@everyone":
            send_messages: false   # read-only channel
          My Role:
            send_messages: true    # role can post
```

Supported permission keys: `view_channel`, `send_messages`, `read_message_history`, `manage_messages`, `add_reactions`, `attach_files`, `embed_links`.

Permission values: `true` = allow, `false` = deny. Omitted = neutral (Discord default).

---

## GitHub Actions

### Auto-sync on push

Any push to `main` that changes a file under `config/` triggers a live sync automatically. No dry-run — changes are applied.

### Manual trigger (dry-run or live)

1. Go to **Actions → Sync Discord Server → Run workflow**
2. Set `dry_run` (`true` = print only, `false` = apply)
3. Set `allow_delete` (`true` = also delete entities not in config)

### Secret setup

Add `DISCORD_BOT_TOKEN` to your repo secrets:
**Settings → Secrets and variables → Actions → New repository secret**

---

## Safety rules

| Rule | Behaviour |
|------|-----------|
| Deletes off by default | Items in Discord but not in config are flagged (`-`) but not removed unless `--allow-delete` |
| Never touch `@everyone` role | Global permissions on `@everyone` are never modified (per-channel overrides are fine) |
| Never delete bot's own role | Roles assigned to the bot are skipped |
| Never delete managed roles | Integration-managed roles (other bots, Nitro boosts) are never touched |
| System channels skipped | Rules channel, community updates channel, system messages channel are never deleted |

---

## Reusing for a new server

1. Fork or clone this repo
2. Rename it (e.g. `myproject-discord-bot`)
3. Replace `config/server.yaml` with your server's structure
4. Add your `DISCORD_BOT_TOKEN` secret to the new repo
5. Update `server.id` with your new guild ID
6. Push to `main` — done
