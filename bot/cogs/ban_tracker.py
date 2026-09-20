import asyncio
import discord
from discord.ext import commands, tasks
from core import config
from infrastructure import database, storage
from services.rcon import RconError


class BanTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_new_bans.start()

    def cog_unload(self):
        self.check_new_bans.cancel()

    async def poll_server(self, srv):
        try:
            current = await self.bot.rcon.bans(srv.id)
            async with database.transaction() as cur:
                await cur.execute("SELECT payload FROM durable_state WHERE namespace='ban_initialized' AND item_key=%s", (srv.id,))
                initialized = await cur.fetchone() is not None
                await cur.execute('SELECT steam_id,announced FROM banned_players WHERE server_id=%s', (srv.id,))
                previous = {r['steam_id']: r['announced'] for r in await cur.fetchall()}
                for removed in set(previous) - current:
                    await cur.execute('DELETE FROM banned_players WHERE server_id=%s AND steam_id=%s', (srv.id, removed))
                    await cur.execute("""UPDATE global_bans SET status='external_removed'
                        WHERE steam_id=%s AND status='external' AND admin_mention=%s""",
                        (removed, f'{srv.title} (beobachtet)'))
                for steam_id in current - set(previous):
                    await cur.execute('INSERT INTO banned_players(server_id,steam_id,announced) VALUES (%s,%s,%s)',
                                      (srv.id, steam_id, not initialized))
                    await cur.execute("""SELECT steam_id FROM admin_targets WHERE steam_id=%s AND desired='ban'
                        AND (expires_at IS NULL OR expires_at>UTC_TIMESTAMP())""", (steam_id,))
                    if not await cur.fetchone():
                        # Keep the audit history without turning an observation into
                        # an authoritative permanent global admin decision.
                        await cur.execute("""INSERT INTO global_bans
                            (steam_id,reason,admin_mention,duration_str,status)
                            VALUES (%s,%s,%s,%s,'external')""",
                            (steam_id, 'Server-Ban beobachtet; Vergabezeit und Dauer unbekannt.',
                             f'{srv.title} (beobachtet)', 'Extern (Dauer unbekannt)'))
                await storage.put_state(cur, 'ban_initialized', srv.id, True)
                await cur.execute('SELECT steam_id FROM banned_players WHERE server_id=%s AND announced=FALSE', (srv.id,))
                announce = [r['steam_id'] for r in await cur.fetchall()]
            for steam_id in announce:
                # Observed per-server bans never overwrite the authoritative admin target.
                async with database.transaction() as cur:
                    await cur.execute('SELECT name FROM leaderboard WHERE server_id=%s AND steam_id=%s', (srv.id, steam_id))
                    row = await cur.fetchone()
                    await cur.execute('UPDATE banned_players SET announced=TRUE WHERE server_id=%s AND steam_id=%s', (srv.id, steam_id))
                name = row['name'] if row else 'Ein Spieler'
                text = f'{name} wurde vom Server verwiesen. Euer Admin-Team.'
                try:
                    reply = await self.bot.rcon.request(srv.id, 'POST', '/v1/broadcast', payload={'message': text})
                    if reply.status == 202:
                        raise RconError(202, uncertain=True)
                except RconError as exc:
                    if not exc.uncertain:
                        async with database.transaction() as cur:
                            await cur.execute('UPDATE banned_players SET announced=FALSE WHERE server_id=%s AND steam_id=%s', (srv.id, steam_id))
                    self.bot.health.error(f'Ban-Broadcast {srv.title}', exc)
                    continue
                self.bot.dispatch('bot_log', '🚨 Bann-Broadcast', f'{srv.title}: {text}', discord.Color.blue())
            self.bot.health.ok(f'Ban-Abgleich {srv.title}')
        except Exception as exc:
            self.bot.health.error(f'Ban-Abgleich {srv.title}', exc)

    @tasks.loop(seconds=30)
    async def check_new_bans(self):
        await asyncio.gather(*(self.poll_server(s) for s in config.servers() if s.enabled))

    @check_new_bans.before_loop
    async def ready(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(BanTracker(bot))
