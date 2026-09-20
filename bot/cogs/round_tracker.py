import asyncio
from collections import defaultdict
from datetime import datetime
import json
import discord
from discord.ext import commands, tasks
from core import config
from domain.rounds import advance
from infrastructure import database, storage


class RoundTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.states = {}
        self.locks = defaultdict(asyncio.Lock)
        self.poll.start()

    def cog_unload(self):
        self.poll.cancel()

    def current(self, server_id, max_age=15):
        state = self.states.get(server_id)
        if not state or state.get('uncertain'):
            return None
        age = (storage.utcnow() - datetime.fromisoformat(state['observed_at'])).total_seconds()
        return state if age <= max_age else None

    async def observe(self, server_id):
        async with self.locks[server_id]:
            if server_id not in self.states:
                self.states[server_id] = await storage.get_state('round', server_id)
            raw = await self.bot.rcon.get(server_id, '/v1/status')
            state, events = advance(self.states[server_id], raw, storage.utcnow().isoformat())
            async with database.transaction() as cur:
                await storage.put_state(cur, 'round', server_id, state)
                for event in events:
                    await cur.execute("""INSERT IGNORE INTO round_events
                        (id,server_id,round_id,kind,payload,created_at) VALUES (%s,%s,%s,%s,%s,%s)""",
                        (event['id'], server_id, event['round_id'], event['kind'],
                         json.dumps(event['data']), datetime.fromisoformat(event['observed_at'])))
                    if cur.rowcount and event['kind'] == 'round_ended':
                        await self.save_result(cur, server_id, event)
            self.states[server_id] = state
            for event in events:
                self.bot.dispatch(event['kind'], dict(event, server_id=server_id))
            return state

    async def save_result(self, cur, server_id, event):
        data = event['data']
        started = datetime.fromisoformat(data['started_at']) if data['started_at'] else None
        ended = datetime.fromisoformat(data['ended_at'])
        snap = data['snapshot']
        await cur.execute("""INSERT IGNORE INTO tracked_rounds(round_id,server_id,quality)
            VALUES (%s,%s,%s)""", (event['round_id'], server_id, data['quality']))
        if not cur.rowcount:
            return
        await cur.execute("""INSERT INTO round_history
            (server_id,winner_faction,map_name,experience,lighting,started_at,ended_at,duration_seconds)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (server_id, data.get('winner'), snap['map'][:100], ', '.join(snap['experiences'])[:255],
             str(snap.get('lighting', 'Unknown'))[:100], started, ended,
             max(0, int((ended-started).total_seconds())) if started else None))
        await cur.execute("UPDATE tracked_rounds SET history_id=%s WHERE round_id=%s", (cur.lastrowid, event['round_id']))
        if data.get('winner'):
            await cur.execute("""INSERT INTO faction_wins(server_id,faction,wins) VALUES (%s,%s,1)
                ON DUPLICATE KEY UPDATE wins=wins+1""", (server_id, data['winner'][:50]))
        await cur.execute("""INSERT INTO map_play_stats(server_id,map_name,times_played) VALUES (%s,%s,1)
            ON DUPLICATE KEY UPDATE times_played=times_played+1""", (server_id, snap['map'][:100]))

    async def sample_players(self, server_id):
        """Bracket player counters with status reads to avoid crossing a known reset."""
        before = await self.observe(server_id)
        if before['uncertain']:
            return None
        players = await self.bot.rcon.players(server_id)
        after = await self.observe(server_id)
        if after['uncertain'] or before['round_id'] != after['round_id']:
            return None
        return after, players

    async def poll_server(self, srv):
        key = f'Rundenstatus {srv.title}'
        try:
            await self.observe(srv.id)
            self.bot.health.ok(key)
        except Exception as exc:
            self.bot.health.error(key, exc)

    @tasks.loop(seconds=5)
    async def poll(self):
        await asyncio.gather(*(self.poll_server(s) for s in config.servers() if s.enabled))

    @poll.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(RoundTracker(bot))
