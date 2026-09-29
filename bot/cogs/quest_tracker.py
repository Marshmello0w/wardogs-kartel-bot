"""Persisted player quests without issuing additional RCON requests."""
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
import json
from uuid import uuid4
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from core import config
from core.permissions import valid_steam_id
from infrastructure import database, storage


BERLIN = ZoneInfo('Europe/Berlin')
UTC = timezone.utc


def as_utc(value):
    """Interpret database DATETIME values as UTC and return an aware timestamp."""
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def valid_team(value):
    candidate = str(value or '').strip()
    return next((team for team in config.QUEST_TEAMS
                 if candidate.casefold() == team.casefold()), None)


def round_is_active_for_quests(state):
    """Return whether a fresh round snapshot represents actual match play.

    A faction point is the definitive signal.  A populated server can also be
    considered active before the first point arrives, but exactly 20 players is
    deliberately not enough: the configured rule says *more than* 20.
    """
    if not isinstance(state, dict) or state.get('ended'):
        return False
    snapshot = state.get('snapshot')
    if not isinstance(snapshot, dict):
        return False
    highest = snapshot.get('highest')
    players = snapshot.get('players')
    count = players.get('current') if isinstance(players, dict) else None
    has_score = (not isinstance(highest, bool) and isinstance(highest, (int, float)) and highest >= 1)
    has_population = (not isinstance(count, bool) and isinstance(count, int) and count > 20)
    return has_score or has_population


def berlin_week_start(value=None):
    stamp = as_utc(value) if value else datetime.now(UTC)
    local = stamp.astimezone(BERLIN)
    return local.date() - timedelta(days=local.weekday())


def split_week_seconds(start, end):
    """Yield Berlin week dates and integral seconds for an observed UTC interval."""
    current, finish = as_utc(start), as_utc(end)
    if current is None or finish is None or finish <= current:
        return
    while current < finish:
        local = current.astimezone(BERLIN)
        next_monday = local.date() + timedelta(days=7 - local.weekday())
        boundary = datetime.combine(next_monday, time.min, tzinfo=BERLIN).astimezone(UTC)
        cut = min(finish, boundary)
        seconds = int((cut - current).total_seconds())
        if seconds > 0:
            yield berlin_week_start(current), seconds
        current = cut


class QuestTracker(commands.Cog):
    """Awards durable points from the statistics that the normal tracker records."""

    health_key = 'Quest-Erfassung'

    def __init__(self, bot):
        self.bot = bot
        # Fresh observations are intentionally process-local. A restart starts a
        # new interval rather than guessing time while the bot was offline.
        self.observations = {}
        self.track_team_time.start()
        self.sync_permanent_rewards.start()

    def cog_unload(self):
        self.track_team_time.cancel()
        self.sync_permanent_rewards.cancel()

    async def _credit(self, cur, steam_id, amount, kind, reference_key, *, reason=None,
                      admin_user_id=None, admin_mention=None):
        """Append one idempotent ledger entry and atomically update the balance."""
        if not amount:
            return False
        await cur.execute("""INSERT INTO quest_point_ledger
            (steam_id,amount,kind,reference_key,reason,admin_user_id,admin_mention)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE id=id""",
            (steam_id, amount, kind, reference_key, reason, admin_user_id, admin_mention))
        if not cur.rowcount:
            return False
        await cur.execute("""INSERT INTO quest_points(steam_id,points) VALUES (%s,%s)
            ON DUPLICATE KEY UPDATE points=points+VALUES(points),updated_at=UTC_TIMESTAMP()""",
            (steam_id, amount))
        return True

    async def _record_week(self):
        week = berlin_week_start().isoformat()
        previous = None
        async with database.transaction() as cur:
            await cur.execute("SELECT payload FROM durable_state WHERE namespace='quests' AND item_key='week' FOR UPDATE")
            row = await cur.fetchone()
            if row:
                try:
                    previous = json.loads(row['payload'])
                except (TypeError, json.JSONDecodeError):
                    previous = None
            await storage.put_state(cur, 'quests', 'week', week)
        if previous and previous != week:
            self.bot.dispatch('bot_log', '🎯 Wochenquests zurückgesetzt',
                              f'Die Team-Quests laufen seit dem {week} wieder von vorn.', discord.Color.blue())

    async def _fresh_faction_states(self):
        cutoff = storage.utcnow() - timedelta(seconds=config.QUEST_MAX_OBSERVATION_GAP_SECONDS)
        async with database.transaction() as cur:
            await cur.execute("""SELECT server_id,steam_id,faction,last_seen FROM player_faction_state
                WHERE last_seen >= %s""", (cutoff,))
            return await cur.fetchall()

    def active_round_servers(self):
        """Use RoundTracker's fresh status only; this does not add RCON polls."""
        tracker = self.bot.get_cog('RoundTracker')
        if tracker is None:
            return set()
        return {
            srv.id for srv in config.servers() if srv.enabled
            and round_is_active_for_quests(tracker.current(srv.id))
        }

    async def apply_team_seconds(self, additions):
        """Persist valid team time and weekly tier rewards in one transaction.

        ``additions`` is keyed by (Berlin week date, Steam64 ID, team name).
        """
        if not additions:
            return
        eligible_by_player = defaultdict(int)
        async with database.transaction() as cur:
            for (week, steam_id, team), seconds in additions.items():
                if seconds <= 0 or not valid_team(team) or not valid_steam_id(steam_id):
                    continue
                eligible_by_player[steam_id] += seconds
                await cur.execute("""INSERT INTO quest_team_playtime
                    (week_start,steam_id,team,playtime_seconds) VALUES (%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE playtime_seconds=playtime_seconds+VALUES(playtime_seconds),
                    updated_at=UTC_TIMESTAMP()""", (week, steam_id, team, seconds))

            for steam_id, seconds in eligible_by_player.items():
                await cur.execute("""INSERT INTO quest_progress(steam_id) VALUES (%s)
                    ON DUPLICATE KEY UPDATE eligible_playtime_seconds=eligible_playtime_seconds+%s,
                    updated_at=UTC_TIMESTAMP()""", (steam_id, seconds))

            for week, steam_id, team in additions:
                if not valid_team(team) or not valid_steam_id(steam_id):
                    continue
                await cur.execute("""SELECT playtime_seconds FROM quest_team_playtime
                    WHERE week_start=%s AND steam_id=%s AND team=%s FOR UPDATE""", (week, steam_id, team))
                row = await cur.fetchone()
                total = int(row['playtime_seconds']) if row else 0
                for threshold, points in config.QUEST_TEAM_MILESTONES:
                    if total >= threshold:
                        await self._credit(cur, steam_id, points, 'weekly_team',
                                           f'{week.isoformat()}:{team.casefold()}:{threshold}',
                                           reason=f'{team}: {threshold // 3600} Stunden in der Wochenquest')

    async def sample_team_time(self):
        await self._record_week()
        rows = await self._fresh_faction_states()
        active_servers = self.active_round_servers()
        fresh_keys, additions = set(), defaultdict(int)
        for row in rows:
            server_id, steam_id = str(row['server_id']), str(row['steam_id'])
            observed_at = as_utc(row['last_seen'])
            if server_id not in active_servers or not valid_steam_id(steam_id) or observed_at is None:
                continue
            key = (server_id, steam_id)
            fresh_keys.add(key)
            team = valid_team(row['faction'])
            previous = self.observations.get(key)
            if previous and team and previous['team'] == team:
                elapsed = (observed_at - previous['observed_at']).total_seconds()
                if 0 < elapsed <= config.QUEST_MAX_OBSERVATION_GAP_SECONDS:
                    for week, seconds in split_week_seconds(previous['observed_at'], observed_at):
                        additions[(week, steam_id, team)] += seconds
            self.observations[key] = {'team': team, 'observed_at': observed_at}

        # A player absent from a fresh *active-round* state starts a new interval.
        # This prevents a later active snapshot from crediting lobby or post-round time.
        self.observations = {key: value for key, value in self.observations.items() if key in fresh_keys}
        await self.apply_team_seconds(additions)

    async def sync_permanent(self):
        """Pay automatic rewards only after a fresh real-faction observation.

        Lifetime cash remains accumulated while unassigned. Its pending blocks
        become payable once a real faction is observed; historical awards stay
        untouched. Future playtime is already accrued only in valid teams.
        """
        cutoff = storage.utcnow() - timedelta(seconds=config.QUEST_MAX_OBSERVATION_GAP_SECONDS)
        async with database.transaction() as cur:
            await cur.execute("""SELECT players.steam_id,
                COALESCE(playtime.playtime_seconds, 0) AS legacy_playtime_seconds,
                COALESCE(cash.lifetime_cash, 0) AS lifetime_cash
                FROM (SELECT steam_id FROM player_playtime UNION SELECT steam_id FROM leaderboard) players
                LEFT JOIN (SELECT steam_id,SUM(playtime_seconds) AS playtime_seconds
                    FROM player_playtime GROUP BY steam_id) playtime ON playtime.steam_id=players.steam_id
                LEFT JOIN (SELECT steam_id,SUM(lifetime_cash) AS lifetime_cash
                    FROM leaderboard GROUP BY steam_id) cash ON cash.steam_id=players.steam_id""")
            players = await cur.fetchall()
            await cur.execute("""SELECT DISTINCT steam_id FROM player_faction_state
                WHERE last_seen >= %s AND LOWER(TRIM(faction)) IN (%s,%s,%s)""",
                              (cutoff, *(team.casefold() for team in config.QUEST_TEAMS)))
            eligible_ids = {str(row['steam_id']) for row in await cur.fetchall()}
            for player in players:
                steam_id = str(player['steam_id'])
                if not valid_steam_id(steam_id) or steam_id not in eligible_ids:
                    continue
                await cur.execute("""INSERT INTO quest_progress(steam_id) VALUES (%s)
                    ON DUPLICATE KEY UPDATE steam_id=steam_id""", (steam_id,))
                await cur.execute("SELECT * FROM quest_progress WHERE steam_id=%s FOR UPDATE", (steam_id,))
                progress = await cur.fetchone()

                if not progress['legacy_playtime_imported']:
                    legacy_hours = max(0, int(player['legacy_playtime_seconds'])) // config.QUEST_PLAYTIME_SECONDS_PER_POINT
                    await self._credit(cur, steam_id, legacy_hours, 'legacy_playtime', 'initial-import',
                                       reason='Einmalige Übernahme der vorhandenen Spielzeit')
                    await cur.execute("UPDATE quest_progress SET legacy_playtime_imported=1 WHERE steam_id=%s", (steam_id,))

                cash_blocks = max(0, int(player['lifetime_cash'])) // config.QUEST_CASH_PER_POINT
                missing_cash = max(0, cash_blocks - int(progress['awarded_cash_blocks']))
                if missing_cash:
                    await self._credit(cur, steam_id, missing_cash, 'cash', f'cash-through-{cash_blocks}',
                                       reason=f'Cash-Fortschritt bis {cash_blocks * config.QUEST_CASH_PER_POINT} USD')
                    await cur.execute("UPDATE quest_progress SET awarded_cash_blocks=%s WHERE steam_id=%s",
                                      (cash_blocks, steam_id))

                eligible_hours = int(progress['eligible_playtime_seconds']) // config.QUEST_PLAYTIME_SECONDS_PER_POINT
                missing_hours = max(0, eligible_hours - int(progress['awarded_eligible_hours']))
                if missing_hours:
                    await self._credit(cur, steam_id, missing_hours, 'eligible_playtime',
                                       f'eligible-hours-through-{eligible_hours}',
                                       reason=f'Quest-Spielzeit bis {eligible_hours} Stunden')
                    await cur.execute("UPDATE quest_progress SET awarded_eligible_hours=%s WHERE steam_id=%s",
                                      (eligible_hours, steam_id))

    async def set_points(self, steam_id, target, *, admin_user_id, admin_mention, reason):
        """Set an admin-approved total, retaining the signed adjustment in the ledger."""
        if not valid_steam_id(str(steam_id)):
            raise ValueError('Ungültige Steam64-ID')
        if isinstance(target, bool) or not isinstance(target, int) or not 0 <= target <= 1_000_000_000:
            raise ValueError('Der Punktestand muss zwischen 0 und 1.000.000.000 liegen')
        reason = str(reason or '').strip()[:255]
        if not reason:
            raise ValueError('Bitte einen Grund angeben')

        async with database.transaction() as cur:
            await cur.execute("""INSERT INTO quest_points(steam_id) VALUES (%s)
                ON DUPLICATE KEY UPDATE steam_id=steam_id""", (steam_id,))
            await cur.execute("SELECT points FROM quest_points WHERE steam_id=%s FOR UPDATE", (steam_id,))
            row = await cur.fetchone()
            previous = int(row['points'])
            delta = target - previous
            if not delta:
                return previous, 0
            await self._credit(cur, steam_id, delta, 'admin_set', uuid4().hex, reason=reason,
                               admin_user_id=str(admin_user_id), admin_mention=str(admin_mention)[:100])
            await cur.execute("UPDATE quest_points SET points=%s,updated_at=UTC_TIMESTAMP() WHERE steam_id=%s",
                              (target, steam_id))
        self.bot.dispatch('bot_log', '🎯 Quest-Punkte geändert',
                          f'{admin_mention} setzte `{steam_id}` von **{previous}** auf **{target}** Punkte '
                          f'(Differenz: {delta:+d}). Grund: {reason}', discord.Color.blue())
        return previous, delta

    @tasks.loop(seconds=60)
    async def track_team_time(self):
        try:
            await self.sample_team_time()
            self.bot.health.ok(self.health_key)
        except Exception as exc:
            self.bot.health.error(self.health_key, exc)

    @track_team_time.before_loop
    async def before_track_team_time(self):
        await self.bot.wait_until_ready()
        # The regular stats tracker runs first after ready and supplies the state.
        await discord.utils.sleep_until(discord.utils.utcnow() + timedelta(seconds=config.QUEST_SAMPLE_DELAY_SECONDS))

    @tasks.loop(seconds=config.QUEST_PERMANENT_SYNC_SECONDS)
    async def sync_permanent_rewards(self):
        try:
            await self.sync_permanent()
            self.bot.health.ok(self.health_key)
        except Exception as exc:
            self.bot.health.error(self.health_key, exc)

    @sync_permanent_rewards.before_loop
    async def before_sync_permanent_rewards(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(QuestTracker(bot))
