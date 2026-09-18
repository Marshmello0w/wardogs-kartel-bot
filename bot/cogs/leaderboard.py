import json
import os
import logging
import aiohttp
import discord
from discord.ext import tasks, commands
import time

import config
import database

class LeaderboardDropdown(discord.ui.Select):
    def __init__(self, cog, server_id, current_tf):
        self.cog = cog
        self.server_id = server_id
        options = [
            discord.SelectOption(label="Letzte 7 Tage", value="7d", description="Top 10 der letzten 7 Tage", default=(current_tf == "7d"), emoji="📅"),
            discord.SelectOption(label="Letzte 30 Tage", value="30d", description="Top 10 der letzten 30 Tage", default=(current_tf == "30d"), emoji="📆"),
            discord.SelectOption(label="All-Time", value="all", description="All-Time Top 10", default=(current_tf == "all"), emoji="🏆")
        ]
        super().__init__(placeholder="Wähle einen Zeitraum...", min_values=1, max_values=1, options=options, custom_id=f"lb_select_{server_id}")

    async def callback(self, interaction: discord.Interaction):
        new_tf = self.values[0]
        
        # Lade State und speichere neuen TF
        saved = self.cog.get_saved_state()
        if self.server_id not in saved:
            saved[self.server_id] = {}
        saved[self.server_id]["tf"] = new_tf
        self.cog.save_state(saved)
        
        await interaction.response.defer()
        
        # Das Update wird direkt geforced
        await self.cog.force_update_message(self.server_id, interaction.message)


class LeaderboardView(discord.ui.View):
    def __init__(self, cog, server_id, current_tf):
        super().__init__(timeout=None)
        self.add_item(LeaderboardDropdown(cog, server_id, current_tf))


class Leaderboard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db_pool = None
        self.state_file = "leaderboard_state.json"
        
        # Load views persistently
        saved = self.get_saved_state()
        for server_id, data in saved.items():
            tf = data.get("tf", "7d")
            self.bot.add_view(LeaderboardView(self, server_id, tf))
            
        self.update_leaderboard.start()

    def cog_unload(self):
        self.update_leaderboard.cancel()

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

    async def fetch_players(self, session, rcon_url, rcon_pass):
        headers = {"Authorization": f"Bearer {rcon_pass}"}
        url = f"{rcon_url.rstrip('/')}/v1/players"
        try:
            async with session.get(url, headers=headers, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("players", [])
                else:
                    logging.warning(f"Failed to fetch leaderboard: HTTP {response.status}")
                    return None
        except Exception as e:
            logging.error(f"Error fetching leaderboard: {e}")
            return None

    async def generate_embed(self, server_id, server_title, tf_key):
        top_players = await database.get_top_players(self.db_pool, server_id, timeframe=tf_key, limit=10)

        titles = {"7d": "Letzte 7 Tage", "30d": "Letzte 30 Tage", "all": "All-Time"}
        
        embed = discord.Embed(
            title=f"🏆 {server_title} - {titles[tf_key]}", 
            color=discord.Color.gold()
        )

        if not top_players:
            embed.add_field(name="No Data", value="Noch keine Spielerdaten vorhanden.", inline=False)
        else:
            rank = 1
            for p in top_players:
                kills = p['kills']
                deaths = p['deaths']
                kd = round(kills / deaths, 2) if deaths > 0 else kills
                
                val = f"**Kills:** {kills} | **Deaths:** {deaths} | **K/D:** {kd}"
                
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

    async def force_update_message(self, server_id, message):
        saved = self.get_saved_state()
        tf = saved.get(server_id, {}).get("tf", "7d")
        
        title = "Server 1" if server_id == "server1" else "Server 2"
        embed = await self.generate_embed(server_id, title, tf)
        view = LeaderboardView(self, server_id, tf)
        
        try:
            await message.edit(embed=embed, view=view)
        except Exception as e:
            logging.error(f"Error forced updating message: {e}")

    @tasks.loop(seconds=15)
    async def update_leaderboard(self):
        self.db_pool = await database.check_and_reconnect(self.db_pool)
        if not self.db_pool:
            logging.warning("Database unavailable, skipping leaderboard update this round.")
            return

        # Wir nutzen nun LEADERBOARD_CHANNEL_ID
 als Hauptkanal für beide Server
        channel_id_str = config.LEADERBOARD_CHANNEL_ID_1 or config.LEADERBOARD_CHANNEL_ID_2
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
            }
        ]

        saved = self.get_saved_state()

        async with aiohttp.ClientSession() as session:
            for srv in servers:
                if not srv["rcon_url"] or not srv["rcon_pass"]:
                    continue

                players = await self.fetch_players(session, srv["rcon_url"], srv["rcon_pass"])
                
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

                srv_state = saved.get(srv["id"], {})
                tf = srv_state.get("tf", "7d")
                msg_id = srv_state.get("msg_id")

                embed = await self.generate_embed(srv["id"], srv["title"], tf)
                view = LeaderboardView(self, srv["id"], tf)

                message = None
                if msg_id:
                    try:
                        message = await channel.fetch_message(msg_id)
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
                        saved[srv["id"]]["tf"] = tf
                        self.save_state(saved)
                    except Exception as e:
                        logging.error(f"Error sending message for {srv['id']}: {e}")

    @update_leaderboard.before_loop
    async def before_update_leaderboard(self):
        await self.bot.wait_until_ready()
        self.db_pool = await database.get_db_pool()

async def setup(bot):
    await bot.add_cog(Leaderboard(bot))