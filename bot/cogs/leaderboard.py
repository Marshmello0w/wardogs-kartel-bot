import json
import os
import logging
import aiohttp
import discord
from discord.ext import tasks, commands
import time

import config
import database

class Leaderboard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db_pool = None
        self.update_leaderboard.start()

    def cog_unload(self):
        self.update_leaderboard.cancel()

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

    @tasks.loop(seconds=15)
    async def update_leaderboard(self):
        servers = [
            {
                "id": "server1",
                "title": "Server 1",
                "rcon_url": config.SERVER1_RCON_URL,
                "rcon_pass": config.SERVER1_RCON_PASS,
                "channel_id": config.LEADERBOARD_CHANNEL_ID_1,
                "msg_file": "leaderboard_msg_1.json"
            },
            {
                "id": "server2",
                "title": "Server 2",
                "rcon_url": config.SERVER2_RCON_URL,
                "rcon_pass": config.SERVER2_RCON_PASS,
                "channel_id": config.LEADERBOARD_CHANNEL_ID_2,
                "msg_file": "leaderboard_msg_2.json"
            }
        ]

        async with aiohttp.ClientSession() as session:
            for srv in servers:
                if not srv["channel_id"] or not srv["rcon_url"] or not srv["rcon_pass"]:
                    continue

                channel = self.bot.get_channel(int(srv["channel_id"]))
                if not channel:
                    try:
                        channel = await self.bot.fetch_channel(int(srv["channel_id"]))
                    except Exception as e:
                        logging.error(f"Could not fetch leaderboard channel for {srv['id']}: {e}")
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
                            await database.update_player_stats(
                                self.db_pool, 
                                srv["id"], 
                                steam_id, 
                                name, 
                                kills, 
                                deaths, 
                                cash
                            )

                timeframes = [
                    ("7d", f"Top 10 (Letzte 7 Tage)"),
                    ("30d", f"Top 10 (Letzte 30 Tage)"),
                    ("all", f"Top 10 (All-Time)")
                ]
                
                embeds = {}
                for tf_key, tf_title in timeframes:
                    top_players = await database.get_top_players(self.db_pool, srv["id"], timeframe=tf_key, limit=10)

                    embed = discord.Embed(
                        title=f"🏆 {srv['title']} - {tf_title}", 
                        color=discord.Color.gold()
                    )

                    if not top_players:
                        embed.add_field(name="No Data", value="No players have been recorded yet.", inline=False)
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
                    embeds[tf_key] = embed

                saved_msgs = {}
                if os.path.exists(srv["msg_file"]):
                    try:
                        with open(srv["msg_file"], "r", encoding="utf-8") as f:
                            saved_msgs = json.load(f)
                    except:
                        pass

                new_saved_msgs = {}
                for tf_key, _ in timeframes:
                    embed = embeds[tf_key]
                    msg_id = saved_msgs.get(tf_key)
                    message = None

                    if msg_id:
                        try:
                            message = await channel.fetch_message(msg_id)
                            await message.edit(embed=embed)
                        except discord.NotFound:
                            message = None
                        except Exception as e:
                            logging.error(f"Error editing {srv['id']} message {tf_key}: {e}")
                            message = None

                    if message is None:
                        try:
                            new_message = await channel.send(embed=embed)
                            new_saved_msgs[tf_key] = new_message.id
                        except Exception as e:
                            logging.error(f"Error sending {srv['id']} message {tf_key}: {e}")
                    else:
                        new_saved_msgs[tf_key] = msg_id

                with open(srv["msg_file"], "w", encoding="utf-8") as f:
                    json.dump(new_saved_msgs, f)

    @update_leaderboard.before_loop
    async def before_update_leaderboard(self):
        await self.bot.wait_until_ready()
        self.db_pool = await database.get_db_pool()

async def setup(bot):
    await bot.add_cog(Leaderboard(bot))