"""Shared, fail-closed checks for commands, components and modals."""
import re
from core import config


def is_admin(member):
    permissions = getattr(member, "guild_permissions", None)
    return bool(permissions and (
        permissions.administrator or any(
            role.id in config.ADMIN_ROLE_IDS for role in getattr(member, "roles", ())
        )
    ))


def valid_steam_id(value):
    return bool(re.fullmatch(r"[0-9]{17}", value))


async def require_admin(interaction):
    configured_guild = str(config.GUILD_ID or '')
    interaction_guild = getattr(interaction, 'guild_id', None)
    if interaction_guild is None and getattr(interaction, 'guild', None) is not None:
        interaction_guild = getattr(interaction.guild, 'id', None)
    if configured_guild and str(interaction_guild or '') == configured_guild and is_admin(interaction.user):
        return True
    message = "❌ Du hast keine Berechtigung für diese Aktion."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)
    return False
