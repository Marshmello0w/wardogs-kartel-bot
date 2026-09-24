"""No live game/API calls: synthetic feed contract and conservative decisions."""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

os.environ['PYTHON_DOTENV_DISABLED'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

from cogs.challenge_quests import advance_round
from cogs.combat_feed import (CombatFeed, ROUND_GOALS, DAILY_GOALS,
                              award_round, record_combat_quests, record_feed_streak)
from domain.combat_feed import normalized_kill, relation, roster_for_event
from web.app import create_app
from web.feed import MAX_BODY_BYTES, forward_once, token_server, validate_batch
from web.settings import Settings

STEAM_1 = '76561199711897930'
STEAM_2 = '76561198000000001'
NOW = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)


def batch(events=None, instance='boot-1'):
    return {'serverId': instance, 'serverName': 'Example', 'events': events or [
        {'eventId': 'event-1', 'type': 'killed', 'eventTime': 62.0,
         'killerSteamId': STEAM_1, 'victimSteamId': STEAM_2,
         'distance': 15_000, 'cause': 'Id.Item.Rifle',
         'contextTags': ['Meta.Progression.Context.Player.KillContext.Headshot']}]}


def sample(at=NOW, *, team2='Manticore', score=1, players=12):
    return ({'round_id': 'round-1', 'observed_at': at.isoformat(), 'uncertain': False,
             'ended': False, 'snapshot': {'highest': score, 'map': 'Kavkazi',
                                          'players': {'current': players}}},
            [{'steamId': STEAM_1, 'faction': 'Valkyra'},
             {'steamId': STEAM_2, 'faction': team2}])


class ContractTests(unittest.TestCase):
    def test_token_identity_is_server_specific_not_instance_specific(self):
        tokens = ('one', 'two', 'three')
        for index, token in enumerate(tokens, 1):
            self.assertEqual(token_server('Bearer ' + token, tokens), f'server{index}')
        self.assertIsNone(token_server('Bearer wrong', tokens))
        self.assertIsNone(token_server('Basic one', tokens))
        self.assertIsNone(token_server('', tokens))
        self.assertIsNone(token_server('Bearer ü', tokens))
        with self.assertRaises(ValueError):
            Settings(session_secret='test-secret-of-at-least-thirty-two-characters',
                     feed_enabled=True, feed_tokens=('same', 'same', 'different'))

    def test_invalid_and_oversized_batches(self):
        for raw in (b'', b'not json', b'{}', b'{"events": []}',
                    json.dumps(batch([{'eventId': 'x'}, {'eventId': 'x'}])).encode(),
                    json.dumps(batch([{'eventId': 'x'}] * 201)).encode(), b'x' * (MAX_BODY_BYTES + 1)):
            with self.assertRaises(ValueError):
                validate_batch(raw)
        self.assertEqual(len(validate_batch(json.dumps(batch()).encode())['events']), 1)

    def test_kill_normalization_headshot_distance_and_restart(self):
        first = normalized_kill(batch()['events'][0], 'old-boot', NOW)
        second = normalized_kill(batch(instance='new-boot')['events'][0], 'new-boot', NOW)
        self.assertEqual(first['distance_m'], 150)
        self.assertTrue(first['headshot'])
        self.assertNotEqual(first['instance_id'], second['instance_id'])
        self.assertIsNone(normalized_kill({'eventId': 'x', 'type': 'joined'}, 'boot', NOW))
        self.assertIsNone(normalized_kill({'eventId': 'x', 'type': 'killed', 'distance': -1},
                                          'boot', NOW)['distance_m'])

    def test_factions_and_stale_or_not_started_round(self):
        event = normalized_kill(batch()['events'][0], 'boot', NOW)
        self.assertEqual(relation(event, roster_for_event(sample(), NOW)), 'enemy')
        self.assertEqual(relation(event, roster_for_event(sample(team2='Valkyra'), NOW)), 'teamkill')
        self.assertIsNone(relation(event, roster_for_event(sample(team2='White'), NOW)))
        self.assertIsNone(roster_for_event(sample(at=NOW-timedelta(seconds=21)), NOW))
        self.assertIsNone(roster_for_event(sample(score=0, players=20), NOW))
        self.assertIsNotNone(roster_for_event(sample(score=0, players=21), NOW))
        self.assertIsNone(roster_for_event(sample(), NOW, 'Ozeti'))

    def test_feed_streak_disables_rcon_kill_guessing(self):
        previous = {'last_seen': NOW.replace(tzinfo=None), 'last_kills': 0,
                    'last_deaths': 0, 'last_cash': 0, 'last_faction': 'Valkyra',
                    'kills': 0, 'deaths': 0, 'cash': 0, 'active_seconds': 0,
                    'streak': 2, 'best_streak': 2, 'active': 1}
        updated, delta, _, _ = advance_round(previous,
            {'kills': 3, 'deaths': 0, 'cash': 0}, 'Valkyra', NOW+timedelta(seconds=15),
            feed_streak=True)
        self.assertEqual(delta['kills'], 3)
        self.assertEqual((updated['streak'], updated['best_streak']), (2, 2))
        updated, _, _, _ = advance_round(previous,
            {'kills': 3, 'deaths': 1, 'cash': 0}, 'Valkyra', NOW+timedelta(seconds=15),
            feed_streak=True)
        self.assertEqual(updated['streak'], 0)

    def test_four_new_thresholds(self):
        self.assertEqual([(field, target) for field, target, _ in ROUND_GOALS],
                         [('headshots', 3), ('long_kills', 1)])
        self.assertEqual([(field, target) for field, target, _ in DAILY_GOALS],
                         [('headshots', 10), ('long_kills', 3)])


class QuestWriteTests(unittest.IsolatedAsyncioTestCase):
    class Cursor:
        def __init__(self, rows):
            self.rows = iter(rows)
            self.statements = []
            self.rowcount = 1
        async def execute(self, sql, args=()):
            self.statements.append((sql, args))
        async def fetchone(self):
            return next(self.rows)
        async def fetchall(self):
            return next(self.rows)

    async def test_round_cap_is_locked_and_never_overpaid(self):
        cur = self.Cursor([{'round_points_awarded': 15}])
        with patch('cogs.combat_feed.credit', new_callable=AsyncMock) as pay:
            self.assertFalse(await award_round(cur, NOW.date(), 'server1', 'round-1',
                                                STEAM_1, 'feed_headshots', 'Headshots'))
            pay.assert_not_awaited()
        self.assertIn('FOR UPDATE', cur.statements[1][0])
        cur = self.Cursor([{'round_points_awarded': 14}])
        with patch('cogs.combat_feed.credit', new_callable=AsyncMock, return_value=True) as pay:
            self.assertTrue(await award_round(cur, NOW.date(), 'server1', 'round-1',
                                               STEAM_1, 'feed_headshots', 'Headshots'))
            pay.assert_awaited_once()
        self.assertIn('round_points_awarded=round_points_awarded+1', cur.statements[-1][0])

    async def test_both_round_and_daily_targets_can_complete_on_same_kill(self):
        event = normalized_kill(batch()['events'][0], 'boot', NOW)
        cur = self.Cursor([{'headshots': 3, 'long_kills': 1,
                            'completed_mask': 0, 'awarded_mask': 0},
                           {'headshots': 10, 'long_kills': 3,
                            'completed_mask': 0, 'awarded_mask': 0}])
        with patch('cogs.combat_feed.award_round', new_callable=AsyncMock,
                   return_value=True) as round_pay, \
             patch('cogs.combat_feed.credit', new_callable=AsyncMock,
                   return_value=True) as daily_pay:
            await record_combat_quests(cur, 'server1', 'round-1', event)
            self.assertEqual(round_pay.await_count, 2)
            self.assertEqual(daily_pay.await_count, 2)
        masks = [args[:2] for sql, args in cur.statements if 'SET completed_mask=' in sql]
        self.assertEqual(masks, [(3, 3), (3, 3)])

    async def test_ordered_kill_streak_and_victim_reset(self):
        event = normalized_kill(batch()['events'][0], 'boot', NOW)
        cur = self.Cursor([[{'steam_id': STEAM_1, 'streak': 4, 'best_streak': 4,
                             'completed_mask': 0, 'awarded_mask': 0},
                            {'steam_id': STEAM_2, 'streak': 2, 'best_streak': 2,
                             'completed_mask': 0, 'awarded_mask': 0}]])
        with patch('cogs.combat_feed.award_round', new_callable=AsyncMock,
                   return_value=True) as pay:
            await record_feed_streak(cur, 'server1', 'round-1', event)
            pay.assert_awaited_once()
        self.assertIn('FOR UPDATE', cur.statements[0][0])
        self.assertIn('SET streak=0', cur.statements[1][0])
        self.assertEqual(cur.statements[-1][1][:4], (5, 5, 2, 2))


class ModerationTests(unittest.IsolatedAsyncioTestCase):
    class Cursor:
        def __init__(self, rows=(), rowcount=1):
            self.rows = iter(rows)
            self.rowcount = rowcount
            self.statements = []
        async def execute(self, sql, args=()):
            self.statements.append((sql, args))
        async def fetchone(self):
            return next(self.rows)

    async def test_duplicate_event_does_not_update_stats_or_alerts(self):
        bot = type('Bot', (), {})()
        cog = CombatFeed(bot)
        cog.rosters['server1'] = sample()
        cur = self.Cursor(rowcount=0)
        with patch('cogs.combat_feed.record_combat_quests', new_callable=AsyncMock) as quests:
            result = await cog._event(cur, 'server1', 'boot', batch()['events'][0], NOW)
            self.assertIsNone(result)
            quests.assert_not_awaited()
        self.assertEqual(len(cur.statements), 1)

    async def test_rapid_window_and_cooldown_excludes_suicide(self):
        bot = type('Bot', (), {})()
        cog = CombatFeed(bot)
        cog.rosters['server1'] = sample()
        cur = self.Cursor([{'rapid_last_at': None}, {'count': 10}])
        with patch('cogs.combat_feed.record_combat_quests', new_callable=AsyncMock), \
             patch('cogs.combat_feed.record_feed_streak', new_callable=AsyncMock):
            result = await cog._event(cur, 'server1', 'boot', batch()['events'][0], NOW)
        self.assertEqual(result, ('rapid', 'server1', STEAM_1, 10))
        window = next(args for sql, args in cur.statements if 'SELECT COUNT(*) AS count' in sql)
        self.assertEqual(window[-2:], ((NOW-timedelta(seconds=60)).replace(tzinfo=None),
                                       NOW.replace(tzinfo=None)))
        cur = self.Cursor([{'rapid_last_at': NOW.replace(tzinfo=None)-timedelta(minutes=1)}])
        with patch('cogs.combat_feed.record_combat_quests', new_callable=AsyncMock), \
             patch('cogs.combat_feed.record_feed_streak', new_callable=AsyncMock):
            self.assertIsNone(await cog._event(cur, 'server1', 'boot', batch()['events'][0], NOW))
        self.assertFalse(any('SELECT COUNT(*) AS count' in sql for sql, _ in cur.statements))
        cog.rosters.clear()
        cur = self.Cursor([{'rapid_last_at': None}, {'count': 10}])
        self.assertEqual((await cog._event(cur, 'server1', 'boot', batch()['events'][0], NOW))[0], 'rapid')
        suicide = dict(batch()['events'][0], eventId='suicide', victimSteamId=STEAM_1)
        cur = self.Cursor()
        await cog._event(cur, 'server1', 'boot', suicide, NOW)
        self.assertFalse(any('SELECT COUNT(*) AS count' in sql for sql, _ in cur.statements))

    async def test_teamkill_is_suspected_and_does_not_earn_quest(self):
        bot = type('Bot', (), {})()
        cog = CombatFeed(bot)
        cog.rosters['server1'] = sample(team2='Valkyra')
        cur = self.Cursor([{'rapid_last_at': None}, {'count': 1}])
        with patch('cogs.combat_feed.record_combat_quests', new_callable=AsyncMock) as quests:
            await cog._event(cur, 'server1', 'boot', batch()['events'][0], NOW)
            quests.assert_not_awaited()
        self.assertTrue(any('teamkill_count' in sql for sql, _ in cur.statements))


class ForwardingTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_is_retried_and_success_sends_identical_payload(self):
        raw = json.dumps(batch()).encode()
        class Queue:
            def __init__(self):
                self.rows = [{'id': 'a', 'server_id': 'server2', 'payload': raw,
                              'forward_attempts': 0, 'received_at': datetime.now(timezone.utc)}]
                self.failed = AsyncMock()
                self.forwarded = AsyncMock()
            async def pending(self, limit=20):
                return self.rows
        queue = Queue()
        sent = []
        def handler(request):
            sent.append((request.headers['authorization'], request.content))
            return httpx.Response(503 if len(sent) == 1 else 204)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await forward_once(queue, ('one', 'two', 'three'), client)
            queue.failed.assert_awaited_once()
            queue.forwarded.assert_not_awaited()
            await forward_once(queue, ('one', 'two', 'three'), client)
            queue.forwarded.assert_awaited_once_with('a')
        self.assertEqual(sent, [('Bearer two', raw), ('Bearer two', raw)])


class IngressTests(unittest.TestCase):
    def test_disabled_and_authenticated_ingress(self):
        class Queue:
            def __init__(self):
                self.received = []
            async def receive(self, server_id, raw):
                self.received.append((server_id, raw))
            async def pending(self, limit=20):
                return []
        class Repo:
            pass
        queue = Queue()
        tokens = ('one', 'two', 'three')
        raw = json.dumps(batch(instance='changed-after-restart')).encode()
        settings = Settings(session_secret='test-secret-of-at-least-thirty-two-characters',
                            feed_db_url='mysql://test:test@localhost/test',
                            feed_tokens=tokens, feed_enabled=True)
        app = create_app(settings, repository=Repo(), feed_database=queue)
        with TestClient(app, base_url=settings.base_url) as client:
            self.assertEqual(client.post('/api/ingest/events', content=raw).status_code, 401)
            self.assertEqual(client.post('/api/ingest/events', content=raw,
                headers={'Authorization': 'Bearer two'}).status_code, 202)
            self.assertEqual(client.post('/api/ingest/events', content=b'{}',
                headers={'Authorization': 'Bearer two'}).status_code, 400)
            self.assertEqual(client.post('/api/ingest/events', content=b'x' * (MAX_BODY_BYTES+1),
                headers={'Authorization': 'Bearer two'}).status_code, 413)
        self.assertEqual(queue.received, [('server2', raw)])
        settings_off = Settings(session_secret='test-secret-of-at-least-thirty-two-characters')
        off = create_app(settings_off, repository=Repo(), feed_database=queue)
        with TestClient(off, base_url=settings_off.base_url) as client:
            self.assertEqual(client.post('/api/ingest/events', content=raw).status_code, 503)


if __name__ == '__main__':
    unittest.main()
