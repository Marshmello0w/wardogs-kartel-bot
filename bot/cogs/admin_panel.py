import discord
from discord.ext import commands
from discord import app_commands
import aiohttp
import logging
import config

class BanModal(discord.ui.Modal, title='Spieler Global Bannen'):
    steam_id = discord.ui.TextInput(
        label='Steam64 ID',
        placeholder='7656119...',
        required=True,
        min_length=17,
        max_length=17
    )
    reason = discord.ui.TextInput(
        label='Ban Grund',
        style=discord.TextStyle.paragraph,
        placeholder='Gibt den Grund für den Ban ein...',
        required=True
    )

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        servers = [
            {"title": "Server 1", "rcon_url": config.SERVER1_RCON_URL, "rcon_pass": config.SERVER1_RCON_PASS},
            {"title": "Server 2", "rcon_url": config.SERVER2_RCON_URL, "rcon_pass": config.SERVER2_RCON_PASS},
            {"title": "Server 3", "rcon_url": config.SERVER3_RCON_URL, "rcon_pass": config.SERVER3_RCON_PASS}
        ]
        
        success_list = []
        error_list = []
        
        async with aiohttp.ClientSession() as session:
            for srv in servers:
                if not srv["rcon_url"] or not srv["rcon_pass"]:
                    continue
                    
                url = f"{srv['rcon_url'].rstrip('/')}/v1/bans"
                headers = {"Authorization": f"Bearer {srv['rcon_pass']}"}
                payload = {"steamId": self.steam_id.value.strip(), "reason": self.reason.value.strip()}
                
                try:
                    async with session.post(url, headers=headers, json=payload, timeout=5) as response:
                        if response.status in [200, 201, 204]:
                            success_list.append(srv["title"])
                        else:
                            error_list.append(f"{srv['title']} (HTTP {response.status})")
                except Exception as e:
                    error_list.append(f"{srv['title']} (Error)")
                    logging.error(f"Error banning on {srv['title']}: {e}")
                    
        # Log via event dispatcher
        log_desc = f"**Admin:** {interaction.user.mention}\n**Aktion:** Globaler Ban\n**SteamID:** `{self.steam_id.value}`\n**Grund:** {self.reason.value}\n\n"
        if success_list:
            log_desc += f"✅ **Erfolgreich auf:** {', '.join(success_list)}\n"
        if error_list:
            log_desc += f"❌ **Fehler auf:** {', '.join(error_list)}"
            
        color = discord.Color.red() if success_list else discord.Color.orange()
        self.bot.dispatch("bot_log", "🔨 Globaler Ban Ausgeführt", log_desc, color)
        
        await interaction.followup.send(f"Bann-Vorgang abgeschlossen! Erfolgreich: {len(success_list)}, Fehler: {len(error_list)}.", ephemeral=True)


class UnbanModal(discord.ui.Modal, title='Spieler Global Entbannen'):
    steam_id = discord.ui.TextInput(
        label='Steam64 ID',
        placeholder='7656119...',
        required=True,
        min_length=17,
        max_length=17
    )

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        servers = [
            {"title": "Server 1", "rcon_url": config.SERVER1_RCON_URL, "rcon_pass": config.SERVER1_RCON_PASS},
            {"title": "Server 2", "rcon_url": config.SERVER2_RCON_URL, "rcon_pass": config.SERVER2_RCON_PASS},
            {"title": "Server 3", "rcon_url": config.SERVER3_RCON_URL, "rcon_pass": config.SERVER3_RCON_PASS}
        ]
        
        success_list = []
        error_list = []
        
        async with aiohttp.ClientSession() as session:
            for srv in servers:
                if not srv["rcon_url"] or not srv["rcon_pass"]:
                    continue
                    
                url = f"{srv['rcon_url'].rstrip('/')}/v1/bans/{self.steam_id.value.strip()}"
                headers = {"Authorization": f"Bearer {srv['rcon_pass']}"}
                
                try:
                    async with session.delete(url, headers=headers, timeout=5) as response:
                        if response.status in [200, 204]:
                            success_list.append(srv["title"])
                        elif response.status == 404:
                            # Not found is technically a success since they are not banned
                            success_list.append(f"{srv['title']} (War nicht gebannt)")
                        else:
                            error_list.append(f"{srv['title']} (HTTP {response.status})")
                except Exception as e:
                    error_list.append(f"{srv['title']} (Error)")
                    logging.error(f"Error unbanning on {srv['title']}: {e}")
                    
        # Log via event dispatcher
        log_desc = f"**Admin:** {interaction.user.mention}\n**Aktion:** Globaler Unban\n**SteamID:** `{self.steam_id.value}`\n\n"
        if success_list:
            log_desc += f"✅ **Erfolgreich auf:** {', '.join(success_list)}\n"
        if error_list:
            log_desc += f"❌ **Fehler auf:** {', '.join(error_list)}"
            
        color = discord.Color.green() if success_list else discord.Color.orange()
        self.bot.dispatch("bot_log", "🕊️ Globaler Unban Ausgeführt", log_desc, color)
        
        await interaction.followup.send(f"Entbann-Vorgang abgeschlossen! Erfolgreich: {len(success_list)}, Fehler: {len(error_list)}.", ephemeral=True)


class AdminPanelView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Spieler Bannen", style=discord.ButtonStyle.danger, custom_id="admin_panel_ban", emoji="🔨")
    async def ban_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # We can add an extra permission check here if we want, but the view should only be in an admin channel.
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ Du hast keine Berechtigung für diese Aktion.", ephemeral=True)
            
        await interaction.response.send_modal(BanModal(self.bot))

    @discord.ui.button(label="Spieler Entbannen", style=discord.ButtonStyle.success, custom_id="admin_panel_unban", emoji="🕊️")
    async def unban_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ Du hast keine Berechtigung für diese Aktion.", ephemeral=True)
            
        await interaction.response.send_modal(UnbanModal(self.bot))


class AdminPanelCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="adminpanel", description="Sendet das globale Admin-Panel zum Bannen/Entbannen von Spielern.")
    @app_commands.default_permissions(administrator=True)
    async def admin_panel(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🛡️ Kartell Global Admin Panel",
            description=(
                "Über dieses Panel können Spieler **gleichzeitig auf allen Servern** (Server 1, Server 2 & Server 3) gebannt oder entbannt werden.\\n\\n"
                "**Hinweis:**\\n"
                "• Du benötigst die 17-stellige **Steam64 ID** des Spielers.\\n"
                "• Alle Aktionen werden zentral in den Logs aufgezeichnet."
            ),
            color=discord.Color.dark_theme()
        )
        embed.set_thumbnail(url=interaction.guild.icon.url if interaction.guild.icon else None)
        
        view = AdminPanelView(self.bot)
        await interaction.response.send_message(embed=embed, view=view)


async def setup(bot):
    await bot.add_cog(AdminPanelCog(bot))
