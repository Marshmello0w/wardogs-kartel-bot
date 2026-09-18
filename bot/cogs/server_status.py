import logging
import json
import os
import aiohttp
import discord
from discord.ext import tasks, commands

import config
import database

class ServerStatus(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db_pool = None
        self.last_regions = {}
        self.update_status_embed.start()

    def cog_unload(self):
        self.update_status_embed.cancel()

    def get_saved_message_id(self):
        if os.path.exists(config.MESSAGE_ID_FILE):
            with open(config.MESSAGE_ID_FILE, "r") as f:
                try:
                    data = json.load(f)
                    return data.get("message_id")
                except:
                    pass
        return None

    def save_message_id(self, message_id):
        with open(config.MESSAGE_ID_FILE, "w") as f:
            json.dump({"message_id": message_id}, f)

    async def fetch_status_rcon(self, session, rcon_url, rcon_pass):
        headers = {"Authorization": f"Bearer {rcon_pass}"}
        url = f"{rcon_url.rstrip('/')}/v1/status"
        try:
            async with session.get(url, headers=headers, timeout=5) as response:
                if response.status == 200:
                    return await response.json()
        except Exception as e:
            pass # Silent fail to avoid spamming logs when offline
        return None

    async def fetch_wardogs_api(self, session, server_id):
        url = f"https://wardogserverlist.com/api/server?key=id|{server_id}"
        try:
            async with session.get(url, timeout=5) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("server", {}).get("region", "Unknown")
        except Exception:
            pass
        return "Unknown"

    @tasks.loop(seconds=60)

    async def update_status_embed(self):
        self.db_pool = await database.check_and_reconnect(self.db_pool)
        if not self.db_pool:
            logging.warning("Database unavailable, skipping status update this round.")
            return

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
            title="Das Kartell Server Status", 
            color=discord.Color.green()
        )

        servers = [
            {"id": "server1", "uuid": config.SERVER_IDS[0] if len(config.SERVER_IDS) > 0 else "", "rcon_url": config.SERVER1_RCON_URL, "rcon_pass": config.SERVER1_RCON_PASS},
            {"id": "server2", "uuid": config.SERVER_IDS[1] if len(config.SERVER_IDS) > 1 else "", "rcon_url": config.SERVER2_RCON_URL, "rcon_pass": config.SERVER2_RCON_PASS},
            {"id": "server3", "uuid": config.SERVER_IDS[2] if len(config.SERVER_IDS) > 2 else "server3_uuid_placeholder", "rcon_url": config.SERVER3_RCON_URL, "rcon_pass": config.SERVER3_RCON_PASS}
        ]

        async with aiohttp.ClientSession() as session:
            for srv in servers:
                s_id = srv["uuid"]
                
                is_online = False
                data = None
                if srv["rcon_url"] and srv["rcon_pass"]:
                    data = await self.fetch_status_rcon(session, srv["rcon_url"], srv["rcon_pass"])
                    if data:
                        is_online = True
                
                # Log uptime in database
                try:
                    await database.log_uptime(self.db_pool, s_id, is_online)
                    stats = await database.get_uptime_stats(self.db_pool, s_id)
                except Exception as e:
                    logging.error(f"DB Error during uptime check: {e}")
                    continue
                uptime_str = f"24h: {stats['24h']} | 7d: {stats['7d']} | 30d: {stats['30d']}"
                
                if is_online:
                    name = data.get("serverName", "Unknown Server")
                    players = data.get("players", {}).get("current", 0)
                    max_players = data.get("players", {}).get("max", 0)
                    map_name = data.get("map", "Unknown")
                    
                    # Fetch region from Master Server List since RCON doesn't provide it
                    region = await self.fetch_wardogs_api(session, s_id)
                    if region != "Unknown":
                        self.last_regions[s_id] = region
                    else:
                        region = self.last_regions.get(s_id, "Unknown")
                    region = region.upper()
                    
                    experiences = data.get("experiences", [])
                    mode = "Unknown"
                    if experiences:
                        main_exp = experiences[0]
                        if "_" in main_exp:
                            mode = main_exp.split("_")[1] # z.B. "Bakurani_KOTH_01" -> "KOTH"
                        else:
                            mode = main_exp
                            
                        modifiers = []
                        for exp in experiences[1:]:
                            if exp == "KOTH_InfantryOnly":
                                modifiers.append("Infantry Only")
                            elif exp == "KOTH_Hardcore":
                                modifiers.append("Hardcore")
                            else:
                                modifiers.append(exp.replace("KOTH_", "").replace("_", " "))
                                
                        if modifiers:
                            mode += " + " + " + ".join(modifiers)
                    
                    value = (
                        f"**Status:** 🟢 Online\n"
                        f"**Region:** {region}\n"
                        f"**Players:** {players}/{max_players}\n"
                        f"**Map:** {map_name}\n"
                        f"**Mode:** {mode}\n"
                        f"**Connection ID:** `{s_id}`\n"
                        f"**Uptime:** {uptime_str}\n"
                    )
                    embed.add_field(name=name, value=value, inline=False)
                else:
                    embed.add_field(
                        name=f"Server: {s_id}", 
                        value=(
                            f"**Status:** 🔴 Offline / Not Found\n"
                            f"**Uptime:** {uptime_str}\n"
                        ), 
                        inline=False
                    )
        
        # Markdown-Timestamp
        import time
        current_time = int(time.time())
        embed.add_field(
            name="\u200b", 
            value=f"Letzte Aktualisierung: <t:{current_time}:t>", 
            inline=False
        )

        message_id = self.get_saved_message_id()
        message = None

        if message_id:
            try:
                message = await channel.fetch_message(message_id)
                old_embed_dict = message.embeds[0].to_dict() if message.embeds else {}
                new_embed_dict = embed.to_dict()
                if old_embed_dict != new_embed_dict:
                    await message.edit(embed=embed)
            except discord.NotFound:
                message = None
            except Exception as e:
                logging.error(f"Error editing message: {e}")
                message = None

        if message is None:
            try:
                new_message = await channel.send(embed=embed)
                self.save_message_id(new_message.id)
            except Exception as e:
                logging.error(f"Error sending message: {e}")

    @update_status_embed.before_loop
    async def before_update_status_embed(self):
        await self.bot.wait_until_ready()
        self.db_pool = await database.get_db_pool()

async def setup(bot):
    await bot.add_cog(ServerStatus(bot))
