"""Entry point: CLI args, bot setup, two-phase sync orchestration."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import discord
from dotenv import load_dotenv

from .config import load_config, ServerConfig
from .differ import (
    DiffAction,
    SYMBOL_CREATE,
    SYMBOL_MODIFY,
    SYMBOL_DELETE,
    SYMBOL_SAME,
    compute_role_diff,
    compute_structure_diff,
)
from .actions import apply_action


load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logging.Formatter.converter = lambda *_: datetime.now(timezone.utc).timetuple()
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

_ANSI = {
    SYMBOL_CREATE: "\033[32m",   # green
    SYMBOL_MODIFY: "\033[33m",   # yellow
    SYMBOL_DELETE: "\033[31m",   # red
    SYMBOL_SAME:   "\033[90m",   # dark grey
}
_RESET = "\033[0m"


def _print_action(action: DiffAction) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    colour = _ANSI.get(action.symbol, "")
    tag = f"[{action.entity_type}]"
    print(f"{ts}  {colour}{action.symbol}{_RESET}  {tag:<12}  {action.description}")


def _print_summary(counts: dict[str, int], skipped: int, dry_run: bool) -> None:
    total = counts[SYMBOL_CREATE] + counts[SYMBOL_MODIFY] + counts[SYMBOL_DELETE]
    mode = "DRY-RUN — no changes applied" if dry_run else f"{total} change(s) applied"
    log.info(
        "Summary: +%d  ~%d  -%d  =%d  (skipped deletes: %d)  [%s]",
        counts[SYMBOL_CREATE],
        counts[SYMBOL_MODIFY],
        counts[SYMBOL_DELETE],
        counts[SYMBOL_SAME],
        skipped,
        mode,
    )


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class SyncBot(discord.Client):
    def __init__(self, config: ServerConfig, dry_run: bool, allow_delete: bool) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.config = config
        self.dry_run = dry_run
        self.allow_delete = allow_delete
        self.exit_code = 0

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)
        try:
            await self._run_sync()
        except Exception:
            log.exception("Sync failed")
            self.exit_code = 1
        finally:
            await self.close()

    async def _run_sync(self) -> None:
        assert self.user is not None

        guild = self.get_guild(self.config.guild_id)
        if guild is None:
            guild = await self.fetch_guild(self.config.guild_id)
        if guild is None:
            raise RuntimeError(
                f"Guild {self.config.guild_id} not found. "
                "Ensure the bot has been invited to the server."
            )

        log.info(
            "Syncing '%s' (id=%s)  mode=%s  allow-delete=%s",
            guild.name,
            guild.id,
            "DRY-RUN" if self.dry_run else "LIVE",
            self.allow_delete,
        )

        # Fetch fresh data to avoid stale cache
        await guild.fetch_channels()

        counts: dict[str, int] = {
            SYMBOL_CREATE: 0,
            SYMBOL_MODIFY: 0,
            SYMBOL_DELETE: 0,
            SYMBOL_SAME: 0,
        }
        skipped_deletes = 0

        # ── Phase 1: roles ─────────────────────────────────────────────────
        log.info("--- Phase 1: roles ---")
        role_actions = compute_role_diff(guild, self.config, self.user.id, self.allow_delete)
        for action in role_actions:
            _print_action(action)
            if action.skipped:
                skipped_deletes += 1
                continue
            counts[action.symbol] = counts.get(action.symbol, 0) + 1
            if not self.dry_run:
                await apply_action(action, guild)

        # Re-fetch roles so newly created roles are available for permission overwrites
        if not self.dry_run:
            await guild.fetch_roles()

        # ── Phase 2: categories + channels ─────────────────────────────────
        log.info("--- Phase 2: categories + channels ---")
        struct_actions = compute_structure_diff(guild, self.config, self.allow_delete)
        for action in struct_actions:
            _print_action(action)
            if action.skipped:
                skipped_deletes += 1
                continue
            counts[action.symbol] = counts.get(action.symbol, 0) + 1
            if not self.dry_run:
                await apply_action(action, guild)

        _print_summary(counts, skipped_deletes, self.dry_run)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync a Discord server to a YAML config.",
        prog="python -m src.sync",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned changes without applying them (safe)",
    )
    parser.add_argument(
        "--allow-delete",
        action="store_true",
        help="Delete Discord roles/categories/channels not present in config",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/server.yaml"),
        metavar="PATH",
        help="Path to server config YAML (default: config/server.yaml)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        log.error("DISCORD_BOT_TOKEN environment variable is not set")
        sys.exit(1)

    config_path: Path = args.config
    if not config_path.exists():
        log.error("Config file not found: %s", config_path)
        sys.exit(1)

    try:
        config = load_config(config_path)
    except ValueError as exc:
        log.error("Config error: %s", exc)
        sys.exit(1)

    log.info(
        "Config loaded: guild_id=%d  %d roles  %d categories",
        config.guild_id,
        len(config.roles),
        len(config.categories),
    )

    bot = SyncBot(config, dry_run=args.dry_run, allow_delete=args.allow_delete)
    bot.run(token, log_handler=None)
    sys.exit(bot.exit_code)


if __name__ == "__main__":
    main()
