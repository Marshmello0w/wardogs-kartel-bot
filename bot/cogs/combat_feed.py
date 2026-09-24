"""Process authenticated, durable kill-feed batches without extra RCON calls."""
from datetime import timedelta, timezone
import json
import logging
import time

import discord
from discord.ext import commands, tasks

from core import config
from domain.combat_feed import normalized_kill, relation, roster_for_event
from infrastructure import database
from cogs.challenge_quests import berlin_day

logger = logging.getLogger(__name__)
ROUND_GOALS = (('headshots', 3, 'Headshots'), ('long_kills', 1, 'Distanzkill'))
DAILY_GOALS = (('headshots', 10, 'Headshots'), ('long_kills', 3, 'Distanzkills'))


async def credit(cur, steam_id, amount, kind, reference, reason):
    await cur.execute('''INSERT IGNORE INTO quest_point_ledger
        (steam_id,amount,kind,reference_key,reason) VALUES (%s,%s,%s,%s,%s)''',
                      (steam_id, amount, kind, reference, reason))
    if not cur.rowcount:
        return False
    await cur.execute('''INSERT INTO quest_points(steam_id,points) VALUES (%s,%s)
        ON DUPLICATE KEY UPDATE points=points+VALUES(points),updated_at=UTC_TIMESTAMP()''',
                      (steam_id, amount))
    return True


async def award_round(cur, day, server_id, round_id, steam_id, key, title):
    """Share the same locked 15-point bucket as sample-based round quests."""
    await cur.execute('''INSERT IGNORE INTO challenge_daily_progress(day,steam_id)
        VALUES (%s,%s)''', (day, steam_id))
    await cur.execute('''SELECT round_points_awarded FROM challenge_daily_progress
        WHERE day=%s AND steam_id=%s FOR UPDATE''', (day, steam_id))
    row = await cur.fetchone()
    if int(row['round_points_awarded']) >= 15:
        return False
    if not await credit(cur, steam_id, 1, 'challenge_round',
                        f'{server_id}:{round_id}:{key}', f'Rundenquest: {title}'):
        return False
    await cur.execute('''UPDATE challenge_daily_progress
        SET round_points_awarded=round_points_awarded+1 WHERE day=%s AND steam_id=%s''',
                      (day, steam_id))
    return True


async def record_combat_quests(cur, server_id, round_id, event):
    steam_id = event['killer']
    day = berlin_day(event['received_at'])
    headshot = int(event['headshot'])
    long_kill = int(event['distance_m'] is not None and event['distance_m'] >= 150)
    await cur.execute('''INSERT INTO combat_round_quests
        (server_id,round_id,steam_id,headshots,long_kills)
        VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE
        headshots=headshots+VALUES(headshots),long_kills=long_kills+VALUES(long_kills)''',
                      (server_id, round_id, steam_id, headshot, long_kill))
    await cur.execute('''SELECT * FROM combat_round_quests WHERE
        server_id=%s AND round_id=%s AND steam_id=%s FOR UPDATE''',
                      (server_id, round_id, steam_id))
    row = dict(await cur.fetchone())
    completed, awarded = int(row['completed_mask']), int(row['awarded_mask'])
    for index, (field, target, title) in enumerate(ROUND_GOALS):
        bit = 1 << index
        if row[field] >= target and not completed & bit:
            completed |= bit
            if await award_round(cur, day, server_id, round_id, steam_id,
                                 f'feed_{field}', title):
                awarded |= bit
    await cur.execute('''UPDATE combat_round_quests SET completed_mask=%s,awarded_mask=%s
        WHERE server_id=%s AND round_id=%s AND steam_id=%s''',
                      (completed, awarded, server_id, round_id, steam_id))

    await cur.execute('''INSERT INTO combat_daily_quests(day,steam_id,headshots,long_kills)
        VALUES (%s,%s,%s,%s) ON DUPLICATE KEY UPDATE
        headshots=headshots+VALUES(headshots),long_kills=long_kills+VALUES(long_kills)''',
                      (day, steam_id, headshot, long_kill))
    await cur.execute('''SELECT * FROM combat_daily_quests WHERE day=%s AND steam_id=%s
        FOR UPDATE''', (day, steam_id))
    row = dict(await cur.fetchone())
    completed, awarded = int(row['completed_mask']), int(row['awarded_mask'])
    for index, (field, target, title) in enumerate(DAILY_GOALS):
        bit = 1 << index
        if row[field] >= target and not completed & bit:
            completed |= bit
            if await credit(cur, steam_id, 2, 'challenge_daily',
                            f'{day}:feed_{field}', f'Tagesquest: {title}'):
                awarded |= bit
    await cur.execute('''UPDATE combat_daily_quests SET completed_mask=%s,awarded_mask=%s
        WHERE day=%s AND steam_id=%s''', (completed, awarded, day, steam_id))


async def record_feed_streak(cur, server_id, round_id, event):
    """Ordered feed events, not RCON counter deltas, drive the streak quest."""
    victim = event['victim']
    killer = event['killer']
    # Match ChallengeQuests' sorted player lock order before touching either
    # side. A kill can otherwise deadlock its sample transaction at a busy time.
    ids = sorted({candidate for candidate in (victim, killer) if candidate})
    if not ids:
        return
    placeholders = ','.join('%s' for _ in ids)
    await cur.execute(f'''SELECT steam_id,streak,best_streak,completed_mask,awarded_mask
        FROM challenge_round_progress WHERE server_id=%s AND round_id=%s
        AND steam_id IN ({placeholders}) ORDER BY steam_id FOR UPDATE''',
                      (server_id, round_id, *ids))
    rows = {row['steam_id']: row for row in await cur.fetchall()}
    if victim in rows:
        await cur.execute('''UPDATE challenge_round_progress SET streak=0 WHERE
            server_id=%s AND round_id=%s AND steam_id=%s''', (server_id, round_id, victim))
    if not killer or event['suicide'] or killer not in rows:
        return
    row = rows[killer]
    streak = int(row['streak']) + 1
    best = max(int(row['best_streak']), streak)
    completed, awarded = int(row['completed_mask']), int(row['awarded_mask'])
    if streak >= 5 and not completed & 2:
        completed |= 2
        if await award_round(cur, berlin_day(event['received_at']), server_id,
                             round_id, killer, 'streak', 'Killserie'):
            awarded |= 2
    await cur.execute('''UPDATE challenge_round_progress SET streak=%s,best_streak=%s,
        completed_mask=%s,awarded_mask=%s WHERE server_id=%s AND round_id=%s
        AND steam_id=%s''',
                      (streak, best, completed, awarded, server_id, round_id, killer))


class CombatFeed(commands.Cog):
    health_key = 'Kampf-Feed'

    def __init__(self, bot):
        self.bot = bot
        self.rosters = {}
        self.last_backlog_log = 0
        self.last_cleanup = 0
        if config.COMBAT_FEED_ENABLED:
            self.poll.start()

    def cog_unload(self):
        if self.poll.is_running():
            self.poll.cancel()

    @commands.Cog.listener()
    async def on_player_sampled(self, server_id, state, players):
        if config.COMBAT_FEED_ENABLED:
            self.rosters[server_id] = (state, players)

    async def _event(self, cur, server_id, instance_id, event, received_at, batch_index=0):
        item = normalized_kill(event, instance_id, received_at)
        if item is None:
            return None
        roster = roster_for_event(self.rosters.get(server_id), received_at, item['map_name'])
        round_id = roster[0] if roster else None
        await cur.execute('''INSERT IGNORE INTO combat_events
            (server_id,event_id,instance_id,round_id,occurred_at,batch_index,event_time,
             killer_steam_id,victim_steam_id,killer_name,victim_name,map_name,cause,
             distance_m,headshot,suicide)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                          (server_id, item['event_id'], item['instance_id'], round_id,
                           received_at.replace(tzinfo=None), batch_index, item['event_time'],
                           item['killer'], item['victim'], item['killer_name'],
                           item['victim_name'], item['map_name'], item['cause'],
                           item['distance_m'], int(item['headshot']), int(item['suicide'])))
        if not cur.rowcount:
            return None
        is_player_kill = bool(item['killer'] and item['victim'] and not item['suicide'])
        if is_player_kill:
            await cur.execute('''INSERT INTO combat_player_stats
                (server_id,steam_id,kills,headshots,longest_kill_m) VALUES (%s,%s,1,%s,%s)
                ON DUPLICATE KEY UPDATE kills=kills+1,headshots=headshots+VALUES(headshots),
                longest_kill_m=GREATEST(longest_kill_m,VALUES(longest_kill_m))''',
                              (server_id, item['killer'], int(item['headshot']), item['distance_m'] or 0))
            if item['cause']:
                await cur.execute('''INSERT INTO combat_weapon_stats
                    (server_id,steam_id,weapon,kills) VALUES (%s,%s,%s,1)
                    ON DUPLICATE KEY UPDATE kills=kills+1''',
                                  (server_id, item['killer'], item['cause']))
        if item['victim']:
            await cur.execute('''INSERT INTO combat_player_stats(server_id,steam_id,deaths)
                VALUES (%s,%s,1) ON DUPLICATE KEY UPDATE deaths=deaths+1''',
                              (server_id, item['victim']))
        relation_value = relation(item, roster)
        if round_id and item['victim']:
            if relation_value == 'enemy':
                await record_feed_streak(cur, server_id, round_id, item)
                await record_combat_quests(cur, server_id, round_id, item)
            else:
                # Any observed death ends the victim's series, even a teamkill.
                await cur.execute('''UPDATE challenge_round_progress SET streak=0 WHERE
                    server_id=%s AND round_id=%s AND steam_id=%s''',
                                  (server_id, round_id, item['victim']))
        if relation_value == 'teamkill':
            await cur.execute('''INSERT INTO combat_alert_state
                (server_id,steam_id,teamkill_count,teamkill_first_at,teamkill_last_at)
                VALUES (%s,%s,1,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))
                ON DUPLICATE KEY UPDATE
                teamkill_first_at=IF(teamkill_count=0,UTC_TIMESTAMP(6),teamkill_first_at),
                teamkill_last_at=UTC_TIMESTAMP(6),teamkill_count=teamkill_count+1''',
                              (server_id, item['killer']))
        if is_player_kill:
            await cur.execute('''INSERT IGNORE INTO combat_alert_state(server_id,steam_id)
                VALUES (%s,%s)''', (server_id, item['killer']))
            await cur.execute('''SELECT rapid_last_at FROM combat_alert_state
                WHERE server_id=%s AND steam_id=%s FOR UPDATE''', (server_id, item['killer']))
            rapid = await cur.fetchone()
            last = rapid['rapid_last_at']
            if last is None or received_at.replace(tzinfo=None) - last >= timedelta(minutes=5):
                await cur.execute('''SELECT COUNT(*) AS count FROM combat_events
                    WHERE server_id=%s AND killer_steam_id=%s
                    AND occurred_at>%s AND occurred_at<=%s
                    AND victim_steam_id IS NOT NULL AND suicide=0''',
                                  (server_id, item['killer'],
                                   (received_at - timedelta(seconds=60)).replace(tzinfo=None),
                                   received_at.replace(tzinfo=None)))
                count = int((await cur.fetchone())['count'])
                if count >= 10:
                    await cur.execute('''UPDATE combat_alert_state SET rapid_last_at=%s
                        WHERE server_id=%s AND steam_id=%s''',
                                      (received_at.replace(tzinfo=None), server_id, item['killer']))
                    return ('rapid', server_id, item['killer'], count)
        return None

    async def process_one(self):
        alerts = []
        async with database.transaction() as cur:
            await cur.execute('''SELECT id,server_id,payload,received_at FROM combat_feed_batches
                WHERE processed_at IS NULL ORDER BY received_at LIMIT 1 FOR UPDATE''')
            batch = await cur.fetchone()
            if not batch:
                return False
            try:
                data = json.loads(batch['payload'])
            except (ValueError, TypeError):
                raise RuntimeError('Invalid persisted feed batch')
            received_at = batch['received_at'].replace(tzinfo=timezone.utc)
            instance_id = data.get('serverId')
            events = data.get('events', [])
            # The feed sends batches in event order. Do not sort by eventTime:
            # that match clock resets on map changes within one game instance.
            for index, event in enumerate(events):
                alert = await self._event(cur, batch['server_id'], instance_id, event,
                                          received_at, index)
                if alert:
                    alerts.append(alert)
            await cur.execute('''UPDATE combat_feed_batches SET processed_at=UTC_TIMESTAMP(6)
                WHERE id=%s''', (batch['id'],))
        for _, server_id, steam_id, count in alerts:
            self.bot.dispatch('bot_log', 'Schnelle Kills – Hinweis',
                              f'{server_id}: {steam_id} erzielte mindestens {count} Spielerkills '
                              'innerhalb von 60 Sekunden. Bitte prüfen; keine automatische Strafe.',
                              discord.Color.orange(), config.COMBAT_ALERT_CHANNEL_ID)
        return True

    async def flush_teamkills(self):
        alerts = []
        async with database.transaction() as cur:
            await cur.execute('''SELECT server_id,steam_id,teamkill_count FROM combat_alert_state
                WHERE teamkill_count>0 AND teamkill_first_at<=DATE_SUB(UTC_TIMESTAMP(6),INTERVAL 30 SECOND)
                LIMIT 30 FOR UPDATE''')
            alerts = list(await cur.fetchall())
            for row in alerts:
                await cur.execute('''UPDATE combat_alert_state SET teamkill_count=0,
                    teamkill_first_at=NULL,teamkill_last_at=NULL
                    WHERE server_id=%s AND steam_id=%s''',
                                  (row['server_id'], row['steam_id']))
        for row in alerts:
            self.bot.dispatch('bot_log', 'Möglicher Teamkill – prüfen',
                              f"{row['server_id']}: {row['steam_id']} – {row['teamkill_count']} "
                              'Ereignis(se) in 30 Sekunden. Einstufung nur anhand zeitnaher '
                              'RCON-Fraktionsdaten; keine automatische Strafe.',
                              discord.Color.orange(), config.COMBAT_ALERT_CHANNEL_ID)

    async def maintenance(self):
        now = time.monotonic()
        if now - self.last_cleanup > 3600:
            async with database.transaction() as cur:
                await cur.execute('''DELETE FROM combat_events
                    WHERE occurred_at<DATE_SUB(UTC_TIMESTAMP(),INTERVAL 90 DAY) LIMIT 1000''')
                await cur.execute('''DELETE FROM combat_feed_batches
                    WHERE processed_at IS NOT NULL AND forwarded_at IS NOT NULL
                    AND received_at<DATE_SUB(UTC_TIMESTAMP(),INTERVAL 7 DAY) LIMIT 1000''')
            self.last_cleanup = now
        if now - self.last_backlog_log > 300:
            async with database.transaction() as cur:
                await cur.execute('''SELECT COUNT(*) AS total FROM combat_feed_batches
                    WHERE forwarded_at IS NULL
                    AND received_at<DATE_SUB(UTC_TIMESTAMP(),INTERVAL 5 MINUTE)''')
                backlog = int((await cur.fetchone())['total'])
            if backlog:
                self.bot.dispatch('bot_log', 'Kampf-Feed: Weiterleitung verzögert',
                                  f'{backlog} Batch(es) warten seit über fünf Minuten auf den Anbieter.',
                                  discord.Color.red())
            self.last_backlog_log = now

    @tasks.loop(seconds=2)
    async def poll(self):
        try:
            for _ in range(10):
                if not await self.process_one():
                    break
            await self.flush_teamkills()
            await self.maintenance()
            self.bot.health.ok(self.health_key)
        except Exception as exc:
            logger.error('Combat feed unavailable: %s', type(exc).__name__)
            self.bot.health.error(self.health_key, exc)

    @poll.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(CombatFeed(bot))
