import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp
import logging
import config
import json
import os

PENDING_ACTIONS_FILE = "pending_admin_actions.json"

def load_pending_actions():
    if os.path.exists(PENDING_ACTIONS_FILE):
        try:
            with open(PENDING_ACTIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Failed to load pending admin actions: {e}")
    return []

def save_pending_actions(actions):
    try:
        with open(PENDING_ACTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(actions, f)
    except Exception as e:
        logging.error(f"Failed to save pending admin actions: {e}")

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
                            self.bot.get_cog("AdminPanelCog").add_pending_action("ban", srv["id"], self.steam_id.value.strip(), self.reason.value.strip(), interaction.user.mention)
                except Exception as e:
                    error_list.append(f"{srv['title']} (Offline)")
                    self.bot.get_cog("AdminPanelCog").add_pending_action("ban", srv["id"], self.steam_id.value.strip(), self.reason.value.strip(), interaction.user.mention)
                    logging.warning(f"Server {srv['title']} offline, queued ban.")
                    
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
                            success_list.append(f"{srv['title']} (War nicht gebannt)")
                        else:
                            error_list.append(f"{srv['title']} (HTTP {response.status})")
                            self.bot.get_cog("AdminPanelCog").add_pending_action("unban", srv["id"], self.steam_id.value.strip(), "", interaction.user.mention)
                except Exception as e:
                    error_list.append(f"{srv['title']} (Offline)")
                    self.bot.get_cog("AdminPanelCog").add_pending_action("unban", srv["id"], self.steam_id.value.strip(), "", interaction.user.mention)
                    logging.warning(f"Server {srv['title']} offline, queued unban.")
                    
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
        self.pending_actions = load_pending_actions()
        self.process_pending_actions.start()

    def cog_unload(self):
        self.process_pending_actions.cancel()

    def add_pending_action(self, action_type, server_id, steam_id, reason, admin_mention):
        # Prevent duplicates
        for action in self.pending_actions:
            if action["action"] == action_type and action["server_id"] == server_id and action["steam_id"] == steam_id:
                return
                
        self.pending_actions.append({
            "action": action_type,
            "server_id": server_id,
            "steam_id": steam_id,
            "reason": reason,
            "admin_mention": admin_mention
        })
        save_pending_actions(self.pending_actions)

    @tasks.loop(minutes=5)
    async def process_pending_actions(self):
        if not self.pending_actions:
            return
            
        servers = {
            "server1": {"title": "Server 1", "url": config.SERVER1_RCON_URL, "pass": config.SERVER1_RCON_PASS},
            "server2": {"title": "Server 2", "url": config.SERVER2_RCON_URL, "pass": config.SERVER2_RCON_PASS},
            "server3": {"title": "Server 3", "url": config.SERVER3_RCON_URL, "pass": config.SERVER3_RCON_PASS}
        }
        
        remaining_actions = []
        actions_to_process = list(self.pending_actions)
        
        async with aiohttp.ClientSession() as session:
            for action in actions_to_process:
                srv_id = action["server_id"]
                srv = servers.get(srv_id)
                if not srv or not srv["url"]:
                    continue # Drop invalid actions
                    
                headers = {"Authorization": f"Bearer {srv['pass']}"}
                success = False
                
                if action["action"] == "ban":
                    url = f"{srv['url'].rstrip('/')}/v1/bans"
                    payload = {"steamId": action["steam_id"], "reason": action["reason"]}
                    try:
                        async with session.post(url, headers=headers, json=payload, timeout=5) as response:
                            if response.status in [200, 201, 204]:
                                success = True
                    except: pass
                
                elif action["action"] == "unban":
                    url = f"{srv['url'].rstrip('/')}/v1/bans/{action['steam_id']}"
                    try:
                        async with session.delete(url, headers=headers, timeout=5) as response:
                            if response.status in [200, 204, 404]:
                                success = True
                    except: pass
                
                if success:
                    # Notify that it finally worked
                    title_str = "🔨 Verzögerter Ban Erfolgreich" if action["action"] == "ban" else "🕊️ Verzögerter Unban Erfolgreich"
                    color = discord.Color.green()
                    desc = f"**Admin:** {action['admin_mention']}\n**Aktion:** {action['action'].capitalize()}\n**SteamID:** `{action['steam_id']}`\n**Server:** {srv['title']}\n\nDie zuvor fehlgeschlagene Aktion konnte nun erfolgreich auf dem Server ausgeführt werden!"
                    self.bot.dispatch("bot_log", title_str, desc, color)
                else:
                    # Keep it in the queue to try again in 5 minutes
                    remaining_actions.append(action)
                    
        if len(remaining_actions) != len(self.pending_actions):
            self.pending_actions = remaining_actions
            save_pending_actions(self.pending_actions)

    @process_pending_actions.before_loop
    async def before_process(self):
        await self.bot.wait_until_ready()

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
