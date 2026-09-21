"""Admin-controlled seed phases that award durable quest points from existing stats."""
from collections import defaultdict
from datetime import timedelta
from time import monotonic
from uuid import uuid4
import asyncio

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.permissions import require_admin, valid_steam_id
from infrastructure import database, storage


def valid_seed_faction(value):
    """Return the canonical real faction name, never White/Unknown."""
    candidate = str(value or '').strip()
    for team in config.QUEST_TEAMS:
        if candidate.casefold() == team.casefold():
            return team
    return None


def advance_presence(previous, seconds, intervals, observed_at):
    """Advance one continuous observation without crediting gaps or downtime.

    The returned fourth value is the number of newly completed 15-minute
    blocks.  ``intervals`` belongs to the current continuous visit only; the
    database separately retains the total already awarded blocks.
    """
    if previous is None:
        return observed_at, 0, 0, 0
    elapsed = int((observed_at - previous).total_seconds())
    if elapsed <= 0 or elapsed > config.SEED_MAX_OBSERVATION_GAP_SECONDS:
        return observed_at, 0, 0, 0
    total = max(0, int(seconds)) + elapsed
    completed = total // config.SEED_REWARD_INTERVAL_SECONDS
    return observed_at, total, completed, max(0, completed - int(intervals))


class SeedTracker(commands.Cog):
    """Issue anti-abuse seed rewards without making another RCON request."""

    health_key = 'Seed-Quest'

    def __init__(self, bot):
        self.bot = bot
        self.locks = defaultdict(asyncio.Lock)
        self.initialized_sessions = set()
        self.monitor.start()

    def cog_unload(self):
        self.monitor.cancel()

    async def _latest_roster(self, server_id, now):
        """Return the complete fresh StatsTracker roster or ``None`` if stale."""
        tracker = self.bot.get_cog('StatsTracker')
        observed = getattr(tracker, 'last_observed', {}).get(server_id) if tracker else None
        if observed is None or monotonic() - observed > config.SEED_MAX_OBSERVATION_GAP_SECONDS:
            return None
        online = set(getattr(tracker, 'last_online', {}).get(server_id, set()))
        cutoff = now - timedelta(seconds=config.SEED_MAX_OBSERVATION_GAP_SECONDS)
        if not online:
            return {}
        placeholders = ','.join('%s' for _ in online)
        async with database.transaction() as cur:
            await cur.execute(f'''SELECT steam_id,faction FROM player_faction_state
                WHERE server_id=%s AND last_seen >= %s AND steam_id IN ({placeholders})''',
                              (server_id, cutoff, *online))
            rows = await cur.fetchall()
        roster = {str(row['steam_id']): row['faction'] for row in rows if valid_steam_id(str(row['steam_id']))}
        # The process-local set is the exact latest response. A mismatch means
        # the database transaction has not completed yet, so wait rather than
        # accidentally carrying over a player from an earlier response.
        if online != set(roster):
            return None
        return roster

    async def _credit(self, cur, steam_id, amount, kind, reference_key, reason):
        """Append an idempotent ledger entry and update the point total."""
        await cur.execute('''INSERT INTO quest_point_ledger
            (steam_id,amount,kind,reference_key,reason) VALUES (%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE id=id''', (steam_id, amount, kind, reference_key, reason))
        if not cur.rowcount:
            return False
        await cur.execute('''INSERT INTO quest_points(steam_id,points) VALUES (%s,%s)
            ON DUPLICATE KEY UPDATE points=points+VALUES(points),updated_at=UTC_TIMESTAMP()''',
                          (steam_id, amount))
        return True

    async def _active_session(self, cur, server_id):
        await cur.execute('''SELECT state.session_id FROM seed_server_state state
            JOIN seed_sessions session ON session.id=state.session_id
            WHERE state.server_id=%s AND session.status='active' FOR UPDATE''', (server_id,))
        row = await cur.fetchone()
        return row['session_id'] if row else None

    async def _stop(self, server_id, reason, *, ended_by=None):
        async with database.transaction() as cur:
            session_id = await self._active_session(cur, server_id)
            if not session_id:
                return None
            await cur.execute('''UPDATE seed_sessions SET status='ended',ended_at=UTC_TIMESTAMP(),
                ended_by=%s,end_reason=%s WHERE id=%s''', (ended_by, reason, session_id))
            await cur.execute('UPDATE seed_server_state SET session_id=NULL WHERE server_id=%s', (server_id,))
        self.initialized_sessions.discard(session_id)
        return session_id

    async def _synchronise_participants(self, cur, session_id, server_id, roster, now):
        """Award joins and full continuous intervals for one fresh observation."""
        await cur.execute('SELECT * FROM seed_participants WHERE session_id=%s FOR UPDATE', (session_id,))
        known = {str(row['steam_id']): row for row in await cur.fetchall()}
        eligible = {steam_id for steam_id, faction in roster.items()
                    if valid_steam_id(steam_id) and valid_seed_faction(faction)}

        # A fresh absence is an observed leave. It does not erase the one-time
        # reward, but makes a later visit start a new 15-minute interval.
        for steam_id, row in known.items():
            if steam_id not in eligible:
                await cur.execute('''UPDATE seed_participants SET observed_at=NULL,
                    continuous_seconds=0,continuous_intervals=0 WHERE session_id=%s AND steam_id=%s''',
                                  (session_id, steam_id))

        granted = {'join': 0, 'intervals': 0}
        for steam_id in eligible:
            row = known.get(steam_id)
            if row is None:
                await cur.execute('''INSERT INTO seed_participants(session_id,steam_id,observed_at)
                    VALUES (%s,%s,%s)''', (session_id, steam_id, now))
                row = {'initial_awarded': 0, 'observed_at': now, 'continuous_seconds': 0,
                       'continuous_intervals': 0, 'awarded_intervals': 0}

            if not row['initial_awarded']:
                if await self._credit(cur, steam_id, 1, 'seed_join', f'{session_id}:join',
                                      f'Seed-Startphase auf {config.server(server_id).title}'):
                    granted['join'] += 1
                await cur.execute('''UPDATE seed_participants SET initial_awarded=1 WHERE session_id=%s AND steam_id=%s''',
                                  (session_id, steam_id))

            observed, seconds, continuous_intervals, new_intervals = advance_presence(
                row['observed_at'], row['continuous_seconds'], row['continuous_intervals'], now)
            total_awarded = int(row['awarded_intervals'])
            for offset in range(1, new_intervals + 1):
                if await self._credit(cur, steam_id, 1, 'seed_playtime',
                                      f'{session_id}:15m:{total_awarded + offset}',
                                      f'15 Minuten Seed-Startphase auf {config.server(server_id).title}'):
                    granted['intervals'] += 1
            await cur.execute('''UPDATE seed_participants SET observed_at=%s,continuous_seconds=%s,
                continuous_intervals=%s,awarded_intervals=%s WHERE session_id=%s AND steam_id=%s''',
                              (observed, seconds, continuous_intervals, total_awarded + new_intervals,
                               session_id, steam_id))
        return granted

    async def _activate(self, server_id, admin_mention):
        now = storage.utcnow()
        roster = await self._latest_roster(server_id, now)
        if roster is None:
            return None, 'Der Spielerstand ist gerade nicht aktuell genug. Bitte gleich erneut versuchen.'
        if len(roster) >= config.SEED_PLAYER_LIMIT:
            return None, f'Der Server hat bereits {len(roster)} Spieler; eine Startphase ist nicht nötig.'
        session_id = str(uuid4())
        async with database.transaction() as cur:
            await cur.execute('''INSERT INTO seed_server_state(server_id) VALUES (%s)
                ON DUPLICATE KEY UPDATE server_id=VALUES(server_id)''', (server_id,))
            if await self._active_session(cur, server_id):
                return None, 'Für diesen Server ist bereits eine Startphase aktiv.'
            await cur.execute('''INSERT INTO seed_sessions(id,server_id,activated_by) VALUES (%s,%s,%s)''',
                              (session_id, server_id, admin_mention[:100]))
            await cur.execute('UPDATE seed_server_state SET session_id=%s WHERE server_id=%s',
                              (session_id, server_id))
            await self._synchronise_participants(cur, session_id, server_id, roster, now)
        self.initialized_sessions.add(session_id)
        return session_id, None

    async def _process_server(self, server_id):
        now = storage.utcnow()
        roster = await self._latest_roster(server_id, now)
        if roster is None:
            return
        if len(roster) >= config.SEED_PLAYER_LIMIT:
            session_id = await self._stop(server_id, 'player_limit')
            if session_id:
                self.bot.dispatch('bot_log', '🌱 Seed-Startphase beendet',
                                  f'{config.server(server_id).title} hat **{len(roster)}** Spieler erreicht.',
                                  discord.Color.green())
            return
        async with database.transaction() as cur:
            session_id = await self._active_session(cur, server_id)
            if not session_id:
                return
            # A restart must never bridge an unobserved time gap. Existing
            # initial rewards remain immutable; only the timer restarts.
            if session_id not in self.initialized_sessions:
                await cur.execute('''UPDATE seed_participants SET observed_at=NULL,
                    continuous_seconds=0,continuous_intervals=0 WHERE session_id=%s''', (session_id,))
                self.initialized_sessions.add(session_id)
            await self._synchronise_participants(cur, session_id, server_id, roster, now)

    @tasks.loop(seconds=60)
    async def monitor(self):
        try:
            async with database.transaction() as cur:
                await cur.execute('''SELECT state.server_id FROM seed_server_state state
                    JOIN seed_sessions session ON session.id=state.session_id WHERE session.status='active' ''')
                active = [str(row['server_id']) for row in await cur.fetchall()]
            for server_id in active:
                async with self.locks[server_id]:
                    await self._process_server(server_id)
            self.bot.health.ok(self.health_key)
        except Exception as exc:
            self.bot.health.error(self.health_key, exc)

    @monitor.before_loop
    async def before_monitor(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name='seed', description='Startet oder beendet eine Server-Startphase.')
    @app_commands.choices(
        server=[app_commands.Choice(name=f'Server {number}', value=f'server{number}') for number in range(1, 4)],
        status=[app_commands.Choice(name='Aktivieren', value='on'),
                app_commands.Choice(name='Deaktivieren', value='off')])
    async def seed(self, interaction: discord.Interaction, server: app_commands.Choice[str],
                   status: app_commands.Choice[str]):
        if not await require_admin(interaction):
            return
        if not config.server(server.value).enabled:
            await interaction.response.send_message('Dieser Server ist nicht konfiguriert.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with self.locks[server.value]:
            if status.value == 'on':
                session_id, problem = await self._activate(server.value, interaction.user.mention)
                if problem:
                    await interaction.followup.send(f'⚠️ {problem}', ephemeral=True)
                    return
                self.bot.dispatch('bot_log', '🌱 Seed-Startphase aktiviert',
                                  f'{interaction.user.mention} aktivierte die Startphase für '
                                  f'**{config.server(server.value).title}**.', discord.Color.blue())
                await interaction.followup.send('✅ Startphase aktiviert. Berechtigte Spieler erhalten nun Seed-Punkte.',
                                                 ephemeral=True)
                return

            session_id = await self._stop(server.value, 'manual', ended_by=interaction.user.mention)
            if not session_id:
                await interaction.followup.send('Für diesen Server ist keine Startphase aktiv.', ephemeral=True)
                return
            self.bot.dispatch('bot_log', '🌱 Seed-Startphase deaktiviert',
                              f'{interaction.user.mention} beendete die Startphase für '
                              f'**{config.server(server.value).title}**.', discord.Color.orange())
            await interaction.followup.send('✅ Startphase beendet.', ephemeral=True)


async def setup(bot):
    await bot.add_cog(SeedTracker(bot))
