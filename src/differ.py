"""Computes the diff between live Discord guild state and YAML config."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import discord

from .config import CategoryConfig, ChannelConfig, PermissionOverride, RoleConfig, ServerConfig


SYMBOL_CREATE = "+"
SYMBOL_MODIFY = "~"
SYMBOL_DELETE = "-"
SYMBOL_SAME   = "="


@dataclass
class DiffAction:
    symbol: str           # +  ~  -  =
    entity_type: str      # role | category | channel
    name: str
    description: str
    kind: str             # create_role | modify_role | delete_role | noop | ...
    payload: dict[str, Any] = field(default_factory=dict)
    skipped: bool = False  # True = delete suppressed (no --allow-delete)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _role_by_name(guild: discord.Guild, name: str) -> Optional[discord.Role]:
    return discord.utils.get(guild.roles, name=name)


def _category_by_name(guild: discord.Guild, name: str) -> Optional[discord.CategoryChannel]:
    return discord.utils.get(guild.categories, name=name)


def _channel_by_name(
    guild: discord.Guild,
    name: str,
    category: Optional[discord.CategoryChannel] = None,
) -> Optional[discord.TextChannel | discord.VoiceChannel]:
    """Find a text or voice channel by name; optionally filter by category."""
    for ch in guild.channels:
        if not isinstance(ch, (discord.TextChannel, discord.VoiceChannel)):
            continue
        if ch.name != name:
            continue
        if category is not None and ch.category != category:
            continue
        return ch
    return None


def _build_desired_overwrites(
    permissions: dict[str, PermissionOverride],
    guild: discord.Guild,
) -> dict[discord.Role, discord.PermissionOverwrite]:
    """Convert config permission block to discord.py PermissionOverwrite dict.

    Roles not yet present in the guild are silently skipped; they will be
    applied on the next sync once the role creation action runs.
    """
    result: dict[discord.Role, discord.PermissionOverwrite] = {}
    for target_name, override in permissions.items():
        target: Optional[discord.Role]
        if target_name == "@everyone":
            target = guild.default_role
        else:
            target = _role_by_name(guild, target_name)
            if target is None:
                continue
        ow = discord.PermissionOverwrite()
        for perm_name, value in override.overrides.items():
            setattr(ow, perm_name, value)
        result[target] = ow
    return result


def _overwrites_match(
    current: dict[discord.Role, discord.PermissionOverwrite],
    desired: dict[discord.Role, discord.PermissionOverwrite],
) -> bool:
    """Return True if every desired overwrite is present and equal in current."""
    for target, desired_ow in desired.items():
        current_ow = current.get(target)
        if current_ow is None:
            return False
        allow_c, deny_c = current_ow.pair()
        allow_d, deny_d = desired_ow.pair()
        if allow_c.value != allow_d.value or deny_c.value != deny_d.value:
            return False
    # Flag extra overwrites in current that aren't in desired
    if set(current.keys()) != set(desired.keys()):
        return False
    return True


def _system_channel_ids(guild: discord.Guild) -> set[int]:
    ids: set[int] = set()
    for attr in ("system_channel", "rules_channel", "public_updates_channel"):
        ch = getattr(guild, attr, None)
        if ch is not None:
            ids.add(ch.id)
    return ids


# ---------------------------------------------------------------------------
# Role diff
# ---------------------------------------------------------------------------

def compute_role_diff(
    guild: discord.Guild,
    config: ServerConfig,
    bot_id: int,
    allow_delete: bool,
) -> list[DiffAction]:
    """Return DiffActions for roles only."""
    actions: list[DiffAction] = []
    config_names = {r.name for r in config.roles}

    # Bot's own roles — never delete these
    bot_member = guild.get_member(bot_id)
    bot_role_ids = {r.id for r in bot_member.roles} if bot_member else set()

    for rc in config.roles:
        existing = _role_by_name(guild, rc.name)
        if existing is None:
            actions.append(DiffAction(
                symbol=SYMBOL_CREATE,
                entity_type="role",
                name=rc.name,
                description=f"create role '{rc.name}'",
                kind="create_role",
                payload={"config": rc},
            ))
        else:
            changes: list[str] = []
            if existing.colour.value != rc.color:
                changes.append(f"colour #{existing.colour.value:06X}→#{rc.color:06X}")
            if existing.hoist != rc.hoist:
                changes.append(f"hoist {existing.hoist}→{rc.hoist}")
            if existing.mentionable != rc.mentionable:
                changes.append(f"mentionable {existing.mentionable}→{rc.mentionable}")
            if changes:
                actions.append(DiffAction(
                    symbol=SYMBOL_MODIFY,
                    entity_type="role",
                    name=rc.name,
                    description=f"modify role '{rc.name}': {', '.join(changes)}",
                    kind="modify_role",
                    payload={"role": existing, "config": rc},
                ))
            else:
                actions.append(DiffAction(
                    symbol=SYMBOL_SAME,
                    entity_type="role",
                    name=rc.name,
                    description=f"role '{rc.name}' — no change",
                    kind="noop",
                ))

    for role in guild.roles:
        if role.name == "@everyone":
            continue
        if role.managed:          # integration / bot-assigned roles
            continue
        if role.id in bot_role_ids:
            continue
        if role.name in config_names:
            continue
        skipped = not allow_delete
        actions.append(DiffAction(
            symbol=SYMBOL_DELETE,
            entity_type="role",
            name=role.name,
            description=(
                f"delete role '{role.name}'"
                if not skipped
                else f"role '{role.name}' not in config (pass --allow-delete to remove)"
            ),
            kind="delete_role",
            payload={"role": role},
            skipped=skipped,
        ))

    return actions


# ---------------------------------------------------------------------------
# Category + channel diff
# ---------------------------------------------------------------------------

def compute_structure_diff(
    guild: discord.Guild,
    config: ServerConfig,
    allow_delete: bool,
) -> list[DiffAction]:
    """Return DiffActions for categories and channels."""
    actions: list[DiffAction] = []
    config_category_names = {c.name for c in config.categories}
    config_channel_names = {ch.name for cat in config.categories for ch in cat.channels}
    system_ids = _system_channel_ids(guild)

    for cat_cfg in config.categories:
        existing_cat = _category_by_name(guild, cat_cfg.name)

        if existing_cat is None:
            actions.append(DiffAction(
                symbol=SYMBOL_CREATE,
                entity_type="category",
                name=cat_cfg.name,
                description=f"create category '{cat_cfg.name}' (position {cat_cfg.position})",
                kind="create_category",
                payload={"config": cat_cfg},
            ))
        else:
            changes: list[str] = []
            if existing_cat.position != cat_cfg.position:
                changes.append(f"position {existing_cat.position}→{cat_cfg.position}")
            if changes:
                actions.append(DiffAction(
                    symbol=SYMBOL_MODIFY,
                    entity_type="category",
                    name=cat_cfg.name,
                    description=f"modify category '{cat_cfg.name}': {', '.join(changes)}",
                    kind="modify_category",
                    payload={"category": existing_cat, "config": cat_cfg},
                ))
            else:
                actions.append(DiffAction(
                    symbol=SYMBOL_SAME,
                    entity_type="category",
                    name=cat_cfg.name,
                    description=f"category '{cat_cfg.name}' — no change",
                    kind="noop",
                ))

        for ch_cfg in cat_cfg.channels:
            desired_overwrites = _build_desired_overwrites(ch_cfg.permissions, guild)

            # Look in the correct category first; fall back to anywhere
            existing_ch = (
                _channel_by_name(guild, ch_cfg.name, existing_cat)
                if existing_cat is not None
                else _channel_by_name(guild, ch_cfg.name)
            )
            if existing_ch is None and existing_cat is not None:
                existing_ch = _channel_by_name(guild, ch_cfg.name)

            if existing_ch is None:
                actions.append(DiffAction(
                    symbol=SYMBOL_CREATE,
                    entity_type="channel",
                    name=ch_cfg.name,
                    description=f"create #{ch_cfg.name} in '{cat_cfg.name}'",
                    kind="create_channel",
                    payload={
                        "config": ch_cfg,
                        "category_name": cat_cfg.name,
                        "desired_overwrites": desired_overwrites,
                    },
                ))
            else:
                changes = []
                if isinstance(existing_ch, discord.TextChannel):
                    if (existing_ch.topic or "") != (ch_cfg.topic or ""):
                        changes.append("topic")
                current_cat_name = existing_ch.category.name if existing_ch.category else None
                if current_cat_name != cat_cfg.name:
                    changes.append(f"category {current_cat_name!r}→{cat_cfg.name!r}")
                current_overwrites = dict(existing_ch.overwrites)
                if not _overwrites_match(current_overwrites, desired_overwrites):
                    changes.append("permission overwrites")

                if changes:
                    actions.append(DiffAction(
                        symbol=SYMBOL_MODIFY,
                        entity_type="channel",
                        name=ch_cfg.name,
                        description=f"modify #{ch_cfg.name}: {', '.join(changes)}",
                        kind="modify_channel",
                        payload={
                            "channel": existing_ch,
                            "config": ch_cfg,
                            "category_name": cat_cfg.name,
                            "desired_overwrites": desired_overwrites,
                        },
                    ))
                else:
                    actions.append(DiffAction(
                        symbol=SYMBOL_SAME,
                        entity_type="channel",
                        name=ch_cfg.name,
                        description=f"#{ch_cfg.name} — no change",
                        kind="noop",
                    ))

    # Channels in Discord not in config
    for ch in guild.channels:
        if not isinstance(ch, (discord.TextChannel, discord.VoiceChannel)):
            continue
        if ch.id in system_ids:
            continue
        if ch.name in config_channel_names:
            continue
        skipped = not allow_delete
        actions.append(DiffAction(
            symbol=SYMBOL_DELETE,
            entity_type="channel",
            name=ch.name,
            description=(
                f"delete #{ch.name}"
                if not skipped
                else f"#{ch.name} not in config (pass --allow-delete to remove)"
            ),
            kind="delete_channel",
            payload={"channel": ch},
            skipped=skipped,
        ))

    # Categories in Discord not in config
    for cat in guild.categories:
        if cat.name in config_category_names:
            continue
        skipped = not allow_delete
        actions.append(DiffAction(
            symbol=SYMBOL_DELETE,
            entity_type="category",
            name=cat.name,
            description=(
                f"delete category '{cat.name}'"
                if not skipped
                else f"category '{cat.name}' not in config (pass --allow-delete to remove)"
            ),
            kind="delete_category",
            payload={"category": cat},
            skipped=skipped,
        ))

    return actions
