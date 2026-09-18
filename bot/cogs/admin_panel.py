import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp
import aiomysql
import logging
import config
import json
import os
import database
from datetime import datetime, timedelta

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

    def __init__(self, bot, duration_value, duration_label):
        super().__init__()
        self.bot = bot
        self.duration_value = duration_value
        self.duration_label = duration_label

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        servers = [
            {"id": "server1", "title": "Server 1", "rcon_url": config.SERVER1_RCON_URL, "rcon_pass": config.SERVER1_RCON_PASS},
            {"id": "server2", "title": "Server 2", "rcon_url": config.SERVER2_RCON_URL, "rcon_pass": config.SERVER2_RCON_PASS},
            {"id": "server3", "title": "Server 3", "rcon_url": config.SERVER3_RCON_URL, "rcon_pass": config.SERVER3_RCON_PASS}
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
                        elif response.status == 404:
                            # Wardogs API blocks banning offline players. We queue it until they join!
                            error_list.append(f"{srv['title']} (Offline - Wird gebannt sobald online)")
                            self.bot.get_cog("AdminPanelCog").add_pending_action("ban", srv["id"], self.steam_id.value.strip(), self.reason.value.strip(), interaction.user.mention)
                        else:
                            error_list.append(f"{srv['title']} (HTTP {response.status})")
                            self.bot.get_cog("AdminPanelCog").add_pending_action("ban", srv["id"], self.steam_id.value.strip(), self.reason.value.strip(), interaction.user.mention)
                except Exception as e:
                    error_list.append(f"{srv['title']} (Offline)")
                    self.bot.get_cog("AdminPanelCog").add_pending_action("ban", srv["id"], self.steam_id.value.strip(), self.reason.value.strip(), interaction.user.mention)
                    logging.warning(f"Server {srv['title']} offline, queued ban.")
                    
        # Calculate expiration
        expires_at = None
        if self.duration_value > 0:
            expires_at = datetime.utcnow() + timedelta(hours=self.duration_value)
            
        # Record into database
        try:
            pool = await database.get_db_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    # Mark any existing active bans for this player as revoked since we're overwriting
                    await cur.execute("UPDATE global_bans SET status = 'revoked' WHERE steam_id = %s AND status = 'active'", (self.steam_id.value.strip(),))
                    
                    await cur.execute(
                        "INSERT INTO global_bans (steam_id, reason, admin_mention, duration_str, expires_at) VALUES (%s, %s, %s, %s, %s)",
                        (self.steam_id.value.strip(), self.reason.value.strip(), interaction.user.mention, self.duration_label, expires_at)
                    )
                await conn.commit()
        except Exception as e:
            logging.error(f"Failed to record ban to database: {e}")

        # Log via event dispatcher
        log_desc = f"**Admin:** {interaction.user.mention}\n**Aktion:** Globaler Ban\n**SteamID:** `{self.steam_id.value}`\n**Dauer:** {self.duration_label}\n**Grund:** {self.reason.value}\n\n"
        if success_list:
            log_desc += f"✅ **Erfolgreich auf:** {', '.join(success_list)}\n"
        if error_list:
            log_desc += f"❌ **Fehler auf:** {', '.join(error_list)}"
            
        color = discord.Color.red() if success_list else discord.Color.orange()
        self.bot.dispatch("bot_log", "🔨 Globaler Ban Ausgeführt", log_desc, color)
        
        await interaction.followup.send(f"Bann-Vorgang abgeschlossen! Erfolgreich: {len(success_list)}, Fehler: {len(error_list)}.", ephemeral=True)


class BanDurationSelect(discord.ui.Select):
    def __init__(self, bot):
        self.bot = bot
        options = [
            discord.SelectOption(label="24 Stunden", value="24", description="Bannt den Spieler für 1 Tag"),
            discord.SelectOption(label="3 Tage", value="72", description="Bannt den Spieler für 3 Tage"),
            discord.SelectOption(label="7 Tage", value="168", description="Bannt den Spieler für 1 Woche"),
            discord.SelectOption(label="2 Wochen", value="336", description="Bannt den Spieler für 2 Wochen"),
            discord.SelectOption(label="1 Monat", value="720", description="Bannt den Spieler für 30 Tage"),
            discord.SelectOption(label="Permanent", value="0", description="Bannt den Spieler für immer")
        ]
        super().__init__(placeholder="Wähle die Bann-Dauer aus...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        value = int(self.values[0])
        label = [opt.label for opt in self.options if opt.value == self.values[0]][0]
        await interaction.response.send_modal(BanModal(self.bot, value, label))


class BanDurationView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=300)
        self.add_item(BanDurationSelect(bot))


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
            {"id": "server1", "title": "Server 1", "rcon_url": config.SERVER1_RCON_URL, "rcon_pass": config.SERVER1_RCON_PASS},
            {"id": "server2", "title": "Server 2", "rcon_url": config.SERVER2_RCON_URL, "rcon_pass": config.SERVER2_RCON_PASS},
            {"id": "server3", "title": "Server 3", "rcon_url": config.SERVER3_RCON_URL, "rcon_pass": config.SERVER3_RCON_PASS}
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
                    

        # Remove any pending bans for this player so the bot stops trying to ban them
        cog = self.bot.get_cog("AdminPanelCog")
        cog.pending_actions = [a for a in cog.pending_actions if not (a["action"] == "ban" and a["steam_id"] == self.steam_id.value.strip())]
        save_pending_actions(cog.pending_actions)
        
        # Update database
        try:
            pool = await database.get_db_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("UPDATE global_bans SET status = 'revoked' WHERE steam_id = %s AND status = 'active'", (self.steam_id.value.strip(),))
                await conn.commit()
        except Exception as e:
            logging.error(f"Failed to update db on unban: {e}")

        # Log via event dispatcher
        log_desc = f"**Admin:** {interaction.user.mention}\n**Aktion:** Globaler Unban\n**SteamID:** `{self.steam_id.value}`\n\n"
        if success_list:
            log_desc += f"✅ **Erfolgreich auf:** {', '.join(success_list)}\n"
        if error_list:
            log_desc += f"❌ **Fehler auf:** {', '.join(error_list)}"
            
        color = discord.Color.green() if success_list else discord.Color.orange()
        self.bot.dispatch("bot_log", "🕊️ Globaler Unban Ausgeführt", log_desc, color)
        
        await interaction.followup.send(f"Entbann-Vorgang abgeschlossen! Erfolgreich: {len(success_list)}, Fehler: {len(error_list)}.", ephemeral=True)


class LookupModal(discord.ui.Modal, title='Spieler Ban-Historie'):
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
        steam_id_val = self.steam_id.value.strip()
        try:
            import aiomysql
            import database
            pool = await database.get_db_pool()
            async with pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute("SELECT * FROM global_bans WHERE steam_id = %s ORDER BY issued_at DESC", (steam_id_val,))
                    rows = await cur.fetchall()
                    
            if not rows:
                return await interaction.followup.send(f"Für die SteamID `{steam_id_val}` gibt es keine Ban-Einträge in der Datenbank.", ephemeral=True)
                
            embed = discord.Embed(title=f"🔍 Ban-Historie: {steam_id_val}", color=discord.Color.blue())
            for row in rows:
                status_emoji = "🔴" if row['status'] == 'active' else "🟢"
                expires = row['expires_at'].strftime('%d.%m.%Y %H:%M') if row['expires_at'] else "Nie"
                
                desc = (
                    f"**Admin:** {row['admin_mention']}\n"
                    f"**Dauer:** {row['duration_str']} (Bis: {expires})\n"
                    f"**Grund:** {row['reason']}\n"
                    f"**Status:** {row['status'].capitalize()}"
                )
                embed.add_field(name=f"{status_emoji} Ban am {row['issued_at'].strftime('%d.%m.%Y %H:%M')}", value=desc, inline=False)
                
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            import logging
            logging.error(f"Error in ban_lookup: {e}")
            await interaction.followup.send("Fehler beim Abrufen der Datenbank.", ephemeral=True)

class AdminPanelView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    def _is_admin(self, member: discord.Member):
        if member.guild_permissions.administrator:
            return True
        for role in member.roles:
            if role.id in config.ADMIN_ROLE_IDS:
                return True
        return False

    @discord.ui.button(label="Spieler Bannen", style=discord.ButtonStyle.danger, custom_id="admin_panel_ban", emoji="🔨")
    async def ban_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_admin(interaction.user):
            return await interaction.response.send_message("❌ Du hast keine Berechtigung für diese Aktion.", ephemeral=True)
            
        view = BanDurationView(self.bot)
        await interaction.response.send_message("Wie lange soll der Spieler gebannt werden?", view=view, ephemeral=True)

    @discord.ui.button(label="Spieler Entbannen", style=discord.ButtonStyle.success, custom_id="admin_panel_unban", emoji="🕊️")
    async def unban_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_admin(interaction.user):
            return await interaction.response.send_message("❌ Du hast keine Berechtigung für diese Aktion.", ephemeral=True)
            
        await interaction.response.send_modal(UnbanModal(self.bot))

    @discord.ui.button(label="Ban Historie", style=discord.ButtonStyle.secondary, custom_id="admin_panel_lookup", emoji="🔍")
    async def lookup_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_admin(interaction.user):
            return await interaction.response.send_message("❌ Du hast keine Berechtigung für diese Aktion.", ephemeral=True)
            
        await interaction.response.send_modal(LookupModal(self.bot))

class AdminPanelCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.pending_actions = load_pending_actions()
        self.process_pending_actions.start()
        self.ensure_panel.start()
        self.auto_unban_task.start()

    def cog_unload(self):
        self.process_pending_actions.cancel()
        self.ensure_panel.cancel()
        self.auto_unban_task.cancel()

    def get_saved_message_id(self):
        if os.path.exists(config.ADMIN_PANEL_MSG_ID_FILE):
            try:
                with open(config.ADMIN_PANEL_MSG_ID_FILE, "r") as f:
                    return json.load(f).get("message_id")
            except: pass
        return None

    def save_message_id(self, message_id):
        try:
            with open(config.ADMIN_PANEL_MSG_ID_FILE, "w") as f:
                json.dump({"message_id": message_id}, f)
        except: pass

    @tasks.loop(count=1)
    async def ensure_panel(self):
        if not config.ADMIN_PANEL_CHANNEL_ID:
            return
            
        try:
            channel = self.bot.get_channel(int(config.ADMIN_PANEL_CHANNEL_ID)) or await self.bot.fetch_channel(int(config.ADMIN_PANEL_CHANNEL_ID))
            if not channel: return
                
            embed = discord.Embed(
                title="🛡️ Kartell Global Admin Panel",
                description=(
                    "Über dieses Panel können Spieler **gleichzeitig auf allen Servern** (Server 1, Server 2 & Server 3) gebannt oder entbannt werden.\n\n"
                    "**Hinweis:**\n"
                    "• Du benötigst die 17-stellige **Steam64 ID** des Spielers.\n"
                    "• Alle Aktionen werden zentral in den Logs aufgezeichnet."
                ),
                color=discord.Color.dark_theme()
            )
            
            view = AdminPanelView(self.bot)
            msg_id = self.get_saved_message_id()
            msg = None
            
            if msg_id:
                try:
                    msg = await channel.fetch_message(msg_id)
                    await msg.edit(embed=embed, view=view)
                except discord.NotFound:
                    msg = None
                except Exception:
                    msg = None
                    
            if msg is None:
                new_msg = await channel.send(embed=embed, view=view)
                self.save_message_id(new_msg.id)
                
        except Exception:
            pass

    @ensure_panel.before_loop
    async def before_ensure_panel(self):
        await self.bot.wait_until_ready()

    def add_pending_action(self, action_type, server_id, steam_id, reason, admin_mention):
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
                    continue
                    
                headers = {"Authorization": f"Bearer {srv['pass']}"}
                success = False
                
                if action["action"] == "ban":
                    url = f"{srv['url'].rstrip('/')}/v1/bans"
                    payload = {"steamId": action["steam_id"], "reason": action["reason"]}
                    try:
                        async with session.post(url, headers=headers, json=payload, timeout=5) as response:
                            if response.status in [200, 201, 204]:
                                success = True
                            # If 404, they are still offline. Keep in queue.
                    except: pass
                
                elif action["action"] == "unban":
                    url = f"{srv['url'].rstrip('/')}/v1/bans/{action['steam_id']}"
                    try:
                        async with session.delete(url, headers=headers, timeout=5) as response:
                            if response.status in [200, 204, 404]:
                                success = True
                    except: pass
                
                if success:
                    title_str = "🔨 Verzögerter Ban Erfolgreich" if action["action"] == "ban" else "🕊️ Verzögerter Unban Erfolgreich"
                    color = discord.Color.green()
                    desc = f"**Admin:** {action['admin_mention']}\n**Aktion:** {action['action'].capitalize()}\n**SteamID:** `{action['steam_id']}`\n**Server:** {srv['title']}\n\nDie zuvor fehlgeschlagene Aktion konnte nun erfolgreich auf dem Server ausgeführt werden!"
                    self.bot.dispatch("bot_log", title_str, desc, color)
                else:
                    remaining_actions.append(action)
                    
        if len(remaining_actions) != len(self.pending_actions):
            self.pending_actions = remaining_actions
            save_pending_actions(self.pending_actions)

    @process_pending_actions.before_loop
    async def before_process(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=10)
    async def auto_unban_task(self):
        try:
            pool = await database.get_db_pool()
            async with pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute("SELECT steam_id FROM global_bans WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at <= UTC_TIMESTAMP()")
                    expired_bans = await cur.fetchall()
                    
                    if not expired_bans:
                        return
                        
                    servers = [
                        {"id": "server1", "title": "Server 1", "rcon_url": config.SERVER1_RCON_URL, "rcon_pass": config.SERVER1_RCON_PASS},
                        {"id": "server2", "title": "Server 2", "rcon_url": config.SERVER2_RCON_URL, "rcon_pass": config.SERVER2_RCON_PASS},
                        {"id": "server3", "title": "Server 3", "rcon_url": config.SERVER3_RCON_URL, "rcon_pass": config.SERVER3_RCON_PASS}
                    ]
                    
                    async with aiohttp.ClientSession() as session:
                        for row in expired_bans:
                            steam_id = row['steam_id']
                            success_list = []
                            error_list = []
                            
                            for srv in servers:
                                if not srv["rcon_url"] or not srv["rcon_pass"]: continue
                                url = f"{srv['rcon_url'].rstrip('/')}/v1/bans/{steam_id}"
                                headers = {"Authorization": f"Bearer {srv['rcon_pass']}"}
                                try:
                                    async with session.delete(url, headers=headers, timeout=5) as response:
                                        if response.status in [200, 204]:
                                            success_list.append(srv["title"])
                                        elif response.status == 404:
                                            config_url = f"{srv['rcon_url'].rstrip('/')}/v1/config"
                                            async with session.get(config_url, headers=headers, timeout=5) as conf_resp:
                                                if conf_resp.status == 200:
                                                    conf_data = await conf_resp.json()
                                                    text = conf_data.get("text", "")
                                                    target_line1 = f"\r\n.DefaultBannedPlayerIds={steam_id}"
                                                    target_line2 = f"\n.DefaultBannedPlayerIds={steam_id}"
                                                    if target_line1 in text or target_line2 in text:
                                                        text = text.replace(target_line1, "").replace(target_line2, "")
                                                        async with session.put(config_url, headers=headers, json={"text": text}, timeout=5) as put_resp:
                                                            if put_resp.status in [200, 202, 204]:
                                                                success_list.append(srv["title"])
                                                            else:
                                                                error_list.append(f"{srv['title']} (Config PUT {put_resp.status})")
                                                                self.add_pending_action("unban", srv["id"], steam_id, "", "System (Auto-Unban)")
                                                    else:
                                                        success_list.append(f"{srv['title']} (War nicht gebannt)")
                                                else:
                                                    error_list.append(f"{srv['title']} (Config GET {conf_resp.status})")
                                                    self.add_pending_action("unban", srv["id"], steam_id, "", "System (Auto-Unban)")
                                        else:
                                            error_list.append(srv["title"])
                                            self.add_pending_action("unban", srv["id"], steam_id, "", "System (Auto-Unban)")
                                except:
                                    error_list.append(srv["title"])
                                    self.add_pending_action("unban", srv["id"], steam_id, "", "System (Auto-Unban)")
                                    
                            await cur.execute("UPDATE global_bans SET status = 'expired' WHERE steam_id = %s AND status = 'active'", (steam_id,))
                            
                            log_desc = f"**Aktion:** Automatischer Unban (Zeit abgelaufen)\n**SteamID:** `{steam_id}`\n\n"
                            if success_list: log_desc += f"✅ **Erfolgreich auf:** {', '.join(success_list)}\n"
                            if error_list: log_desc += f"❌ **Fehler auf:** {', '.join(error_list)}"
                            
                            self.bot.dispatch("bot_log", "⏳ Auto-Unban Ausgeführt", log_desc, discord.Color.blue())
                            
                await conn.commit()
        except Exception as e:
            logging.error(f"Error in auto_unban_task: {e}")

    @auto_unban_task.before_loop
    async def before_auto_unban(self):
        await self.bot.wait_until_ready()

    def _is_admin(self, member: discord.Member):
        if member.guild_permissions.administrator:
            return True
        for role in member.roles:
            if role.id in config.ADMIN_ROLE_IDS:
                return True
        return False

    @app_commands.command(name="adminpanel", description="Sendet das globale Admin-Panel zum Bannen/Entbannen von Spielern.")
    async def admin_panel(self, interaction: discord.Interaction):
        if not self._is_admin(interaction.user):
            return await interaction.response.send_message("❌ Du hast keine Berechtigung für diesen Befehl.", ephemeral=True)
            
        embed = discord.Embed(
            title="🛡️ Kartell Global Admin Panel",
            description=(
                "Über dieses Panel können Spieler **gleichzeitig auf allen Servern** (Server 1, Server 2 & Server 3) gebannt oder entbannt werden.\n\n"
                "**Hinweis:**\n"
                "• Du benötigst die 17-stellige **Steam64 ID** des Spielers.\n"
                "• Alle Aktionen werden zentral in den Logs aufgezeichnet."
            ),
            color=discord.Color.dark_theme()
        )
        embed.set_thumbnail(url=interaction.guild.icon.url if interaction.guild.icon else None)
        
        view = AdminPanelView(self.bot)
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="ban_lookup", description="Sucht nach der Ban-Historie eines Spielers.")
    @app_commands.describe(steam_id="Die Steam64 ID des Spielers")
    async def ban_lookup(self, interaction: discord.Interaction, steam_id: str):
        if not self._is_admin(interaction.user):
            return await interaction.response.send_message("❌ Du hast keine Berechtigung für diesen Befehl.", ephemeral=True)
            
        await interaction.response.defer(ephemeral=True)
        try:
            pool = await database.get_db_pool()
            async with pool.acquire() as conn:
                import aiomysql
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute("SELECT * FROM global_bans WHERE steam_id = %s ORDER BY issued_at DESC", (steam_id,))
                    rows = await cur.fetchall()
                    
            if not rows:
                return await interaction.followup.send(f"Für die SteamID `{steam_id}` gibt es keine Ban-Einträge in der Datenbank.", ephemeral=True)
                
            embed = discord.Embed(title=f"🔍 Ban-Historie: {steam_id}", color=discord.Color.blue())
            for row in rows:
                status_emoji = "🔴" if row['status'] == 'active' else "🟢"
                expires = row['expires_at'].strftime('%d.%m.%Y %H:%M') if row['expires_at'] else "Nie"
                
                desc = (
                    f"**Admin:** {row['admin_mention']}\n"
                    f"**Dauer:** {row['duration_str']} (Bis: {expires})\n"
                    f"**Grund:** {row['reason']}\n"
                    f"**Status:** {row['status'].capitalize()}"
                )
                embed.add_field(name=f"{status_emoji} Ban am {row['issued_at'].strftime('%d.%m.%Y %H:%M')}", value=desc, inline=False)
                
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logging.error(f"Error in ban_lookup: {e}")
            await interaction.followup.send("Fehler beim Abrufen der Datenbank.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(AdminPanelCog(bot))
