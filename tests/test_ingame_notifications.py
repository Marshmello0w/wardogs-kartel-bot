import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, patch
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

from cogs.ingame_notifications import (IngameNotifications, join_notice,
                                       quest_notice, reached_thresholds, real_faction)
from cogs.quest_tracker import QuestTracker, valid_team


STEAM_ID = '76561198000000001'


class NotificationTextTests(TestCase):
    def test_only_the_three_real_factions_are_eligible(self):
        for team in ('Lonestar', 'Valkyra', 'Manticore'):
            self.assertEqual(real_faction(team.lower()), team)
            self.assertEqual(valid_team(team), team)
        for team in ('White', 'Unknown', 'SomeOtherTeam', None):
            self.assertIsNone(real_faction(team))
            self.assertIsNone(valid_team(team))

    def test_milestone_uses_highest_real_faction_once(self):
        snapshot = {'factionScores': [
            {'name': 'Lonestar', 'score': 26}, {'name': 'Valkyra', 'score': 25},
            {'name': 'White', 'score': 90}]}
        self.assertEqual(reached_thresholds(snapshot), (25,))
        snapshot['factionScores'][1]['score'] = 76
        self.assertEqual(reached_thresholds(snapshot), (25, 50, 75))

    def test_messages_are_private_sized_and_name_is_clean(self):
        notice = quest_notice('Rundenquest: Erster Einsatz\nspam', 1)
        self.assertEqual(notice, 'Quest abgeschlossen: Erster Einsatzspam (+1 Punkt).')
        stats = join_notice('server1', {'lifetime_kills': 10, 'lifetime_deaths': 5,
                                        'lifetime_cash': 100000, 'playtime_seconds': 3660})
        self.assertIn('K/D 2.00', stats)
        self.assertIn('1 Std. 01 Min.', stats)
        self.assertLess(len(stats), 200)


class NotificationBehaviorTests(IsolatedAsyncioTestCase):
    def make_cog(self):
        cog = IngameNotifications.__new__(IngameNotifications)
        cog.bot = Mock()
        cog.bot.rcon.request = AsyncMock()
        cog.bot.get_cog.return_value = None
        cog.rosters = {}
        cog.queue = {}
        cog.workers = {}
        cog.sequence = 0
        cog.queued_quests = set()
        cog.queued_milestones = set()
        cog.initialized_scores = set()
        cog.ledger_floor = 0
        cog.delivered_counts = {'server1': {'quest': 0, 'join': 0}}
        cog.last_log_flush = time.monotonic()
        return cog

    async def test_first_sample_and_long_gap_are_baselines(self):
        cog = self.make_cog()
        cog._enqueue = Mock()
        state = {'round_id': 'round', 'uncertain': False}
        await cog.on_player_sampled('server1', state, [{'steamId': STEAM_ID}])
        cog._enqueue.assert_not_called()
        cog.rosters['server1'] = (time.monotonic() - 91, {})
        await cog.on_player_sampled('server1', state, [{'steamId': STEAM_ID}])
        cog._enqueue.assert_not_called()
        cog.rosters['server1'] = (time.monotonic(), {})
        await cog.on_player_sampled('server1', state, [{'steamId': STEAM_ID}])
        cog._enqueue.assert_called_once_with('server1', 2, 'join', STEAM_ID)

    async def test_quest_whisper_requires_real_faction(self):
        cog = self.make_cog()
        cog._record_delivery = AsyncMock()
        row = {'id': 4, 'steam_id': STEAM_ID, 'reason': 'Tagesquest: Tagesjäger', 'amount': 2}
        cog.rosters['server1'] = (time.monotonic(), {STEAM_ID: {'faction': 'White'}})
        await cog._send_quest('server1', row)
        cog.bot.rcon.request.assert_not_called()
        cog._record_delivery.assert_awaited_once_with(4, 'skipped')

        cog._record_delivery.reset_mock()
        cog.rosters['server1'] = (time.monotonic(), {STEAM_ID: {'faction': 'Lonestar'}})
        await cog._send_quest('server1', row)
        self.assertEqual(cog._record_delivery.await_args_list[0].args, (4, 'attempted', 'server1'))
        self.assertEqual(cog._record_delivery.await_args_list[1].args, (4, 'sent', 'server1'))
        cog.bot.rcon.request.assert_awaited_once_with(
            'server1', 'POST', f'/v1/players/{STEAM_ID}/message',
            payload={'message': 'Quest abgeschlossen: Tagesjäger (+2 Punkte).'})

    async def test_permanent_rewards_wait_for_fresh_real_team(self):
        class Cursor:
            def __init__(self):
                self.queries = []

            async def execute(self, sql, params=None):
                self.queries.append((sql, params))

            async def fetchall(self):
                if len(self.queries) == 1:
                    return [{'steam_id': STEAM_ID, 'legacy_playtime_seconds': 7200,
                             'lifetime_cash': 200000}]
                return []  # White / Unknown / stale players are absent.

        cur = Cursor()

        @asynccontextmanager
        async def fake_transaction():
            yield cur

        cog = QuestTracker.__new__(QuestTracker)
        cog._credit = AsyncMock()
        with patch('cogs.quest_tracker.database.transaction', fake_transaction):
            await cog.sync_permanent()
        cog._credit.assert_not_awaited()
        self.assertEqual(len(cur.queries), 2)
        self.assertIn('player_faction_state', cur.queries[1][0])
        self.assertEqual(cur.queries[1][1][1:], ('lonestar', 'valkyra', 'manticore'))

    async def test_permanent_cash_and_legacy_time_pay_after_team_detection(self):
        class Cursor:
            def __init__(self):
                self.queries = []

            async def execute(self, sql, params=None):
                self.queries.append((sql, params))

            async def fetchall(self):
                if len(self.queries) == 1:
                    return [{'steam_id': STEAM_ID, 'legacy_playtime_seconds': 7200,
                             'lifetime_cash': 200000}]
                return [{'steam_id': STEAM_ID}]

            async def fetchone(self):
                return {'legacy_playtime_imported': 0, 'awarded_cash_blocks': 0,
                        'eligible_playtime_seconds': 0, 'awarded_eligible_hours': 0}

        @asynccontextmanager
        async def fake_transaction():
            yield Cursor()

        cog = QuestTracker.__new__(QuestTracker)
        cog._credit = AsyncMock(return_value=True)
        with patch('cogs.quest_tracker.database.transaction', fake_transaction):
            await cog.sync_permanent()
        amounts = [(call.args[2], call.args[3]) for call in cog._credit.await_args_list]
        self.assertEqual(amounts, [(2, 'legacy_playtime'), (2, 'cash')])

    async def test_milestones_bootstrap_without_replay_then_queue_once(self):
        cog = self.make_cog()
        state = {'round_id': 'round-1', 'ended': False,
                 'snapshot': {'factionScores': [{'name': 'Lonestar', 'score': 24}]}}
        tracker = Mock()
        tracker.current.return_value = state
        cog.bot.get_cog.return_value = tracker
        cog._enqueue = Mock(return_value=True)
        saved = {}

        async def get_state(namespace, key):
            return saved.get(key)

        async def save_state(namespace, key, value):
            saved[key] = value

        with (patch('cogs.ingame_notifications.config.servers', return_value=[Mock(id='server1', enabled=True)]),
              patch('cogs.ingame_notifications.storage.get_state', get_state),
              patch('cogs.ingame_notifications.storage.save_state', save_state)):
            await cog._poll_scores()
            cog._enqueue.assert_not_called()
            state['snapshot']['factionScores'][0]['score'] = 25
            await cog._poll_scores()
            await cog._poll_scores()
            cog._enqueue.assert_called_once_with('server1', 1, 'milestone', ('server1', 'round-1', 25))
            # A fresh process must not replay thresholds already reached.
            restarted = self.make_cog()
            restarted.bot.get_cog.return_value = tracker
            restarted._enqueue = Mock(return_value=True)
            await restarted._poll_scores()
            restarted._enqueue.assert_not_called()
            self.assertEqual(saved['server1']['attempted'], [25])

    async def test_milestone_is_recorded_before_public_broadcast(self):
        cog = self.make_cog()
        state = {'round_id': 'round-1', 'ended': False,
                 'snapshot': {'factionScores': [{'name': 'Lonestar', 'score': 25}]}}
        cog.bot.get_cog.return_value = Mock(current=Mock(return_value=state))
        saved = {'round_id': 'round-1', 'attempted': []}

        class Cursor:
            async def execute(self, sql, params=None):
                pass

            async def fetchone(self):
                return {'payload': json.dumps(saved)}

        @asynccontextmanager
        async def fake_transaction():
            yield Cursor()

        async def put_state(cur, namespace, key, value):
            saved.update(value)

        async def request(server_id, method, path, *, payload):
            self.assertEqual(saved['attempted'], [25])
            self.assertEqual(path, '/v1/broadcast')
            self.assertIn('dsc.gg/dkwd', payload['message'])

        cog.bot.rcon.request.side_effect = request
        with (patch('cogs.ingame_notifications.database.transaction', fake_transaction),
              patch('cogs.ingame_notifications.storage.put_state', put_state)):
            await cog._attempt_milestone('server1', ('server1', 'round-1', 25))
            await cog._attempt_milestone('server1', ('server1', 'round-1', 25))
        self.assertEqual(cog.bot.rcon.request.await_count, 1)
        cog.bot.dispatch.assert_called_once()
