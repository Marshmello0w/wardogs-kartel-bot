"""Private player notices and once-per-round public score reminders.

All input comes from existing player samples, the quest ledger and RoundTracker.
This cog does not poll RCON for additional data or post directly to Discord.
"""
import asyncio
from collections import defaultdict
import json
import logging
import time
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

from core import config
from core.permissions import valid_steam_id
from infrastructure import database, storage
from services.rcon import RconError


logger = logging.getLogger(__name__)
THRESHOLDS = (25, 50, 75)
REMINDER = 'Auffälligkeiten oder Anregungen? Komm auf unseren Discord: dsc.gg/dkwd'
ROSTER_MAX_AGE = 30
QUEST_TARGET_WAIT = 60
MAX_QUEUE = 100


def real_faction(value):
    candidate = str(value or '').strip()
    return next((team for team in config.QUEST_TEAMS
                 if candidate.casefold() == team.casefold()), None)


def reached_thresholds(snapshot):
    scores = snapshot.get('factionScores', []) if isinstance(snapshot, dict) else []
    highest = max((item['score'] for item in scores
                   if isinstance(item, dict) and real_faction(item.get('name'))
                   and isinstance(item.get('score'), (int, float))
                   and not isinstance(item['score'], bool)), default=0)
    return tuple(threshold for threshold in THRESHOLDS if highest >= threshold)


def quest_notice(reason, amount):
    name = str(reason or '').partition(':')[2].strip() or 'Quest'
    name = ''.join(char for char in name if char.isprintable())[:70]
    return f'Quest abgeschlossen: {name} (+{amount} {"Punkt" if amount == 1 else "Punkte"}).'


def join_notice(server_id, row):
    kills = int(row.get('lifetime_kills') or 0)
    deaths = int(row.get('lifetime_deaths') or 0)
    cash = int(row.get('lifetime_cash') or 0)
    seconds = int(row.get('playtime_seconds') or 0)
    hours, minutes = divmod(seconds // 60, 60)
    ratio = kills / deaths if deaths else float(kills)
    return (f'Deine Stats auf {config.server(server_id).title}: '
            f'{kills} Kills, {deaths} Tode, K/D {ratio:.2f}, '
            f'{cash} USD Cash, {hours} Std. {minutes:02d} Min. Spielzeit.')


class IngameNotifications(commands.Cog):
    health_key = 'Ingame-Benachrichtigungen'

    def __init__(self, bot):
        self.bot = bot
        self.rosters = {}
        self.join_visits = {}
        self.join_sequence = 0
        self.queue = {}
        self.workers = {}
        self.sequence = 0
        self.queued_quests = set()
        self.queued_milestones = set()
        self.initialized_scores = set()
        self.ledger_floor = None
        self.delivered_counts = defaultdict(lambda: {'quest': 0, 'join': 0})
        self.last_log_flush = time.monotonic()
        self.monitor.start()

    def cog_unload(self):
        self.monitor.cancel()
        for worker in self.workers.values():
            worker.cancel()

    def _enqueue(self, server_id, priority, kind, value):
        queue = self.queue.setdefault(server_id, asyncio.PriorityQueue(maxsize=MAX_QUEUE))
        if queue.full():
            self.bot.health.error(self.health_key, RuntimeError('message queue full'))
            return False
        self.sequence += 1
        queue.put_nowait((priority, self.sequence, kind, value))
        if server_id not in self.workers or self.workers[server_id].done():
            self.workers[server_id] = asyncio.create_task(self._worker(server_id))
        return True

    @commands.Cog.listener()
    async def on_player_sampled(self, server_id, state, players):
        if not isinstance(state, dict) or state.get('uncertain'):
            self.rosters.pop(server_id, None)
            self.join_visits.pop(server_id, None)
            return
        now = time.monotonic()
        current = {str(player.get('steamId')): player for player in players
                   if isinstance(player, dict) and valid_steam_id(str(player.get('steamId', '')))}
        previous = self.rosters.get(server_id)
        self.rosters[server_id] = (now, current)
        # A restart or a long sampling gap cannot distinguish old occupants
        # from new arrivals. Treat the current roster as a baseline.
        if previous is None or now - previous[0] > 90:
            self.join_visits[server_id] = {}
            return
        visits = self.join_visits.setdefault(server_id, {})
        for steam_id in visits.keys() - current.keys():
            del visits[steam_id]
        for steam_id in current.keys() - previous[1].keys():
            self.join_sequence += 1
            visits[steam_id] = {'token': self.join_sequence, 'queued': False, 'attempted': False}
        # Keep an arrival pending while the player is in the selection menu.
        # The roster ID does not change when they choose their faction.
        for steam_id, visit in visits.items():
            if (not visit['queued'] and not visit['attempted']
                    and real_faction(current[steam_id].get('faction'))):
                visit['queued'] = self._enqueue(server_id, 2, 'join', (steam_id, visit['token']))

    def _fresh_player_server(self, steam_id):
        now = time.monotonic()
        candidates = [(observed, server_id, roster[steam_id])
                      for server_id, (observed, roster) in self.rosters.items()
                      if now - observed <= ROSTER_MAX_AGE and steam_id in roster]
        if not candidates:
            return None
        observed, server_id, player = max(candidates, key=lambda item: item[0])
        return server_id, player

    async def _initialize_ledger(self):
        async with database.transaction() as cur:
            await cur.execute("SELECT payload FROM durable_state WHERE namespace='ingame' AND item_key='ledger_floor' FOR UPDATE")
            row = await cur.fetchone()
            if row is None:
                await cur.execute('SELECT COALESCE(MAX(id),0) AS max_id FROM quest_point_ledger')
                floor = int((await cur.fetchone())['max_id'])
                await storage.put_state(cur, 'ingame', 'ledger_floor', floor)
            else:
                floor = int(json.loads(row['payload']))
        self.ledger_floor = floor

    async def _record_delivery(self, ledger_id, status, server_id=None):
        async with database.transaction() as cur:
            await cur.execute('''INSERT INTO ingame_quest_delivery
                (ledger_id,server_id,status,attempted_at) VALUES (%s,%s,%s,UTC_TIMESTAMP())
                ON DUPLICATE KEY UPDATE status=VALUES(status)''',
                              (ledger_id, server_id, status))

    async def _poll_quests(self):
        if self.ledger_floor is None:
            await self._initialize_ledger()
        async with database.transaction() as cur:
            await cur.execute('''SELECT l.id,l.steam_id,l.amount,l.reason,l.created_at
                FROM quest_point_ledger l LEFT JOIN ingame_quest_delivery d ON d.ledger_id=l.id
                WHERE l.id>%s AND l.kind IN ('challenge_round','challenge_daily')
                AND l.amount>0 AND d.ledger_id IS NULL ORDER BY l.id LIMIT 100''',
                              (self.ledger_floor,))
            rows = await cur.fetchall()
        for row in rows:
            ledger_id = int(row['id'])
            if ledger_id in self.queued_quests:
                continue
            steam_id = str(row['steam_id'])
            target = self._fresh_player_server(steam_id)
            if target and real_faction(target[1].get('faction')):
                if self._enqueue(target[0], 0, 'quest', dict(row)):
                    self.queued_quests.add(ledger_id)
            else:
                created = row['created_at']
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - created).total_seconds() >= QUEST_TARGET_WAIT:
                    await self._record_delivery(ledger_id, 'skipped')

    async def _poll_scores(self):
        tracker = self.bot.get_cog('RoundTracker')
        if tracker is None:
            return
        for srv in config.servers():
            if not srv.enabled:
                continue
            state = tracker.current(srv.id)
            if not state or state.get('ended'):
                continue
            reached = reached_thresholds(state.get('snapshot'))
            round_id = str(state['round_id'])
            saved = await storage.get_state('ingame_score', srv.id)
            if srv.id not in self.initialized_scores:
                self.initialized_scores.add(srv.id)
                # On process startup, do not replay milestones already passed.
                attempted = set(saved.get('attempted', [])) if saved and saved.get('round_id') == round_id else set()
                attempted.update(reached)
                await storage.save_state('ingame_score', srv.id,
                                         {'round_id': round_id, 'attempted': sorted(attempted)})
                continue
            if not saved or saved.get('round_id') != round_id:
                await storage.save_state('ingame_score', srv.id,
                                         {'round_id': round_id, 'attempted': []})
                saved = {'round_id': round_id, 'attempted': []}
            attempted = set(saved.get('attempted', []))
            for threshold in reached:
                key = (srv.id, round_id, threshold)
                if threshold not in attempted and key not in self.queued_milestones:
                    if self._enqueue(srv.id, 1, 'milestone', key):
                        self.queued_milestones.add(key)

    async def _attempt_milestone(self, server_id, key):
        _, round_id, threshold = key
        tracker = self.bot.get_cog('RoundTracker')
        state = tracker.current(server_id) if tracker else None
        if not state or state.get('ended') or state['round_id'] != round_id:
            return
        async with database.transaction() as cur:
            await cur.execute("SELECT payload FROM durable_state WHERE namespace='ingame_score' AND item_key=%s FOR UPDATE",
                              (server_id,))
            row = await cur.fetchone()
            saved = json.loads(row['payload']) if row else None
            if not saved or saved.get('round_id') != round_id or threshold in saved.get('attempted', []):
                return
            saved['attempted'].append(threshold)
            await storage.put_state(cur, 'ingame_score', server_id, saved)
        try:
            await self.bot.rcon.request(server_id, 'POST', '/v1/broadcast', payload={'message': REMINDER})
        except Exception as exc:
            self.bot.health.error(self.health_key, exc)
            self.bot.dispatch('bot_log', 'Ingame-Rundenhinweis fehlgeschlagen',
                              f'{config.server(server_id).title}: Schwelle {threshold} nicht bestätigt '
                              f'({type(exc).__name__}). Keine automatische Wiederholung.', discord.Color.blue())
        else:
            self.bot.dispatch('bot_log', 'Ingame-Rundenhinweis gesendet',
                              f'{config.server(server_id).title}: {threshold} Fraktionspunkte erreicht. '
                              f'Nachricht: {REMINDER}', discord.Color.blue())

    async def _send_quest(self, server_id, row):
        ledger_id, steam_id = int(row['id']), str(row['steam_id'])
        target = self._fresh_player_server(steam_id)
        if not target or target[0] != server_id or not real_faction(target[1].get('faction')):
            await self._record_delivery(ledger_id, 'skipped')
            return
        # Record the attempt before the write: a lost HTTP response or process
        # crash must not produce a duplicate private message after restart.
        await self._record_delivery(ledger_id, 'attempted', server_id)
        try:
            await self.bot.rcon.request(server_id, 'POST', f'/v1/players/{steam_id}/message',
                                        payload={'message': quest_notice(row['reason'], int(row['amount']))})
        except Exception as exc:
            await self._record_delivery(ledger_id, 'uncertain', server_id)
            self.bot.health.error(self.health_key, exc)
            self.bot.dispatch('bot_log', 'Ingame-Questmeldung fehlgeschlagen',
                              f'{config.server(server_id).title}: Meldung für `{steam_id}` nicht bestätigt '
                              f'({type(exc).__name__}). Keine automatische Wiederholung.', discord.Color.blue())
        else:
            await self._record_delivery(ledger_id, 'sent', server_id)
            self.delivered_counts[server_id]['quest'] += 1

    async def _send_join(self, server_id, arrival):
        steam_id, token = arrival
        visit = self.join_visits.get(server_id, {}).get(steam_id)
        if not visit or visit['token'] != token or visit['attempted']:
            return
        target = self._fresh_player_server(steam_id)
        if not target or target[0] != server_id or not real_faction(target[1].get('faction')):
            visit['queued'] = False
            return
        try:
            async with database.transaction() as cur:
                # A new player may not have a leaderboard row yet. Still show
                # zero counters and any independently recorded playtime.
                await cur.execute('''SELECT l.lifetime_kills,l.lifetime_deaths,l.lifetime_cash,
                    p.playtime_seconds FROM (SELECT %s AS server_id,%s AS steam_id) identity_row
                    LEFT JOIN leaderboard l ON l.server_id=identity_row.server_id
                        AND l.steam_id=identity_row.steam_id
                    LEFT JOIN player_playtime p ON p.server_id=identity_row.server_id
                        AND p.steam_id=identity_row.steam_id''', (server_id, steam_id))
                row = await cur.fetchone()
        except Exception:
            visit['queued'] = False  # No RCON write happened; a later sample may retry.
            raise
        target = self._fresh_player_server(steam_id)
        if (self.join_visits.get(server_id, {}).get(steam_id) is not visit
                or not target or target[0] != server_id
                or not real_faction(target[1].get('faction'))):
            visit['queued'] = False
            return
        visit['attempted'] = True  # Never blindly retry an ambiguous write.
        try:
            await self.bot.rcon.request(server_id, 'POST', f'/v1/players/{steam_id}/message',
                                        payload={'message': join_notice(server_id, row or {})})
        except Exception as exc:
            self.bot.health.error(self.health_key, exc)
            self.bot.dispatch('bot_log', 'Ingame-Beitrittsstatistik fehlgeschlagen',
                              f'{config.server(server_id).title}: Nachricht für `{steam_id}` nicht bestätigt '
                              f'({exc.safe_message if isinstance(exc, RconError) else type(exc).__name__}). '
                              'Keine automatische Wiederholung.', discord.Color.blue())
        else:
            self.delivered_counts[server_id]['join'] += 1

    def _flush_delivery_log(self):
        if time.monotonic() - self.last_log_flush < 60:
            return
        self.last_log_flush = time.monotonic()
        for server_id, counts in list(self.delivered_counts.items()):
            if counts['quest'] or counts['join']:
                self.bot.dispatch('bot_log', 'Ingame-Nachrichten gesendet',
                                  f'{config.server(server_id).title}: {counts["join"]} Beitrittsstatistiken, '
                                  f'{counts["quest"]} Quest-Abschlüsse.', discord.Color.blue())
        self.delivered_counts.clear()

    async def _worker(self, server_id):
        queue = self.queue[server_id]
        while True:
            priority, sequence, kind, value = await queue.get()
            try:
                if kind == 'quest':
                    await self._send_quest(server_id, value)
                elif kind == 'milestone':
                    await self._attempt_milestone(server_id, value)
                else:
                    await self._send_join(server_id, value)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception('Ingame notice processing failed')
                self.bot.health.error(self.health_key, exc)
            finally:
                if kind == 'quest':
                    self.queued_quests.discard(int(value['id']))
                elif kind == 'milestone':
                    self.queued_milestones.discard(value)
                queue.task_done()
            await asyncio.sleep(1)

    @tasks.loop(seconds=5)
    async def monitor(self):
        try:
            await self._poll_quests()
            await self._poll_scores()
            self._flush_delivery_log()
            self.bot.health.ok(self.health_key)
        except Exception as exc:
            self.bot.health.error(self.health_key, exc)

    @monitor.before_loop
    async def ready(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    cog = IngameNotifications(bot)
    try:
        # setup_hook runs before the gateway is ready, so no live award can be
        # produced before this first-deployment high-water mark is recorded.
        await cog._initialize_ledger()
    except Exception as exc:
        bot.health.error(cog.health_key, exc)
    await bot.add_cog(cog)
