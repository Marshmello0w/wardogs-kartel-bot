import logging
import aiohttp
from discord.ext import tasks, commands

import config
import database

class BanTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db_pool = None
        self.check_new_bans.start()

    def cog_unload(self):
        self.check_new_bans.cancel()

    async def fetch_bans(self, session, rcon_url, rcon_pass):
        headers = {"Authorization": f"Bearer {rcon_pass}"}
        url = f"{rcon_url.rstrip('/')}/v1/bans"
        try:
            async with session.get(url, headers=headers, timeout=5) as response:
                if response.status == 200:
                    data = await response.json()
                    return [b["steamId"] for b in data.get("bans", []) if "steamId" in b]
        except Exception as e:
            logging.error(f"Error fetching bans for tracker: {e}")
        return []

    async def send_broadcast(self, session, rcon_url, rcon_pass, text):
        headers = {"Authorization": f"Bearer {rcon_pass}"}
        url = f"{rcon_url.rstrip('/')}/v1/broadcast"
        try:
            async with session.post(url, headers=headers, json={"message": text}, timeout=5) as response:
                if response.status == 200:
                    logging.info(f"Ban broadcast sent successfully: {text}")
                else:
                    logging.warning(f"Failed to send ban broadcast, HTTP {response.status}")
        except Exception as e:
            logging.error(f"Error sending ban broadcast: {e}")

    @tasks.loop(seconds=30)
    async def check_new_bans(self):
        self.db_pool = await database.check_and_reconnect(self.db_pool)
        if not self.db_pool:
            return

        servers = [
            {"id": "server1", "rcon_url": config.SERVER1_RCON_URL, "rcon_pass": config.SERVER1_RCON_PASS},
            {"id": "server2", "rcon_url": config.SERVER2_RCON_URL, "rcon_pass": config.SERVER2_RCON_PASS}
        ]

        async with aiohttp.ClientSession() as session:
            for srv in servers:
                if not srv["rcon_url"] or not srv["rcon_pass"]:
                    continue

                # Holt die aktuelle Bannliste vom Server
                current_bans = await self.fetch_bans(session, srv["rcon_url"], srv["rcon_pass"])
                if not current_bans:
                    continue

                # DB abgleichen und neue Banns ermitteln
                try:
                    new_bans = await database.sync_bans_and_get_new(self.db_pool, srv["id"], current_bans)
                except Exception as e:
                    logging.error(f"Error syncing bans with DB: {e}")
                    continue

                for steam_id in new_bans:
                    # Namen aus dem Leaderboard auslesen, falls vorhanden
                    try:
                        name = await database.get_player_name(self.db_pool, srv["id"], steam_id)
                    except Exception as e:
                        logging.error(f"Error getting player name: {e}")
                        name = None

                    display_name = name if name else "Ein Spieler"
                    
                    msg = f"{display_name} wurde vom Server verwiesen. Euer Admin-Team. Bei weiteren Auffälligkeiten gerne auf unserem Discord melden: https://discord.gg/bakuranikartell"
                    
                    # Broadcast in-game senden
                    await self.send_broadcast(session, srv["rcon_url"], srv["rcon_pass"], msg)
                    
                    import discord
                    self.bot.dispatch("bot_log", "🚨 Bann-Broadcast", f"Auf **{srv['id']}** wurde ein neuer Bann gemeldet.\nGesendet:\n```{msg}```", discord.Color.red())
                    
                    # In der DB als 'gesendet' markieren
                    try:
                        await database.mark_ban_announced(self.db_pool, srv["id"], steam_id)
                    except Exception as e:
                        logging.error(f"Error marking ban as announced: {e}")

    @check_new_bans.before_loop
    async def before_check_new_bans(self):
        await self.bot.wait_until_ready()
        self.db_pool = await database.get_db_pool()

async def setup(bot):
    await bot.add_cog(BanTracker(bot))
