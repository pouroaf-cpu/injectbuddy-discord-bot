"""Applies DiffActions to the live Discord guild via discord.py."""
from __future__ import annotations

import discord

from .differ import DiffAction

_REASON = "config sync"


async def apply_action(action: DiffAction, guild: discord.Guild) -> None:
    """Execute a single DiffAction. Noops and skipped deletes are ignored."""
    if action.kind == "noop" or action.skipped:
        return

    handlers = {
        "create_role":     _create_role,
        "modify_role":     _modify_role,
        "delete_role":     _delete_role,
        "create_category": _create_category,
        "modify_category": _modify_category,
        "delete_category": _delete_category,
        "create_channel":  _create_channel,
        "modify_channel":  _modify_channel,
        "delete_channel":  _delete_channel,
    }
    handler = handlers.get(action.kind)
    if handler is None:
        raise ValueError(f"Unknown action kind: {action.kind!r}")
    await handler(action, guild)


# ---------------------------------------------------------------------------
# Role handlers
# ---------------------------------------------------------------------------

async def _create_role(action: DiffAction, guild: discord.Guild) -> None:
    cfg = action.payload["config"]
    await guild.create_role(
        name=cfg.name,
        colour=discord.Colour(cfg.color),
        hoist=cfg.hoist,
        mentionable=cfg.mentionable,
        reason=_REASON,
    )


async def _modify_role(action: DiffAction, guild: discord.Guild) -> None:
    role: discord.Role = action.payload["role"]
    cfg = action.payload["config"]
    await role.edit(
        name=cfg.name,
        colour=discord.Colour(cfg.color),
        hoist=cfg.hoist,
        mentionable=cfg.mentionable,
        reason=_REASON,
    )


async def _delete_role(action: DiffAction, guild: discord.Guild) -> None:
    role: discord.Role = action.payload["role"]
    await role.delete(reason=_REASON)


# ---------------------------------------------------------------------------
# Category handlers
# ---------------------------------------------------------------------------

async def _create_category(action: DiffAction, guild: discord.Guild) -> None:
    cfg = action.payload["config"]
    await guild.create_category(
        name=cfg.name,
        position=cfg.position,
        reason=_REASON,
    )


async def _modify_category(action: DiffAction, guild: discord.Guild) -> None:
    category: discord.CategoryChannel = action.payload["category"]
    cfg = action.payload["config"]
    await category.edit(
        name=cfg.name,
        position=cfg.position,
        reason=_REASON,
    )


async def _delete_category(action: DiffAction, guild: discord.Guild) -> None:
    category: discord.CategoryChannel = action.payload["category"]
    await category.delete(reason=_REASON)


# ---------------------------------------------------------------------------
# Channel handlers
# ---------------------------------------------------------------------------

async def _create_channel(action: DiffAction, guild: discord.Guild) -> None:
    cfg = action.payload["config"]
    category_name: str = action.payload["category_name"]
    desired_overwrites: dict = action.payload.get("desired_overwrites") or {}

    # Category may have been created earlier in this same sync pass
    category = discord.utils.get(guild.categories, name=category_name)

    kwargs = dict(
        name=cfg.name,
        category=category,
        overwrites=desired_overwrites,
        reason=_REASON,
    )
    if cfg.type == "text":
        if cfg.topic:
            kwargs["topic"] = cfg.topic
        await guild.create_text_channel(**kwargs)
    else:
        await guild.create_voice_channel(**kwargs)


async def _modify_channel(action: DiffAction, guild: discord.Guild) -> None:
    channel = action.payload["channel"]
    cfg = action.payload["config"]
    category_name: str = action.payload["category_name"]
    desired_overwrites: dict = action.payload.get("desired_overwrites") or {}

    category = discord.utils.get(guild.categories, name=category_name)

    kwargs: dict = dict(
        category=category,
        overwrites=desired_overwrites,
        reason=_REASON,
    )
    if isinstance(channel, discord.TextChannel):
        kwargs["topic"] = cfg.topic or ""

    await channel.edit(**kwargs)
