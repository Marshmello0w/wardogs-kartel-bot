"""Shared, fail-closed checks for commands, components and modals."""
import re
import config


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
    if interaction.guild is not None and is_admin(interaction.user):
        return True
    message = "❌ Du hast keine Berechtigung für diese Aktion."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)
    return False
