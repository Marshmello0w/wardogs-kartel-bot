import asyncio
from time import monotonic
from discord.ext import commands, tasks
from core import config
from core.permissions import valid_steam_id
from infrastructure import database


class StatsTracker(commands.Cog):
    NON_TEAM_FACTIONS = frozenset({'white'})

    def __init__(self, bot):
        self.bot = bot
        self.last_online = {}
        self.last_observed = {}
        self.track_stats.start()

    def cog_unload(self):
        self.track_stats.cancel()

    @staticmethod
    def faction_name(player):
        """Return a stored faction label without treating a missing value as a team."""
        faction = str(player.get('faction', '')).strip()[:50]
        if not faction or faction.casefold() in StatsTracker.NON_TEAM_FACTIONS:
            return 'Unknown'
        return faction

    async def recent_factions(self, cur, server_id, steam_ids):
        """Return faction state from the preceding polling window only.

        Persisting this small amount of state avoids a bogus new entry when the
        bot restarts during a normal poll interval. A state older than 90 seconds
        is instead a new observed server visit.
        """
        if not steam_ids:
            return {}
        placeholders = ','.join('%s' for _ in steam_ids)
        await cur.execute(f'''SELECT steam_id,faction FROM player_faction_state
            WHERE server_id=%s AND last_seen>=DATE_SUB(UTC_TIMESTAMP(), INTERVAL 90 SECOND)
            AND steam_id IN ({placeholders})''', (server_id, *steam_ids))
        return {str(row['steam_id']): row['faction'] for row in await cur.fetchall()}

    async def sample(self, srv):
        try:
            players = await self.bot.rcon.players(srv.id)
            now = monotonic()
            elapsed = now - self.last_observed.get(srv.id, now)
            seconds = int(elapsed) if 0 <= elapsed <= 90 else 0
            previous = self.last_online.get(srv.id, set())
            current = {}
            for player in players:
                sid = str(player.get('steamId', ''))
                if valid_steam_id(sid):
                    # A Steam ID must only be processed once per RCON response.
                    current[sid] = player
            async with database.transaction() as cur:
                recent_factions = await self.recent_factions(cur, srv.id, tuple(current))
                for sid, player in current.items():
                    name = str(player.get('name', 'Unknown'))[:255]
                    credited = seconds if sid in previous else 0
                    await cur.execute("""INSERT INTO player_playtime(server_id,steam_id,name,playtime_seconds)
                        VALUES (%s,%s,%s,%s) ON DUPLICATE KEY UPDATE name=VALUES(name),
                        playtime_seconds=playtime_seconds+VALUES(playtime_seconds),last_seen=UTC_TIMESTAMP()""",
                        (srv.id, sid, name, credited))
                    faction = self.faction_name(player)
                    # Count a detected entry once: when joining after a gap or
                    # changing faction. Repeated 60-second polls do not alter it.
                    if faction != 'Unknown' and recent_factions.get(sid) != faction:
                        await cur.execute("""INSERT INTO player_faction_stats(server_id,steam_id,faction,times_seen)
                            VALUES (%s,%s,%s,1) ON DUPLICATE KEY UPDATE times_seen=times_seen+1""",
                            (srv.id, sid, faction))
                    await cur.execute("""INSERT INTO player_faction_state(server_id,steam_id,faction,last_seen)
                        VALUES (%s,%s,%s,UTC_TIMESTAMP()) ON DUPLICATE KEY UPDATE
                        faction=VALUES(faction),last_seen=VALUES(last_seen)""", (srv.id, sid, faction))
                    ping = player.get('pingMs')
                    if isinstance(ping, (int,float)) and 0 < ping < 60000:
                        await cur.execute("""INSERT INTO player_ping_stats(server_id,steam_id,total_ping,ping_samples)
                            VALUES (%s,%s,%s,1) ON DUPLICATE KEY UPDATE total_ping=total_ping+VALUES(total_ping),
                            ping_samples=ping_samples+1""", (srv.id, sid, int(ping)))
                await cur.execute('INSERT INTO server_player_snapshots(server_id,player_count) VALUES (%s,%s)', (srv.id, len(current)))
            self.last_online[srv.id], self.last_observed[srv.id] = set(current), now
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
