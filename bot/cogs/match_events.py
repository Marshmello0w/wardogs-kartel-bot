import logging
import aiohttp
from discord.ext import tasks, commands

import config

class MatchEvents(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Track whether the end-of-match broadcast was sent for a server
        self.broadcast_sent = {"server1": False, "server2": False}
        self.check_match_events.start()

    def cog_unload(self):
        self.check_match_events.cancel()

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

    async def send_broadcast(self, session, rcon_url, rcon_pass, text):
        headers = {"Authorization": f"Bearer {rcon_pass}"}
        url = f"{rcon_url.rstrip('/')}/v1/broadcast"
        try:
            async with session.post(url, headers=headers, json={"message": text}, timeout=5) as response:
                if response.status == 200:
                    logging.info(f"Broadcast sent successfully: {text}")
                else:
                    logging.warning(f"Failed to send broadcast, HTTP {response.status}")
        except Exception as e:
            logging.error(f"Error sending broadcast: {e}")

    @tasks.loop(seconds=5)
    async def check_match_events(self):
        servers = [
            {"id": "server1", "rcon_url": config.SERVER1_RCON_URL, "rcon_pass": config.SERVER1_RCON_PASS},
            {"id": "server2", "rcon_url": config.SERVER2_RCON_URL, "rcon_pass": config.SERVER2_RCON_PASS},
            {"id": "server3", "title": "Server 3", "rcon_url": config.SERVER3_RCON_URL, "rcon_pass": config.SERVER3_RCON_PASS}
        ]
        
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
                            
                    # Match is ending / has ended
                    if highest_score >= 100:
                        if not self.broadcast_sent.get(srv["id"], False):
                            msg = "Immer die neuesten News & Events zu WarDogs mitbekommen und neue Teamkollegen kennenlernen – hier geht’s zum Discord: https://discord.gg/bakuranikartell"
                            await self.send_broadcast(session, srv["rcon_url"], srv["rcon_pass"], msg)
                            self.broadcast_sent[srv["id"]] = True
                            
                            import discord
                            self.bot.dispatch("bot_log", "🏁 Runden-Ende Broadcast", f"Auf **{srv['id']}** endete eine Runde.\nGesendet:\n```{msg}```", discord.Color.gold())
                            
                    # Match restarted
                    elif highest_score < 50:
                        self.broadcast_sent[srv["id"]] = False

    @check_match_events.before_loop
    async def before_check_match_events(self):
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(MatchEvents(bot))
