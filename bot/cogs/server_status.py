import json
import os
import logging
import aiohttp
import discord
from discord.ext import tasks, commands

import config

class ServerStatus(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.update_status_embed.start()

    def cog_unload(self):
        self.update_status_embed.cancel()

    def get_saved_message_id(self):
        if os.path.exists(config.MESSAGE_ID_FILE):
            with open(config.MESSAGE_ID_FILE, "r") as f:
                data = json.load(f)
                return data.get("message_id")
        return None

    def save_message_id(self, message_id):
        with open(config.MESSAGE_ID_FILE, "w") as f:
            json.dump({"message_id": message_id}, f)

    async def fetch_server_data(self, session, server_id):
        url = f"{config.API_URL}?key=id|{server_id}"
        try:
            async with session.get(url, timeout=10) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    logging.warning(f"Failed to fetch {server_id}: HTTP {response.status}")
                    return None
        except Exception as e:
            logging.error(f"Error fetching {server_id}: {e}")
            return None

    @tasks.loop(seconds=60)
    async def update_status_embed(self):
        # Wait until bot is fully ready before running the task
        await self.bot.wait_until_ready()

        if not config.SERVER_STATUS_CHANNEL_ID:
            logging.error("SERVER_STATUS_CHANNEL_ID is not set in .env")
            return

        channel = self.bot.get_channel(int(config.SERVER_STATUS_CHANNEL_ID))
        if not channel:
            try:
                channel = await self.bot.fetch_channel(int(config.SERVER_STATUS_CHANNEL_ID))
            except Exception as e:
                logging.error(f"Could not fetch channel: {e}")
                return

        embed = discord.Embed(
            title="📡 WARDOGS Server Status", 
            color=discord.Color.green(),
            description="Live overview of our servers."
        )

        async with aiohttp.ClientSession() as session:
            for s_id in config.SERVER_IDS:
                data = await self.fetch_server_data(session, s_id)
                if data and "server" in data:
                    srv = data["server"]
                    name = srv.get("name", "Unknown Server")
                    players = srv.get("players", 0)
                    max_players = srv.get("maxPlayers", 0)
                    map_name = srv.get("map", "Unknown")
                    mode = srv.get("gameMode", "Unknown")
                    
                    value = (
                        f"**Status:** 🟢 Online\n"
                        f"**Players:** {players}/{max_players}\n"
                        f"**Map:** {map_name}\n"
                        f"**Mode:** {mode}\n"
                    )
                    embed.add_field(name=name, value=value, inline=False)
                else:
                    embed.add_field(
                        name=f"Server: {s_id}", 
                        value="**Status:** 🔴 Offline / Not Found", 
                        inline=False
                    )
        
        embed.set_footer(text="Updates every 60 seconds")

        message_id = self.get_saved_message_id()
        message = None

        if message_id:
            try:
                message = await channel.fetch_message(message_id)
                await message.edit(embed=embed)
                logging.info("Updated existing embed.")
            except discord.NotFound:
                logging.warning("Saved message not found, creating a new one.")
                message = None
            except Exception as e:
                logging.error(f"Error editing message: {e}")
                message = None

        if message is None:
            try:
                new_message = await channel.send(embed=embed)
                self.save_message_id(new_message.id)
                logging.info("Created new embed and saved message ID.")
            except Exception as e:
                logging.error(f"Error sending new message: {e}")

async def setup(bot):
    await bot.add_cog(ServerStatus(bot))
