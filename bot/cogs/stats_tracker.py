import asyncio
from time import monotonic
from discord.ext import commands, tasks
import config
import database
from permissions import valid_steam_id


class StatsTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.last_online = {}
        self.last_observed = {}
        self.track_stats.start()

    def cog_unload(self):
        self.track_stats.cancel()

    async def sample(self, srv):
        try:
            players = await self.bot.rcon.players(srv.id)
            now = monotonic()
            elapsed = now - self.last_observed.get(srv.id, now)
            seconds = int(elapsed) if 0 <= elapsed <= 90 else 0
            previous = self.last_online.get(srv.id, set())
            current = set()
            async with database.transaction() as cur:
                for player in players:
                    sid = str(player.get('steamId', ''))
                    if not valid_steam_id(sid):
                        continue
                    current.add(sid)
                    name = str(player.get('name', 'Unknown'))[:255]
                    credited = seconds if sid in previous else 0
                    await cur.execute("""INSERT INTO player_playtime(server_id,steam_id,name,playtime_seconds)
                        VALUES (%s,%s,%s,%s) ON DUPLICATE KEY UPDATE name=VALUES(name),
                        playtime_seconds=playtime_seconds+VALUES(playtime_seconds),last_seen=UTC_TIMESTAMP()""",
                        (srv.id, sid, name, credited))
                    await cur.execute("""INSERT INTO player_faction_stats(server_id,steam_id,faction,times_seen)
                        VALUES (%s,%s,%s,1) ON DUPLICATE KEY UPDATE times_seen=times_seen+1""",
                        (srv.id, sid, str(player.get('faction', 'Unknown'))[:50]))
                    ping = player.get('pingMs')
                    if isinstance(ping, (int,float)) and 0 < ping < 60000:
                        await cur.execute("""INSERT INTO player_ping_stats(server_id,steam_id,total_ping,ping_samples)
                            VALUES (%s,%s,%s,1) ON DUPLICATE KEY UPDATE total_ping=total_ping+VALUES(total_ping),
                            ping_samples=ping_samples+1""", (srv.id, sid, int(ping)))
                await cur.execute('INSERT INTO server_player_snapshots(server_id,player_count) VALUES (%s,%s)', (srv.id, len(current)))
            self.last_online[srv.id], self.last_observed[srv.id] = current, now
            self.bot.health.ok(f'Statistik-Erfassung {srv.title}')
        except Exception as exc:
            self.last_online.pop(srv.id, None)
            self.last_observed.pop(srv.id, None)
            self.bot.health.error(f'Statistik-Erfassung {srv.title}', exc)

    @tasks.loop(seconds=60)
    async def track_stats(self):
        await asyncio.gather(*(self.sample(s) for s in config.servers() if s.enabled))

    @track_stats.before_loop
    async def ready(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(StatsTracker(bot))
