"""Round and Berlin-day quests from the existing, bracketed player samples."""
import asyncio
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

from core import config
from core.permissions import valid_steam_id
from cogs.quest_tracker import round_is_active_for_quests
from infrastructure import database


BERLIN = ZoneInfo('Europe/Berlin')
MAX_SAMPLE_GAP_SECONDS = 30
ROUND_POINT_LIMIT = 15
ROUND_QUESTS = (
    ('first', 'Erster Einsatz', lambda row: row['kills'] >= 5),
    ('streak', 'Killserie', lambda row: row['best_streak'] >= 5),
    ('cash', 'Verdiener', lambda row: row['cash'] >= 50_000),
    ('time', 'Durchhalten', lambda row: row['active_seconds'] >= 40 * 60),
    ('fighter', 'Frontkämpfer', lambda row: row['kills'] >= 10 and row['deaths'] >= 3),
)
DAILY_QUESTS = (
    ('hunter', 'Tagesjäger', lambda row, rounds: row['kills'] >= 25),
    ('big_hunt', 'Großjagd', lambda row, rounds: row['kills'] >= 50),
    ('cash', 'Geschäft läuft', lambda row, rounds: row['cash'] >= 200_000),
    ('time', 'Stammspieler', lambda row, rounds: row['active_seconds'] >= 90 * 60),
    ('consistency', 'Konstanz', lambda row, rounds: rounds >= 3),
)


def berlin_day(stamp):
    return stamp.astimezone(BERLIN).date()


def split_berlin_seconds(start, end):
    """Split a short observed interval at local midnight, including DST days."""
    current = start
    while current < end:
        boundary = datetime.combine(berlin_day(current) + timedelta(days=1), time.min,
                                    tzinfo=BERLIN).astimezone(timezone.utc)
        cut = min(end, boundary)
        seconds = int((cut - current).total_seconds())
        if seconds:
            yield berlin_day(current), seconds
        current = cut


def faction_name(value):
    for team in config.QUEST_TEAMS:
        if str(value or '').strip().casefold() == team.casefold():
            return team
    return None


def valid_counters(player):
    values = (player.get('kills'), player.get('deaths'), player.get('cash'))
    return all(isinstance(value, int) and not isinstance(value, bool) and value >= 0
               for value in values)


def advance_round(row, player, faction, observed_at, *, first_sample=False, feed_streak=False):
    """Conservatively advance one persisted round row; never infer through gaps."""
    values = {key: int(player[key]) for key in ('kills', 'deaths', 'cash')}
    result = dict(row)
    previous_at = row.get('last_seen')
    if previous_at and previous_at.tzinfo is None:
        previous_at = previous_at.replace(tzinfo=timezone.utc)
    elapsed = int((observed_at - previous_at).total_seconds()) if previous_at else 0
    safe = (not first_sample and bool(row.get('active')) and row.get('last_faction') == faction
            and 0 < elapsed < MAX_SAMPLE_GAP_SECONDS
            and all(values[key] >= int(row[f'last_{key}']) for key in values))
    delta = {key: values[key] - int(row[f'last_{key}']) if safe else 0 for key in values}
    seconds = elapsed if safe else 0
    result.update({f'last_{key}': value for key, value in values.items()})
    result['last_seen'] = observed_at.replace(tzinfo=None)
    result['last_faction'] = faction
    result['active'] = 1
    if safe:
        for key in ('kills', 'deaths', 'cash'):
            result[key] = int(row[key]) + delta[key]
        result['active_seconds'] = int(row['active_seconds']) + seconds
        if feed_streak and delta['deaths']:
            # A missing/delayed death feed must not preserve a possible streak.
            # Never *add* RCON kill deltas to a feed-owned streak.
            result['streak'] = 0
        elif not feed_streak:
            result['streak'] = 0 if delta['deaths'] else int(row['streak']) + delta['kills']
            result['best_streak'] = max(int(row['best_streak']), result['streak'])
    else:
        result['streak'] = 0
    return result, delta, seconds, previous_at if safe else None


class ChallengeQuests(commands.Cog):
    health_key = 'Runden- und Tagesquests'

    def __init__(self, bot):
        self.bot = bot
        self.locks = defaultdict(asyncio.Lock)
        # The first sample after a process restart is a baseline, not downtime.
        self.started_servers = set()
        self.last_day = berlin_day(datetime.now(timezone.utc))

    async def _credit(self, cur, steam_id, amount, kind, reference, reason):
        await cur.execute('''INSERT IGNORE INTO quest_point_ledger
            (steam_id,amount,kind,reference_key,reason) VALUES (%s,%s,%s,%s,%s)''',
                          (steam_id, amount, kind, reference, reason))
        if not cur.rowcount:
            return False
        await cur.execute('''INSERT INTO quest_points(steam_id,points) VALUES (%s,%s)
            ON DUPLICATE KEY UPDATE points=points+VALUES(points),updated_at=UTC_TIMESTAMP()''',
                          (steam_id, amount))
        return True

    async def _daily_row(self, cur, day, steam_id):
        await cur.execute('''INSERT IGNORE INTO challenge_daily_progress(day,steam_id)
            VALUES (%s,%s)''', (day, steam_id))
        await cur.execute('''SELECT * FROM challenge_daily_progress
            WHERE day=%s AND steam_id=%s FOR UPDATE''', (day, steam_id))
        return dict(await cur.fetchone())

    async def _update_daily(self, cur, steam_id, server_id, round_id, day, *, kills=0,
                            cash=0, seconds=0):
        row = await self._daily_row(cur, day, steam_id)
        row['kills'] += kills
        row['cash'] += cash
        row['active_seconds'] += seconds
        if kills:
            await cur.execute('''INSERT INTO challenge_daily_round_kills
                (day,steam_id,server_id,round_id,kills) VALUES (%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE kills=kills+VALUES(kills)''',
                              (day, steam_id, server_id, round_id, kills))
        await cur.execute('''SELECT COUNT(*) AS count FROM challenge_daily_round_kills
            WHERE day=%s AND steam_id=%s AND kills>=5''', (day, steam_id))
        qualified_rounds = int((await cur.fetchone())['count'])
        completed, awarded = int(row['completed_mask']), int(row['awarded_mask'])
        for index, (key, title, reached) in enumerate(DAILY_QUESTS):
            bit = 1 << index
            if reached(row, qualified_rounds) and not completed & bit:
                completed |= bit
                if await self._credit(cur, steam_id, 2, 'challenge_daily', f'{day}:{key}',
                                      f'Tagesquest: {title}'):
                    awarded |= bit
        row['completed_mask'], row['awarded_mask'] = completed, awarded
        await cur.execute('''UPDATE challenge_daily_progress SET kills=%s,cash=%s,
            active_seconds=%s,completed_mask=%s,awarded_mask=%s
            WHERE day=%s AND steam_id=%s''',
                          (row['kills'], row['cash'], row['active_seconds'], completed, awarded,
                           day, steam_id))
        return row

    async def _process_player(self, cur, server_id, round_id, steam_id, player, faction,
                              observed_at, first_sample):
        await cur.execute('''SELECT * FROM challenge_round_progress
            WHERE server_id=%s AND round_id=%s AND steam_id=%s FOR UPDATE''',
                          (server_id, round_id, steam_id))
        previous = await cur.fetchone()
        if previous is None:
            await cur.execute('''INSERT INTO challenge_round_progress
                (server_id,round_id,steam_id,last_seen,last_kills,last_deaths,last_cash,
                 last_faction,active) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1)''',
                              (server_id, round_id, steam_id, observed_at.replace(tzinfo=None),
                               player['kills'], player['deaths'], player['cash'], faction))
            return

        row, delta, seconds, interval_start = advance_round(
            previous, player, faction, observed_at, first_sample=first_sample,
            feed_streak=config.COMBAT_FEED_ENABLED)
        day = berlin_day(observed_at)
        additions = defaultdict(lambda: {'kills': 0, 'cash': 0, 'seconds': 0})
        # Counters have no event timestamps. When a polling interval spans
        # midnight, assigning its kills/cash to either day could complete a
        # quest on the wrong date. Keep the new counter baseline, but do not
        # award those ambiguous increments to a daily quest.
        if not interval_start or berlin_day(interval_start) == day:
            additions[day]['kills'] += delta['kills']
            additions[day]['cash'] += delta['cash']
        if interval_start:
            for interval_day, amount in split_berlin_seconds(interval_start, observed_at):
                additions[interval_day]['seconds'] += amount
        daily = {}
        for interval_day in sorted(additions):
            daily[interval_day] = await self._update_daily(
                cur, steam_id, server_id, round_id, interval_day, **additions[interval_day])

        completed, awarded = int(row['completed_mask']), int(row['awarded_mask'])
        round_points = int(daily[day]['round_points_awarded'])
        for index, (key, title, reached) in enumerate(ROUND_QUESTS):
            bit = 1 << index
            if reached(row) and not completed & bit:
                completed |= bit
                if round_points < ROUND_POINT_LIMIT:
                    if await self._credit(cur, steam_id, 1, 'challenge_round',
                                          f'{server_id}:{round_id}:{key}',
                                          f'Rundenquest: {title}'):
                        round_points += 1
                        awarded |= bit
        row['completed_mask'], row['awarded_mask'] = completed, awarded
        await cur.execute('''UPDATE challenge_round_progress SET last_seen=%s,last_kills=%s,
            last_deaths=%s,last_cash=%s,last_faction=%s,kills=%s,deaths=%s,cash=%s,
            active_seconds=%s,streak=%s,best_streak=%s,active=%s,
            completed_mask=%s,awarded_mask=%s
            WHERE server_id=%s AND round_id=%s AND steam_id=%s''',
                          (row['last_seen'], row['last_kills'], row['last_deaths'], row['last_cash'],
                           row['last_faction'], row['kills'], row['deaths'], row['cash'],
                           row['active_seconds'], row['streak'], row['best_streak'], row['active'],
                           completed, awarded, server_id, round_id, steam_id))
        if round_points != daily[day]['round_points_awarded']:
            await cur.execute('''UPDATE challenge_daily_progress SET round_points_awarded=%s
                WHERE day=%s AND steam_id=%s''', (round_points, day, steam_id))

    async def process_sample(self, server_id, state, players):
        """Called after the normal statistics sample succeeds; no RCON of its own."""
        async with self.locks[server_id]:
            active = (state and not state.get('uncertain') and
                      round_is_active_for_quests(state))
            if not active:
                async with database.transaction() as cur:
                    await cur.execute('''UPDATE challenge_round_progress SET active=0,streak=0
                        WHERE server_id=%s AND active=1''', (server_id,))
                return
            observed_at = datetime.fromisoformat(state['observed_at'])
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=timezone.utc)
            round_id = str(state['round_id'])
            eligible = {}
            for player in players:
                steam_id = str(player.get('steamId', ''))
                faction = faction_name(player.get('faction'))
                if valid_steam_id(steam_id) and faction and valid_counters(player):
                    eligible[steam_id] = (player, faction)
            first_sample = server_id not in self.started_servers
            async with database.transaction() as cur:
                await cur.execute('''UPDATE challenge_round_progress SET active=0,streak=0
                    WHERE server_id=%s AND round_id<>%s AND active=1''', (server_id, round_id))
                if eligible:
                    placeholders = ','.join('%s' for _ in eligible)
                    await cur.execute(f'''UPDATE challenge_round_progress SET active=0,streak=0
                        WHERE server_id=%s AND round_id=%s AND active=1
                        AND steam_id NOT IN ({placeholders})''',
                                      (server_id, round_id, *eligible))
                else:
                    await cur.execute('''UPDATE challenge_round_progress SET active=0,streak=0
                        WHERE server_id=%s AND round_id=%s AND active=1''', (server_id, round_id))
                for steam_id, (player, faction) in sorted(eligible.items()):
                    await self._process_player(cur, server_id, round_id, steam_id, player,
                                               faction, observed_at, first_sample)
            self.started_servers.add(server_id)
            today = berlin_day(observed_at)
            if today != self.last_day:
                self.last_day = today
                self.bot.dispatch('bot_log', '🎯 Tagesquests neu gestartet',
                                  f'Die Tagesquests gelten seit dem {today} wieder von vorn.',
                                  discord.Color.blue())
            self.bot.health.ok(self.health_key)

    @commands.Cog.listener()
    async def on_round_ended(self, event):
        try:
            async with database.transaction() as cur:
                await cur.execute('''UPDATE challenge_round_progress SET active=0,streak=0
                    WHERE server_id=%s AND round_id=%s AND active=1''',
                                  (event['server_id'], event['round_id']))
        except Exception as exc:
            self.bot.health.error(self.health_key, exc)


async def setup(bot):
    await bot.add_cog(ChallengeQuests(bot))
