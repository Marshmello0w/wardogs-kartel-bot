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

    def get_saved_message_id(self):
        if os.path.exists(config.LEADERBOARD_MSG_FILE):
            with open(config.LEADERBOARD_MSG_FILE, "r") as f:
                data = json.load(f)
                return data.get("message_id")
        return None

    def save_message_id(self, message_id):
        with open(config.LEADERBOARD_MSG_FILE, "w") as f:
            json.dump({"message_id": message_id}, f)

    async def fetch_players(self, session):
        headers = {"Authorization": f"Bearer {config.SERVER2_RCON_PASS}"}
        url = f"{config.SERVER2_RCON_URL.rstrip('/')}/v1/players"
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
        if not config.LEADERBOARD_CHANNEL_ID:
            logging.error("LEADERBOARD_CHANNEL_ID is not set.")
            return

        channel = self.bot.get_channel(int(config.LEADERBOARD_CHANNEL_ID))
        if not channel:
            try:
                channel = await self.bot.fetch_channel(int(config.LEADERBOARD_CHANNEL_ID))
            except Exception as e:
                logging.error(f"Could not fetch leaderboard channel: {e}")
                return

        # RCON Abfrage
        async with aiohttp.ClientSession() as session:
            players = await self.fetch_players(session)
            
            if players is not None:
                # Update Database
                for p in players:
                    steam_id = p.get("steamId")
                    name = p.get("name", "Unknown")
                    kills = p.get("kills", 0)
                    deaths = p.get("deaths", 0)
                    cash = p.get("cash", 0)
                    
                    if steam_id:
                        await database.update_player_stats(
                            self.db_pool, 
                            "server2", 
                            steam_id, 
                            name, 
                            kills, 
                            deaths, 
                            cash
                        )


        # Get Top 10 from Database for different timeframes
        timeframes = [
            ("7d", "Top 10 (Letzte 7 Tage)"),
            ("30d", "Top 10 (Letzte 30 Tage)"),
            ("all", "Top 10 (All-Time)")
        ]
        
        embeds = {}
        for tf_key, tf_title in timeframes:
            top_players = await database.get_top_players(self.db_pool, "server2", timeframe=tf_key, limit=10)

            embed = discord.Embed(
                title=f"🏆 Server 2 - {tf_title}", 
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
                name="​",
                value=f"Letzte Aktualisierung: <t:{current_time}:t>", 
                inline=False
            )
            embeds[tf_key] = embed

        # Read saved message IDs (dict mapping tf_key -> message_id)
        saved_msgs = {}
        if os.path.exists(config.LEADERBOARD_MSG_FILE):
            try:
                with open(config.LEADERBOARD_MSG_FILE, "r") as f:
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
                    logging.info(f"Updated existing leaderboard embed ({tf_key}).")
                except discord.NotFound:
                    logging.warning(f"Leaderboard message {tf_key} not found, creating a new one.")
                    message = None
                except Exception as e:
                    logging.error(f"Error editing leaderboard message {tf_key}: {e}")
                    message = None

            if message is None:
                try:
                    new_message = await channel.send(embed=embed)
                    new_saved_msgs[tf_key] = new_message.id
                    logging.info(f"Created new leaderboard embed ({tf_key}).")
                except Exception as e:
                    logging.error(f"Error sending leaderboard message {tf_key}: {e}")
            else:
                new_saved_msgs[tf_key] = msg_id

        # Save the message IDs
        with open(config.LEADERBOARD_MSG_FILE, "w") as f:
            json.dump(new_saved_msgs, f)

    @update_leaderboard.before_loop

    async def before_update_leaderboard(self):
        await self.bot.wait_until_ready()
        self.db_pool = await database.get_db_pool()

async def setup(bot):
    await bot.add_cog(Leaderboard(bot))
