import json
import os
import logging
import aiohttp
import discord
from discord.ext import tasks, commands
import time

import config
import database

class EphemeralTimeframeDropdown(discord.ui.Select):
    def __init__(self, current_tf):
        options = [
            discord.SelectOption(label="Letzte 7 Tage", value="7d", emoji="📅", default=(current_tf == "7d")),
            discord.SelectOption(label="Letzte 30 Tage", value="30d", emoji="📆", default=(current_tf == "30d")),
            discord.SelectOption(label="All-Time", value="all", emoji="🏆", default=(current_tf == "all"))
        ]
        super().__init__(placeholder="Zeitraum wählen...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        self.view.current_tf = self.values[0]
        await self.view.update_message(interaction)

class EphemeralSortDropdown(discord.ui.Select):
    def __init__(self, current_sort):
        options = [
            discord.SelectOption(label="Nach K/D sortieren", value="kd", emoji="⚔️", default=(current_sort == "kd")),
            discord.SelectOption(label="Nach Cash sortieren", value="cash", emoji="💵", default=(current_sort == "cash"))
        ]
        super().__init__(placeholder="Sortierung wählen...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        self.view.current_sort = self.values[0]
        await self.view.update_message(interaction)

class EphemeralLeaderboardView(discord.ui.View):
    def __init__(self, cog, server_id, current_tf, current_sort):
        super().__init__(timeout=300)
        self.cog = cog
        self.server_id = server_id
        self.current_tf = current_tf
        self.current_sort = current_sort
        
        self.add_item(EphemeralTimeframeDropdown(current_tf))
        self.add_item(EphemeralSortDropdown(current_sort))

    async def update_message(self, interaction: discord.Interaction):
        title = "Server 1" if self.server_id == "server1" else "Server 2"
        banned_ids = await self.cog.fetch_bans_for_server(self.server_id)
        embed = await self.cog.generate_embed(self.server_id, title, self.current_tf, self.current_sort, banned_ids)
        new_view = EphemeralLeaderboardView(self.cog, self.server_id, self.current_tf, self.current_sort)
        await interaction.response.edit_message(embed=embed, view=new_view)

class PublicLeaderboardDropdown(discord.ui.Select):
    def __init__(self, cog, server_id):
        self.cog = cog
        self.server_id = server_id
        options = [
            discord.SelectOption(label="Letzte 7 Tage", value="7d", emoji="📅"),
            discord.SelectOption(label="Letzte 30 Tage", value="30d", emoji="📆"),
            discord.SelectOption(label="All-Time", value="all", emoji="🏆")
        ]
        super().__init__(placeholder="Auswahl / Menü öffnen...", min_values=1, max_values=1, options=options, custom_id=f"pub_lb_{server_id}")

    async def callback(self, interaction: discord.Interaction):
        tf = self.values[0]
        title = "Server 1" if self.server_id == "server1" else "Server 2"
        
        banned_ids = await self.cog.fetch_bans_for_server(self.server_id)
        embed = await self.cog.generate_embed(self.server_id, title, tf, sort_by="kd", banned_ids=banned_ids)
        view = EphemeralLeaderboardView(self.cog, self.server_id, tf, "kd")
        
        # 1. Schicke die persönliche/ephemere Nachricht
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        
        # 2. Setze das Haupt-Dropdown direkt wieder auf den Placeholder zurück
        try:
            reset_view = PublicLeaderboardView(self.cog, self.server_id)
            await interaction.message.edit(view=reset_view)
        except:
            pass

class PublicLeaderboardView(discord.ui.View):
    def __init__(self, cog, server_id):
        super().__init__(timeout=None)
        self.add_item(PublicLeaderboardDropdown(cog, server_id))

class Leaderboard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db_pool = None
        self.state_file = "leaderboard_state.json"
        self.fast_mode = False
        self.update_leaderboard_data.start()
        self.update_leaderboard_ui.start()

    def cog_unload(self):
        self.update_leaderboard_data.cancel()
        self.update_leaderboard_ui.cancel()

    def get_saved_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except:
                pass
        return {}

    def save_state(self, data):
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(data, f)

    async def fetch_bans(self, session, rcon_url, rcon_pass, server_title="Unbekannter Server"):
        headers = {"Authorization": f"Bearer {rcon_pass}"}
        url = f"{rcon_url.rstrip('/')}/v1/bans"
        try:
            async with session.get(url, headers=headers, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    return [b["steamId"] for b in data.get("bans", []) if "steamId" in b]
        except Exception as e:
            logging.error(f"Error fetching bans on {server_title}: {e}")
        return []

    async def fetch_bans_for_server(self, server_id):
        if server_id == "server1":
            rcon_url, rcon_pass = config.SERVER1_RCON_URL, config.SERVER1_RCON_PASS
        elif server_id == "server2":
            rcon_url, rcon_pass = config.SERVER2_RCON_URL, config.SERVER2_RCON_PASS
        else:
            rcon_url, rcon_pass = config.SERVER3_RCON_URL, config.SERVER3_RCON_PASS
        try:
            async with aiohttp.ClientSession() as session:
                return await self.fetch_bans(session, rcon_url, rcon_pass, server_id)
        except:
            return []

    async def fetch_status(self, session, rcon_url, rcon_pass):

        headers = {"Authorization": f"Bearer {rcon_pass}"}
        url = f"{rcon_url.rstrip('/')}/v1/status"
        try:
            async with session.get(url, headers=headers, timeout=5) as response:
                if response.status == 200:
                    return await response.json()
        except:
            pass
        return None

    async def fetch_players(self, session, rcon_url, rcon_pass, server_title="Unbekannter Server"):

        headers = {"Authorization": f"Bearer {rcon_pass}"}
        url = f"{rcon_url.rstrip('/')}/v1/players"
        try:
            async with session.get(url, headers=headers, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("players", [])
                else:
                    logging.warning(f"Failed to fetch leaderboard on {server_title}: HTTP {response.status}")
                    return None
        except Exception as e:
            logging.error(f"Error fetching leaderboard on {server_title}: {e}")
            return None

    async def generate_embed(self, server_id, server_title, tf_key, sort_by="kd", banned_ids=None, show_sort_text=True):
        try:
            top_players = await database.get_top_players(self.db_pool, server_id, timeframe=tf_key, limit=10, banned_steam_ids=banned_ids, sort_by=sort_by)
        except Exception as e:
            logging.error(f"DB Error getting top players: {e}")
            top_players = []

        titles = {"7d": "Letzte 7 Tage", "30d": "Letzte 30 Tage", "all": "All-Time"}
        sort_text = f" (Nach Cash)" if sort_by == "cash" else f" (Nach K/D)"
        
        title_str = f"🏆 {server_title} - {titles[tf_key]}"
        if show_sort_text:
            title_str += sort_text
            
        embed = discord.Embed(
            title=title_str, 
            color=discord.Color.gold()
        )

        if not top_players:
            embed.add_field(name="No Data", value="Noch keine Spielerdaten vorhanden.", inline=False)
        else:
            rank = 1
            for p in top_players:
                kills = p['kills']
                deaths = p['deaths']
                cash = p.get('cash', 0)
                kd = round(kills / deaths, 2) if deaths > 0 else kills
                
                val = f"**Kills:** {kills} | **Deaths:** {deaths} | **K/D:** {kd} | **Cash:** ${cash}"
                
                prefix = ""
                if rank == 1: prefix = "🥇 "
                elif rank == 2: prefix = "🥈 "
                elif rank == 3: prefix = "🥉 "
                else: prefix = f"**{rank}.** "
                
                embed.add_field(name=f"{prefix}{p['name']}", value=val, inline=False)
                rank += 1

        current_time = int(time.time())
        embed.add_field(
            name="\u200b",
            value=f"Letzte Aktualisierung: <t:{current_time}:t>", 
            inline=False
        )
        return embed

    @tasks.loop(seconds=15)
    async def update_leaderboard_data(self):
        self.db_pool = await database.check_and_reconnect(self.db_pool)
        if not self.db_pool:
            logging.warning("Database unavailable, skipping leaderboard update this round.")
            return

        channel_id_str = config.LEADERBOARD_CHANNEL_ID
        if not channel_id_str:
            return

        channel = self.bot.get_channel(int(channel_id_str))
        if not channel:
            try:
                channel = await self.bot.fetch_channel(int(channel_id_str))
            except Exception as e:
                logging.error(f"Could not fetch leaderboard channel: {e}")
                return

        servers = [
            {
                "id": "server1",
                "title": "Server 1",
                "rcon_url": config.SERVER1_RCON_URL,
                "rcon_pass": config.SERVER1_RCON_PASS,
            },
            {
                "id": "server2",
                "title": "Server 2",
                "rcon_url": config.SERVER2_RCON_URL,
                "rcon_pass": config.SERVER2_RCON_PASS,
            },
            {
                "id": "server3",
                "title": "Server 3",
                "rcon_url": config.SERVER3_RCON_URL,
                "rcon_pass": config.SERVER3_RCON_PASS,
            }
        ]
        
        fast_mode = False

        async with aiohttp.ClientSession() as session:

            for srv in servers:
                if not srv["rcon_url"] or not srv["rcon_pass"]:
                    continue
                    
                status_data = await self.fetch_status(session, srv["rcon_url"], srv["rcon_pass"])
                if status_data and "factionScores" in status_data:
                    highest_score = 0
                    for faction in status_data["factionScores"]:
                        score = faction.get("score", 0)
                        if score > highest_score:
                            highest_score = score
                        if score >= 99:  # Ab 99 Punkten gehen wir in den Turbo-Modus
                            fast_mode = True

                players = await self.fetch_players(session, srv["rcon_url"], srv["rcon_pass"], srv.get("title", srv["id"]))

                
                if players is not None:
                    for p in players:
                        steam_id = p.get("steamId")
                        name = p.get("name", "Unknown")
                        kills = p.get("kills", 0)
                        deaths = p.get("deaths", 0)
                        cash = p.get("cash", 0)
                        
                        if steam_id:
                            try:
                                await database.update_player_stats(
                                    self.db_pool, 
                                    srv["id"], 
                                    steam_id, 
                                    name, 
                                    kills, 
                                    deaths, 
                                    cash
                                )
                            except Exception as e:
                                logging.error(f"DB Error updating player: {e}")

        # Passe das Intervall an
        if fast_mode and not self.fast_mode:
            self.update_leaderboard_data.change_interval(seconds=5)
            self.fast_mode = True
            logging.info("Match fast vorbei: Wechsle in den 5-Sekunden-Turbo-Modus!")
        elif not fast_mode and self.fast_mode:
            self.update_leaderboard_data.change_interval(seconds=15)
            self.fast_mode = False
            logging.info("Match läuft normal: Wechsle zurück in den 15-Sekunden-Modus.")


    @tasks.loop(seconds=30)
    async def update_leaderboard_ui(self):
        self.db_pool = await database.check_and_reconnect(self.db_pool)
        if not self.db_pool:
            return

        servers = [
            {"id": "server1", "title": "Server 1", "rcon_url": config.SERVER1_RCON_URL, "rcon_pass": config.SERVER1_RCON_PASS},
            {"id": "server2", "title": "Server 2", "rcon_url": config.SERVER2_RCON_URL, "rcon_pass": config.SERVER2_RCON_PASS},
            {"id": "server3", "title": "Server 3", "rcon_url": config.SERVER3_RCON_URL, "rcon_pass": config.SERVER3_RCON_PASS}
        ]
        
        channel = self.bot.get_channel(int(config.LEADERBOARD_CHANNEL_ID))
        if not channel:
            try:
                channel = await self.bot.fetch_channel(int(config.LEADERBOARD_CHANNEL_ID))
            except:
                return

        saved = self.get_saved_state()
        async with aiohttp.ClientSession() as session:
            for srv in servers:
                if not srv["rcon_url"] or not srv["rcon_pass"]:
                    continue

                banned_ids = await self.fetch_bans(session, srv["rcon_url"], srv["rcon_pass"])
                embed = await self.generate_embed(srv["id"], srv["title"], tf_key="7d", sort_by="kd", banned_ids=banned_ids, show_sort_text=False)
                view = PublicLeaderboardView(self, srv["id"])

                srv_state = saved.get(srv["id"], {})
                msg_id = srv_state.get("msg_id")

                message = None
                if msg_id:
                    try:
                        message = await channel.fetch_message(msg_id)
                        old_embed_dict = message.embeds[0].to_dict() if message.embeds else {}
                        new_embed_dict = embed.to_dict()
                        if old_embed_dict != new_embed_dict:
                            await message.edit(embed=embed, view=view)
                    except discord.NotFound:
                        message = None
                    except Exception as e:
                        logging.error(f"Error editing message for {srv['id']}: {e}")
                        message = None

                if message is None:
                    try:
                        new_message = await channel.send(embed=embed, view=view)
                        if srv["id"] not in saved:
                            saved[srv["id"]] = {}
                        saved[srv["id"]]["msg_id"] = new_message.id
                        self.save_state(saved)
                    except Exception as e:
                        logging.error(f"Error sending message for {srv['id']}: {e}")
    @update_leaderboard_data.before_loop
    async def before_update_leaderboard_data(self):
        await self.bot.wait_until_ready()
        self.db_pool = await database.get_db_pool()

    @update_leaderboard_ui.before_loop
    async def before_update_leaderboard_ui(self):
        await self.bot.wait_until_ready()
        self.db_pool = await database.get_db_pool()

async def setup(bot):
    await bot.add_cog(Leaderboard(bot))
