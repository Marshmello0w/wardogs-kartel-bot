import logging
import json
import os
import aiohttp
import discord
from discord.ext import tasks, commands
from discord import app_commands
import config

STATE_FILE = "bot/map_vote_state.json"

class MapVoteButton(discord.ui.Button):
    def __init__(self, option_name):
        super().__init__(label=option_name, style=discord.ButtonStyle.primary, custom_id=f"vote_{option_name}")
        self.option_name = option_name

    async def callback(self, interaction: discord.Interaction):
        view: MapVoteView = self.view
        
        if view.locked or not view.cog.state.get(view.server_id, {}).get("enabled", True):
            await interaction.response.send_message("Das Voting ist derzeit deaktiviert oder geschlossen!", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        
        # User can change their vote
        view.votes[user_id] = self.option_name
        view.save_callback()

        # Update the embed to show current vote counts
        await interaction.response.edit_message(embed=view.generate_embed(), view=view)

class MapVoteView(discord.ui.View):
    def __init__(self, server_id, server_title, cog, votes=None, locked=False):
        super().__init__(timeout=None)
        self.server_id = server_id
        self.server_title = server_title
        self.cog = cog
        self.votes = votes if votes is not None else {}
        self.locked = locked

        for option in config.MAP_VOTE_OPTIONS.keys():
            btn = MapVoteButton(option)
            if self.locked:
                btn.disabled = True
            self.add_item(btn)

    def save_callback(self):
        self.cog.save_votes(self.server_id, self.votes)

    def generate_embed(self):
        embed = discord.Embed(
            title=f"🗺️ Map Voting: {self.server_title}",
            description=f"Stimme für die nächste Map auf **{self.server_title}** ab! (Benötigt mindestens 5 Stimmen für einen Wechsel)\n\n*(Dieses Voting gilt ausschließlich für {self.server_title})*",
            color=discord.Color.blue() if not self.locked else discord.Color.red()
        )
        if self.locked:
            embed.description = f"Das Voting für die nächste Runde auf **{self.server_title}** ist **GESCHLOSSEN**."

        vote_counts = {opt: 0 for opt in config.MAP_VOTE_OPTIONS.keys()}
        for uid, opt in self.votes.items():
            if opt in vote_counts:
                vote_counts[opt] += 1
                
        for opt, count in vote_counts.items():
            embed.add_field(name=opt, value=f"{count} Stimme(n)", inline=True)
            
        return embed


class MapVoteCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.state = self.load_state()
        self.check_match_scores.start()

    def cog_unload(self):
        self.check_match_scores.cancel()

    def load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except:
                pass
        return {
            "server2": {"enabled": True, "msg_id": None, "votes": {}, "locked": False, "channel_id": config.SERVER2_VOTE_CHANNEL_ID, "injected_map": None, "waiting_to_cleanup": None},
            "server3": {"enabled": True, "msg_id": None, "votes": {}, "locked": False, "channel_id": config.SERVER3_VOTE_CHANNEL_ID, "injected_map": None, "waiting_to_cleanup": None}
        }

    def save_state(self):
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(self.state, f)

    def save_votes(self, server_id, votes):
        if server_id not in self.state:
            self.state[server_id] = {}
        self.state[server_id]["votes"] = votes
        self.save_state()

    async def fetch_status(self, session, rcon_url, rcon_pass):
        headers = {"Authorization": f"Bearer {rcon_pass}"}
        url = f"{rcon_url.rstrip('/')}/v1/status"
        try:
            async with session.get(url, headers=headers, timeout=5) as response:
                if response.status == 200:
                    return await response.json()
        except Exception:
            pass
        return None

    async def fetch_config(self, session, rcon_url, rcon_pass):
        headers = {"Authorization": f"Bearer {rcon_pass}"}
        url = f"{rcon_url.rstrip('/')}/v1/config"
        try:
            async with session.get(url, headers=headers, timeout=10) as response:
                if response.status == 200:
                    return await response.json()
        except Exception as e:
            logging.error(f"Error fetching config: {e}")
        return None

    async def push_config(self, session, rcon_url, rcon_pass, config_text, revision):
        headers = {"Authorization": f"Bearer {rcon_pass}", "If-Match": f'"{revision}"'}
        url = f"{rcon_url.rstrip('/')}/v1/config?force=true&fullApply=true"
        try:
            async with session.put(url, headers=headers, data=config_text.encode('utf-8'), timeout=10) as response:
                return response.status in (200, 202)
        except Exception as e:
            logging.error(f"Error pushing config: {e}")
        return False

    async def remove_injected_map(self, session, rcon_url, rcon_pass, injected_entry):
        conf = await self.fetch_config(session, rcon_url, rcon_pass)
        if not conf: return False
        
        text = conf.get("text", "")
        revision = conf.get("revision", "")
        
        import re
        section_pattern = r'(\[/Script/WDGame\.WDServerMapRotationSettings\].*?)(?=\n\[|$)'
        match = re.search(section_pattern, text, re.DOTALL)
        if not match: return False
        
        section_text = match.group(1)
        lines = section_text.split("\n")
        other_lines = []
        old_entries = []
        for line in lines:
            if line.startswith(".RotationEntries"):
                old_entries.append(line)
            elif not line.startswith("!RotationEntries"):
                other_lines.append(line)
                
        # Only remove if the top entry is exactly the one we injected
        if old_entries and old_entries[0] == injected_entry:
            old_entries.pop(0)
            
            other_lines.append("!RotationEntries=ClearArray")
            for old in old_entries:
                other_lines.append(old)
                
            new_section_text = "\n".join(other_lines)
            new_text = text.replace(section_text, new_section_text)
            return await self.push_config(session, rcon_url, rcon_pass, new_text, revision)
        return True

    async def modify_rotation(self, session, rcon_url, rcon_pass, winning_option):
        # Fetch current config
        conf = await self.fetch_config(session, rcon_url, rcon_pass)
        if not conf:
            return False
            
        text = conf.get("text", "")
        revision = conf.get("revision", "")
        
        # We need to find the rotation section
        import re
        section_pattern = r'(\[/Script/WDGame\.WDServerMapRotationSettings\].*?)(?=\n\[|$)'
        match = re.search(section_pattern, text, re.DOTALL)
        if not match:
            logging.error("Could not find WDServerMapRotationSettings in config")
            return False
            
        section_text = match.group(1)
        
        # Remove old !RotationEntries and .RotationEntries
        lines = section_text.split("\n")
        other_lines = []
        old_entries = []
        for line in lines:
            if line.startswith(".RotationEntries"):
                old_entries.append(line)
            elif not line.startswith("!RotationEntries"):
                other_lines.append(line)
                
        # Append the new ones
        other_lines.append("!RotationEntries=ClearArray")
        
        map_settings = config.MAP_VOTE_OPTIONS[winning_option]
        entry = f'.RotationEntries=(Map="{map_settings["Map"]}",Experience="{map_settings["Experience"]}",Lighting="{map_settings["Lighting"]}")'
        other_lines.append(entry)
        
        # Append old entries that don't match the new entry
        for old in old_entries:
            if old != entry:
                other_lines.append(old)
                
        # Replace section in text
        new_section_text = "\n".join(other_lines)
        new_text = text.replace(section_text, new_section_text)
        
        success = await self.push_config(session, rcon_url, rcon_pass, new_text, revision)
        return entry if success else None

    @tasks.loop(seconds=15)
    async def check_match_scores(self):
        await self.bot.wait_until_ready()
        
        servers = [
            {"id": "server2", "title": "Server 2", "rcon_url": config.SERVER2_RCON_URL, "rcon_pass": config.SERVER2_RCON_PASS, "channel": config.SERVER2_VOTE_CHANNEL_ID},
            {"id": "server3", "title": "Server 3", "rcon_url": config.SERVER3_RCON_URL, "rcon_pass": config.SERVER3_RCON_PASS, "channel": config.SERVER3_VOTE_CHANNEL_ID}
        ]

        async with aiohttp.ClientSession() as session:
            for srv in servers:
                s_id = srv["id"]
                if not srv["rcon_url"] or not srv["rcon_pass"] or not srv["channel"]:
                    continue
                    
                channel = self.bot.get_channel(int(srv["channel"]))
                if not channel:
                    try:
                        channel = await self.bot.fetch_channel(int(srv["channel"]))
                    except:
                        continue
                        
                if s_id not in self.state:
                    self.state[s_id] = {"enabled": True, "msg_id": None, "votes": {}, "locked": False, "channel_id": srv["channel"], "last_highest_score": -1, "injected_map": None, "waiting_to_cleanup": None}
                state = self.state[s_id]
                
                if not state.get("enabled", True):
                    continue

                status_data = await self.fetch_status(session, srv["rcon_url"], srv["rcon_pass"])
                if status_data and "factionScores" in status_data:
                    highest_score = 0
                    for faction in status_data["factionScores"]:
                        score = faction.get("score", 0)
                        if score > highest_score:
                            highest_score = score
                            
                    last_score = state.get("last_highest_score", -1)
                    
                    # Detect if a new match started (score reset) OR if it's the very first time
                    if (highest_score < last_score) or (state.get("msg_id") is None and highest_score < 95):
                        # Shift the injected map to "waiting_to_cleanup". 
                        # We won't delete it immediately at score 0, but wait for the first point.
                        injected = state.get("injected_map")
                        if injected:
                            state["waiting_to_cleanup"] = injected
                            state["injected_map"] = None
                            
                        # Unlock and clear votes
                        state["locked"] = False
                        state["votes"] = {}
                        
                        view = MapVoteView(s_id, srv["title"], self, votes=state["votes"], locked=False)
                        embed = view.generate_embed()
                        
                        msg = await channel.send(embed=embed, view=view)
                        state["msg_id"] = msg.id
                        self.bot.dispatch("bot_log", "🗺️ Map Voting Gestartet", f"Das Voting für die nächste Map auf **{srv['title']}** wurde gestartet.", discord.Color.blue())
                        
                    # If a new round has started and scored its first point, finally clean up the old injected map
                    if highest_score > 0 and state.get("waiting_to_cleanup"):
                        to_clean = state["waiting_to_cleanup"]
                        try:
                            await self.remove_injected_map(session, srv["rcon_url"], srv["rcon_pass"], to_clean)
                            logging.info(f"Removed injected map for {s_id} after first point scored.")
                        except Exception as e:
                            logging.error(f"Failed to remove injected map: {e}")
                        state["waiting_to_cleanup"] = None

                    # Lock votes when score hits 95
                    elif highest_score >= 95 and not state.get("locked", False):
                        state["locked"] = True
                        
                        # Count votes
                        vote_counts = {opt: 0 for opt in config.MAP_VOTE_OPTIONS.keys()}
                        for uid, opt in state.get("votes", {}).items():
                            if opt in vote_counts:
                                vote_counts[opt] += 1
                                
                        winner = None
                        highest_votes = 0
                        for opt, count in vote_counts.items():
                            if count >= 5 and count > highest_votes:
                                winner = opt
                                highest_votes = count
                                
                        # Update Discord message to locked
                        msg_id = state.get("msg_id")
                        if msg_id:
                            try:
                                msg = await channel.fetch_message(msg_id)
                                view = MapVoteView(s_id, srv["title"], self, votes=state["votes"], locked=True)
                                await msg.edit(embed=view.generate_embed(), view=view)
                            except:
                                pass
                                
                        # Apply to server
                        if winner:
                            success = await self.modify_rotation(session, srv["rcon_url"], srv["rcon_pass"], winner)
                            if success:
                                await channel.send(f"✅ **Voting Beendet!** Die Map für die nächste Runde ist: **{winner}** ({highest_votes} Stimmen).")
                                self.bot.dispatch("bot_log", "🗺️ Map Voting Erfolgreich", f"Auf {srv['title']} wurde erfolgreich **{winner}** gewählt und auf Platz 1 der Rotation gesetzt.", discord.Color.green())
                            else:
                                await channel.send(f"❌ **Fehler!** Konnte die Map **{winner}** nicht in der Server-Config setzen.")
                        else:
                            await channel.send("ℹ️ **Voting Beendet!** Nicht genügend Stimmen (mindestens 5 erforderlich). Die Rotation bleibt unverändert.")
                            self.bot.dispatch("bot_log", "🗺️ Map Voting Beendet", f"Auf **{srv['title']}** gab es nicht genügend Stimmen. Die Rotation bleibt unverändert.", discord.Color.orange())
                            
                # Update last known score for next loop
                state["last_highest_score"] = highest_score
                self.save_state()

    @app_commands.command(name="voting", description="Schaltet das Map-Voting an oder aus.")
    @app_commands.describe(server="Der Server", status="Soll das Voting an oder aus sein?")
    @app_commands.choices(
        server=[
            app_commands.Choice(name="Server 2", value="server2"),
            app_commands.Choice(name="Server 3", value="server3")
        ],
        status=[
            app_commands.Choice(name="On", value="on"),
            app_commands.Choice(name="Off", value="off")
        ]
    )
    async def toggle_voting(self, interaction: discord.Interaction, server: app_commands.Choice[str], status: app_commands.Choice[str]):
        if config.ADMIN_ROLE_IDS:
            user_roles = [r.id for r in interaction.user.roles] if hasattr(interaction.user, 'roles') else []
            is_admin = getattr(interaction.user.guild_permissions, 'administrator', False)
            if not any(r in config.ADMIN_ROLE_IDS for r in user_roles) and not is_admin:
                await interaction.response.send_message("❌ Du hast keine Berechtigung für diesen Befehl.", ephemeral=True)
                return
                
        server = server.value
        enable = status.value == "on"
        
        if server not in self.state:
            self.state[server] = {}
        
        self.state[server]["enabled"] = enable
        self.save_state()
        
        status_text = "aktiviert" if enable else "deaktiviert"
        await interaction.response.send_message(f"✅ Map-Voting wurde **{status_text}**.", ephemeral=True)
        
        # Update current message if exists
        msg_id = self.state[server].get("msg_id")
        channel_id = self.state[server].get("channel_id")
        if msg_id and channel_id and not enable:
            try:
                channel = self.bot.get_channel(int(channel_id)) or await self.bot.fetch_channel(int(channel_id))
                msg = await channel.fetch_message(int(msg_id))
                view = MapVoteView(server, f"Server {server[-1]}", self, votes=self.state[server].get("votes", {}), locked=True)
                embed = view.generate_embed()
                embed.description = "⚠️ **Map-Voting wurde vom Admin vorzeitig DEAKTIVIERT.**"
                embed.color = discord.Color.orange()
                await msg.edit(embed=embed, view=view)
            except Exception as e:
                pass

    @app_commands.command(name="forcemap", description="[Admin] Erzwingt eine Map-Änderung in der Server-Rotation zum Testen.")
    @app_commands.describe(server="Der Server", map_name="Die Map, die erzwungen werden soll")
    @app_commands.choices(
        server=[
            app_commands.Choice(name="Server 2", value="server2"),
            app_commands.Choice(name="Server 3", value="server3")
        ],
        map_name=[
            app_commands.Choice(name=opt, value=opt) for opt in config.MAP_VOTE_OPTIONS.keys()
        ]
    )
    async def force_map(self, interaction: discord.Interaction, server: app_commands.Choice[str], map_name: app_commands.Choice[str]):
        if config.ADMIN_ROLE_IDS:
            user_roles = [r.id for r in interaction.user.roles] if hasattr(interaction.user, 'roles') else []
            is_admin = getattr(interaction.user.guild_permissions, 'administrator', False)
            if not any(r in config.ADMIN_ROLE_IDS for r in user_roles) and not is_admin:
                await interaction.response.send_message("❌ Du hast keine Berechtigung für diesen Befehl.", ephemeral=True)
                return

        await interaction.response.defer(ephemeral=True)
        
        srv_id = server.value
        rcon_url = config.SERVER2_RCON_URL if srv_id == "server2" else config.SERVER3_RCON_URL
        rcon_pass = config.SERVER2_RCON_PASS if srv_id == "server2" else config.SERVER3_RCON_PASS
        
        async with aiohttp.ClientSession() as session:
            injected_entry = await self.modify_rotation(session, rcon_url, rcon_pass, map_name.value)
            if injected_entry:
                if srv_id in self.state:
                    self.state[srv_id]["injected_map"] = injected_entry
                    self.save_state()
                await interaction.followup.send(f"✅ Die Map **{map_name.value}** wurde erfolgreich auf Platz 1 der Rotation für {srv_id} gesetzt!")
                self.bot.dispatch("bot_log", "🛠️ Admin Force Map", f"Ein Admin hat manuell die Map **{map_name.value}** auf Platz 1 gesetzt.", discord.Color.green())
            else:
                await interaction.followup.send(f"❌ Fehler beim Setzen der Map **{map_name.value}**.")

async def setup(bot):
    await bot.add_cog(MapVoteCog(bot))
