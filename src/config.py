"""YAML config loader with schema validation."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


VALID_PERMISSIONS: frozenset[str] = frozenset({
    "view_channel",
    "send_messages",
    "read_message_history",
    "manage_messages",
    "add_reactions",
    "attach_files",
    "embed_links",
})


def _hex_to_int(value: str | int | None) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value & 0xFFFFFF
    return int(str(value).strip().lstrip("#"), 16)


@dataclass
class PermissionOverride:
    """Permission overrides for one target. Values: True = allow, False = deny."""
    overrides: dict[str, bool] = field(default_factory=dict)


@dataclass
class ChannelConfig:
    name: str
    type: str  # "text" | "voice"
    topic: Optional[str] = None
    permissions: dict[str, PermissionOverride] = field(default_factory=dict)


@dataclass
class CategoryConfig:
    name: str
    position: int
    channels: list[ChannelConfig] = field(default_factory=list)


@dataclass
class RoleConfig:
    name: str
    color: int      # discord colour as integer
    hoist: bool
    mentionable: bool


@dataclass
class ServerConfig:
    guild_id: int
    roles: list[RoleConfig]
    categories: list[CategoryConfig]


# ---------------------------------------------------------------------------
# Internal parsers
# ---------------------------------------------------------------------------

def _parse_permission_block(target: str, raw: dict) -> PermissionOverride:
    unknown = set(raw) - VALID_PERMISSIONS
    if unknown:
        raise ValueError(
            f"Unknown permissions under {target!r}: {unknown}. "
            f"Valid: {sorted(VALID_PERMISSIONS)}"
        )
    bad = {k: v for k, v in raw.items() if not isinstance(v, bool)}
    if bad:
        raise ValueError(
            f"Permission values must be true/false. Bad entries under {target!r}: {bad}"
        )
    return PermissionOverride(overrides=dict(raw))


def _parse_channels(raw_channels: list | None, category_name: str) -> list[ChannelConfig]:
    if not raw_channels:
        return []
    channels: list[ChannelConfig] = []
    for ch in raw_channels:
        if not isinstance(ch, dict) or "name" not in ch:
            raise ValueError(f"Channel entry in '{category_name}' missing 'name'")
        ch_type = ch.get("type", "text")
        if ch_type not in ("text", "voice"):
            raise ValueError(
                f"Channel '{ch['name']}' in '{category_name}': "
                f"type must be 'text' or 'voice', got {ch_type!r}"
            )
        raw_perms: dict = ch.get("permissions") or {}
        permissions: dict[str, PermissionOverride] = {}
        for target, perm_dict in raw_perms.items():
            if not isinstance(perm_dict, dict):
                raise ValueError(
                    f"Channel '{ch['name']}': permissions for {target!r} must be a mapping"
                )
            permissions[target] = _parse_permission_block(target, perm_dict)
        channels.append(ChannelConfig(
            name=ch["name"],
            type=ch_type,
            topic=ch.get("topic"),
            permissions=permissions,
        ))
    return channels


def _parse_roles(raw_roles: list | None) -> list[RoleConfig]:
    if not raw_roles:
        return []
    roles: list[RoleConfig] = []
    for r in raw_roles:
        if not isinstance(r, dict) or "name" not in r:
            raise ValueError("Each role entry must be a mapping with a 'name' field")
        roles.append(RoleConfig(
            name=r["name"],
            color=_hex_to_int(r.get("color", 0)),
            hoist=bool(r.get("hoist", False)),
            mentionable=bool(r.get("mentionable", False)),
        ))
    return roles


def _parse_categories(raw_cats: list | None) -> list[CategoryConfig]:
    if not raw_cats:
        return []
    categories: list[CategoryConfig] = []
    for i, cat in enumerate(raw_cats):
        if not isinstance(cat, dict) or "name" not in cat:
            raise ValueError("Each category entry must be a mapping with a 'name' field")
        categories.append(CategoryConfig(
            name=cat["name"],
            position=cat.get("position", i),
            channels=_parse_channels(cat.get("channels"), cat["name"]),
        ))
    return categories


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_config(path: Path) -> ServerConfig:
    """Load and validate server.yaml. Raises ValueError on schema errors."""
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError("Config file must be a YAML mapping at the top level")

    server_block = raw.get("server")
    if not isinstance(server_block, dict) or "id" not in server_block:
        raise ValueError("Config must have a 'server:' block with an 'id' field")

    raw_id = server_block["id"]
    if str(raw_id) == "PLACEHOLDER_SERVER_ID":
        raise ValueError(
            "Replace 'PLACEHOLDER_SERVER_ID' in config/server.yaml with your actual Discord server ID"
        )
    try:
        guild_id = int(raw_id)
    except (TypeError, ValueError):
        raise ValueError(f"server.id must be an integer snowflake, got {raw_id!r}")

    return ServerConfig(
        guild_id=guild_id,
        roles=_parse_roles(raw.get("roles")),
        categories=_parse_categories(raw.get("categories")),
    )
