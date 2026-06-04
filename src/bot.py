"""Persistent Discord bot: member events, verification webhook, #verify pin."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

import discord
from aiohttp import web
from dotenv import load_dotenv

from .config import load_config
from .differ import (
    compute_role_diff,
    compute_structure_diff,
    SYMBOL_CREATE,
    SYMBOL_MODIFY,
    SYMBOL_DELETE,
    SYMBOL_SAME,
)
from .actions import apply_action


load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config from env
# ---------------------------------------------------------------------------
TOKEN             = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
GUILD_ID          = int(os.environ.get("DISCORD_GUILD_ID", "0"))
WEBHOOK_SECRET    = os.environ.get("BOT_WEBHOOK_SECRET", "").strip()
PORT              = int(os.environ.get("PORT", "8080"))
DB_PATH           = Path(os.environ.get("DB_PATH", "data/mappings.db"))

WELCOME_CHANNEL   = "welcome"
VERIFY_CHANNEL    = "verify"
RULES_CHANNEL     = "rules"
GENERAL_CHANNEL   = "general"
MEMBER_ROLE       = "Member"
VERIFIED_ROLE     = "Verified"
VERIFY_MESSAGE    = (
    "## Verify your InjectBuddy account\n\n"
    "Link your InjectBuddy account to unlock the community.\n\n"
    "**→ https://www.injectbuddy.com/discord-link/**\n\n"
    "*Takes 30 seconds. Your health data stays on InjectBuddy — "
    "we only confirm you have an account.*"
)

# ---------------------------------------------------------------------------
# Mapping storage (discord_id ↔ supabase_user_id)
# ---------------------------------------------------------------------------

def _init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS verified_users (
                discord_id       TEXT PRIMARY KEY,
                supabase_user_id TEXT NOT NULL,
                discord_username TEXT,
                linked_at        TEXT DEFAULT (datetime('now'))
            )
        """)


def _store_mapping(discord_id: str, supabase_user_id: str, discord_username: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO verified_users "
            "(discord_id, supabase_user_id, discord_username) VALUES (?, ?, ?)",
            (discord_id, supabase_user_id, discord_username),
        )


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class VerificationBot(discord.Client):

    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds  = True
        intents.members = True   # privileged — must be enabled in Dev Portal
        super().__init__(intents=intents)

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)
        guild = self.get_guild(GUILD_ID) or await self.fetch_guild(GUILD_ID)
        if guild is None:
            log.error("Guild %s not found", GUILD_ID)
            return
        await self._run_yaml_sync(guild)
        await self._ensure_welcome_pin(guild)
        # Verification is currently disabled — access is open to everyone.
        # Re-enable by uncommenting the line below (and re-gating channels in server.yaml).
        # await self._ensure_verify_pin(guild)

    async def _run_yaml_sync(self, guild: discord.Guild) -> None:
        config_path = Path(os.environ.get("CONFIG_PATH", "config/server.yaml"))
        if not config_path.exists():
            log.warning("server.yaml not found at %s — skipping sync", config_path)
            return
        try:
            config = load_config(config_path)
        except ValueError as exc:
            log.error("Config error: %s", exc)
            return

        log.info("YAML sync starting on '%s'", guild.name)
        await guild.fetch_channels()

        counts = {SYMBOL_CREATE: 0, SYMBOL_MODIFY: 0, SYMBOL_DELETE: 0, SYMBOL_SAME: 0}

        for action in compute_role_diff(guild, config, self.user.id, allow_delete=False):
            if not action.skipped:
                counts[action.symbol] = counts.get(action.symbol, 0) + 1
                await apply_action(action, guild)
                log.info("%s [role] %s", action.symbol, action.description)

        await guild.fetch_roles()

        for action in compute_structure_diff(guild, config, allow_delete=False):
            if not action.skipped:
                counts[action.symbol] = counts.get(action.symbol, 0) + 1
                await apply_action(action, guild)
                log.info("%s [channel] %s", action.symbol, action.description)

        summary = (
            f"YAML sync complete — "
            f"+{counts[SYMBOL_CREATE]} created  "
            f"~{counts[SYMBOL_MODIFY]} updated  "
            f"={counts[SYMBOL_SAME]} unchanged"
        )
        log.info(summary)

        mod_log = discord.utils.get(guild.text_channels, name="mod-log")
        if mod_log:
            await mod_log.send(f"🔧 {summary}")

    # ── events ─────────────────────────────────────────────────────────────

    async def on_member_join(self, member: discord.Member) -> None:
        if member.guild.id != GUILD_ID:
            return
        role = discord.utils.get(member.guild.roles, name=MEMBER_ROLE)
        if role:
            await member.add_roles(role, reason="auto-assign on join")
            log.info("Assigned @%s to %s (%s)", MEMBER_ROLE, member.name, member.id)
        else:
            log.warning("@%s role not found in guild — did the sync bot run?", MEMBER_ROLE)

    # ── verification ───────────────────────────────────────────────────────

    async def verify_member(
        self,
        discord_id: str,
        supabase_user_id: str,
        discord_username: str,
    ) -> tuple[bool, str]:
        """Assign @Verified, post welcome, store mapping. Returns (ok, message)."""
        guild = self.get_guild(GUILD_ID)
        if guild is None:
            return False, "Guild not cached"

        try:
            member = guild.get_member(int(discord_id)) or await guild.fetch_member(int(discord_id))
        except discord.NotFound:
            return False, f"Member {discord_id} not in guild"
        except discord.HTTPException as exc:
            return False, f"Discord error: {exc}"

        verified_role = discord.utils.get(guild.roles, name=VERIFIED_ROLE)
        if verified_role is None:
            return False, f"@{VERIFIED_ROLE} role not found — run the sync bot first"

        await member.add_roles(verified_role, reason="verified via injectbuddy.com")
        log.info("Assigned @%s to %s (%s)", VERIFIED_ROLE, member.name, discord_id)

        _store_mapping(discord_id, supabase_user_id, discord_username)

        general = discord.utils.get(guild.text_channels, name=GENERAL_CHANNEL)
        if general:
            await general.send(
                f"Welcome {member.mention} 🎯 fresh in from InjectBuddy. "
                f"Drop an intro in <#{ await _find_channel_id(guild, 'introductions') }>."
            )

        return True, "ok"

    # ── #welcome pin ─────────────────────────────────────────────────────────

    async def _ensure_welcome_pin(self, guild: discord.Guild) -> None:
        channel = discord.utils.get(guild.text_channels, name=WELCOME_CHANNEL)
        if channel is None:
            log.warning("#%s channel not found — cannot pin welcome message", WELCOME_CHANNEL)
            return

        pins = await channel.pins()
        assert self.user is not None
        already_pinned = any(m.author.id == self.user.id for m in pins)
        if already_pinned:
            log.info("#%s already has a pinned bot message — skipping", WELCOME_CHANNEL)
            return

        general_id = await _find_channel_id(guild, GENERAL_CHANNEL)
        rules_id   = await _find_channel_id(guild, RULES_CHANNEL)
        general_ref = f"<#{general_id}>" if general_id else "#general"
        rules_ref   = f"<#{rules_id}>"   if rules_id   else "#rules"

        message = (
            "# Welcome to InjectBuddy 👋\n\n"
            "You're in. This is the community for people running TRT, peptides, "
            "and GLP-1 protocols — sharing dosing, bloodwork, and real-world results.\n\n"
            f"Jump into {general_ref} to say hi, and read {rules_ref} while you're here."
        )

        msg = await channel.send(message)
        await msg.pin(reason="welcome prompt")
        log.info("Pinned welcome message in #%s", WELCOME_CHANNEL)

    # ── #verify pin ────────────────────────────────────────────────────────

    async def _ensure_verify_pin(self, guild: discord.Guild) -> None:
        channel = discord.utils.get(guild.text_channels, name=VERIFY_CHANNEL)
        if channel is None:
            log.warning("#%s channel not found — cannot pin verify message", VERIFY_CHANNEL)
            return

        pins = await channel.pins()
        assert self.user is not None
        already_pinned = any(m.author.id == self.user.id for m in pins)
        if already_pinned:
            log.info("#verify already has a pinned bot message — skipping")
            return

        msg = await channel.send(VERIFY_MESSAGE)
        await msg.pin(reason="verification prompt")
        log.info("Pinned verification message in #%s", VERIFY_CHANNEL)


async def _find_channel_id(guild: discord.Guild, name: str) -> int:
    ch = discord.utils.get(guild.text_channels, name=name)
    return ch.id if ch else 0


# ---------------------------------------------------------------------------
# aiohttp webhook server
# ---------------------------------------------------------------------------

def _build_app(bot: VerificationBot) -> web.Application:
    app = web.Application()
    app.router.add_post("/api/verified", _handle_verified(bot))
    app.router.add_get("/healthz", _handle_health)
    return app


def _handle_verified(bot: VerificationBot):
    async def handler(request: web.Request) -> web.Response:
        secret = request.headers.get("X-Webhook-Secret", "")
        if not WEBHOOK_SECRET or secret != WEBHOOK_SECRET:
            log.warning("Rejected /api/verified — bad secret")
            return web.Response(status=401, text="Unauthorized")

        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400, text="Invalid JSON")

        discord_id       = str(body.get("discord_id", "")).strip()
        supabase_user_id = str(body.get("supabase_user_id", "")).strip()
        discord_username = str(body.get("discord_username", "")).strip()

        if not discord_id or not supabase_user_id:
            return web.Response(status=422, text="Missing discord_id or supabase_user_id")

        ok, message = await bot.verify_member(discord_id, supabase_user_id, discord_username)
        if ok:
            return web.json_response({"status": "ok"})
        else:
            log.error("verify_member failed: %s", message)
            return web.json_response({"status": "error", "detail": message}, status=500)

    return handler


async def _handle_health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _runner() -> None:
    _init_db()

    bot = VerificationBot()

    # aiohttp runner shares the same event loop as discord.py
    app = _build_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("Webhook server listening on port %s", PORT)

    await bot.start(TOKEN)


def main() -> None:
    if not TOKEN:
        log.error("DISCORD_BOT_TOKEN not set")
        raise SystemExit(1)
    if not GUILD_ID:
        log.error("DISCORD_GUILD_ID not set")
        raise SystemExit(1)

    import asyncio
    asyncio.run(_runner())


if __name__ == "__main__":
    main()
