import logging
import discord
import aiohttp
from discord.ext import tasks, commands

import config
import database


class PlaytimeTracker(commands.Cog):
    """Trackt die Spielzeit jedes Spielers auf allen 3 Servern."""

    def __init__(self, bot):
        self.bot = bot
        self.db_pool = None
        # Speichert pro Server welche SteamIDs beim letzten Check online waren
        self.last_online = {"server1": set(), "server2": set(), "server3": set()}
        self.track_playtime.start()

    def cog_unload(self):
        self.track_playtime.cancel()

    async def fetch_online_players(self, session, rcon_url, rcon_pass, server_title="Unknown"):
        """Holt die Liste der aktuell online Spieler vom Server."""
        headers = {"Authorization": f"Bearer {rcon_pass}"}
        url = f"{rcon_url.rstrip('/')}/v1/players"
        try:
            async with session.get(url, headers=headers, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("players", [])
        except Exception as e:
            logging.error(f"Error fetching players for playtime on {server_title}: {e}")
        return None

    @tasks.loop(seconds=60)
    async def track_playtime(self):
        self.db_pool = await database.check_and_reconnect(self.db_pool)
        if not self.db_pool:
            return

        servers = [
            {"id": "server1", "title": "Server 1", "rcon_url": config.SERVER1_RCON_URL, "rcon_pass": config.SERVER1_RCON_PASS},
            {"id": "server2", "title": "Server 2", "rcon_url": config.SERVER2_RCON_URL, "rcon_pass": config.SERVER2_RCON_PASS},
            {"id": "server3", "title": "Server 3", "rcon_url": config.SERVER3_RCON_URL, "rcon_pass": config.SERVER3_RCON_PASS},
        ]

        async with aiohttp.ClientSession() as session:
            for srv in servers:
                if not srv["rcon_url"] or not srv["rcon_pass"]:
                    continue

                players = await self.fetch_online_players(session, srv["rcon_url"], srv["rcon_pass"], srv["title"])
                if players is None:
                    continue

                current_online = set()
                for p in players:
                    steam_id = p.get("steamId")
                    name = p.get("name", "Unknown")
                    if steam_id:
                        current_online.add(steam_id)
                        # Spieler war auch beim letzten Check schon online → 60 Sekunden addieren
                        if steam_id in self.last_online.get(srv["id"], set()):
                            try:
                                await self.add_playtime(srv["id"], steam_id, name, 60)
                            except Exception as e:
                                logging.error(f"Error updating playtime for {steam_id} on {srv['id']}: {e}")

                self.last_online[srv["id"]] = current_online

    async def add_playtime(self, server_id, steam_id, name, seconds):
        """Fügt Spielzeit in der DB hinzu (pro Server)."""
        import aiomysql
        async with self.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute('''
                    INSERT INTO player_playtime (server_id, steam_id, name, playtime_seconds)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        name = VALUES(name),
                        playtime_seconds = playtime_seconds + VALUES(playtime_seconds),
                        last_seen = NOW()
                ''', (server_id, steam_id, name, seconds))

    @track_playtime.before_loop
    async def before_track_playtime(self):
        await self.bot.wait_until_ready()
        self.db_pool = await database.get_db_pool()


async def setup(bot):
    await bot.add_cog(PlaytimeTracker(bot))
