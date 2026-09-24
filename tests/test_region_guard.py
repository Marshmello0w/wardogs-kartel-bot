import asyncio
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

os.environ['PYTHON_DOTENV_DISABLED'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

from cogs.region_guard import (RegionGuardCog, berlin_day, in_restart_window, restart_allowed,
                               initial_state, response_is_new_enough)


UTC = timezone.utc


def snapshot(players):
    return {'snapshot': {'players': {'current': players}}}


class FakeTracker:
    def __init__(self, value):
        self.value = value

    def current(self, _server_id):
        return self.value


class FakeBot:
    def __init__(self, players=0):
        self.tracker = FakeTracker(snapshot(players))
        self.logs = []
        self.health = SimpleNamespace(ok=lambda *_: None, error=lambda *_: None)

    def get_cog(self, name):
        return self.tracker if name == 'RoundTracker' else None

    def dispatch(self, event, *args):
        self.logs.append((event, args))


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status, self.payload = status, payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self, **_kwargs):
        return self.payload


class RegionGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.server = SimpleNamespace(id='server1', title='Server 1', uuid='join-code', enabled=True)
        # 04:00 in Berlin during CEST.
        self.now = datetime(2026, 9, 22, 2, 0, tzinfo=UTC)
        self.memory = {}

    def cog(self, players=0):
        cog = RegionGuardCog.__new__(RegionGuardCog)
        cog.bot = FakeBot(players)
        cog.locks = defaultdict(asyncio.Lock)
        cog.states = {}
        cog.panel_ids = {}
        cog.panel_checked_at = None
        cog.ptero_session = None
        cog.public_session = None
        return cog

    async def _get(self, _namespace, key):
        return self.memory.get(key)

    async def _save(self, _namespace, key, value):
        self.memory[key] = dict(value)

    def api(self, updated_at, region='eu-central', instance_id='new-instance'):
        return {'updated_at': updated_at, 'region': region, 'instance_id': instance_id}

    async def test_window_uses_berlin_time_and_excludes_ten(self):
        self.assertTrue(in_restart_window(self.now))
        self.assertFalse(in_restart_window(datetime(2026, 9, 22, 8, 0, tzinfo=UTC)))
        self.assertEqual(berlin_day(self.now), '2026-09-22')

    async def test_empty_fresh_rcon_starts_only_after_five_minutes(self):
        cog = self.cog(0)
        restarts = []

        async def restart(_srv, state, now):
            restarts.append(now)
            state.update(phase='awaiting_api', restart_at=now.isoformat())
            return True

        cog._restart = restart
        cog._server_api = AsyncMock(return_value=(self.api(self.now, instance_id='old-instance'), None))
        with patch('cogs.region_guard.storage.get_state', self._get), \
             patch('cogs.region_guard.storage.save_state', self._save):
            await cog.tick_server(self.server, self.now)
            await cog.tick_server(self.server, self.now + timedelta(seconds=299))
            await cog.tick_server(self.server, self.now + timedelta(seconds=300))
        self.assertEqual(restarts, [self.now + timedelta(seconds=300)])
        self.assertEqual(self.memory['server1']['phase'], 'awaiting_api')

    async def test_real_restart_persists_the_previous_instance_id(self):
        cog = self.cog(0)
        state = initial_state(berlin_day(self.now))
        state['last_instance_id'] = 'before-restart'
        session = SimpleNamespace(post=Mock(return_value=FakeResponse(204)))
        cog._ptero_session = AsyncMock(return_value=session)
        cog._discover_panel_servers = AsyncMock(return_value={'server1': 'panel-identifier'})
        self.assertTrue(await cog._restart(self.server, state, self.now))
        self.assertEqual(state['previous_instance_id'], 'before-restart')
        self.assertEqual(state['restart_at'], self.now.isoformat())
        self.assertEqual(state['phase'], 'awaiting_api')
        self.assertEqual(session.post.call_args.kwargs['json'], {'signal': 'restart'})

    async def test_server_api_reads_instance_id_and_rejects_missing_id(self):
        cog = self.cog(0)
        state = initial_state(berlin_day(self.now))
        payload = {'updatedAt': self.now.isoformat(),
                   'server': {'id': 'session-42', 'region': 'eu-central', 'linkKey': 'stable-key'}}
        session = SimpleNamespace(get=Mock(return_value=FakeResponse(200, payload)))
        cog._public_session = AsyncMock(return_value=session)
        result, problem = await cog._server_api(self.server, state)
        self.assertIsNone(problem)
        self.assertEqual(result['instance_id'], 'session-42')
        self.assertEqual(state['link_key'], 'stable-key')
        session.get.return_value = FakeResponse(200, {'updatedAt': self.now.isoformat(),
                                                     'server': {'region': 'eu-central'}})
        result, problem = await cog._server_api(self.server, state)
        self.assertIsNone(result)
        self.assertIn('Spielinstanz-ID', problem)

    async def test_player_join_keeps_api_proof_and_never_triggers_a_second_restart(self):
        cog = self.cog(3)
        state = initial_state(berlin_day(self.now))
        state.update(phase='awaiting_api', restart_at=self.now.isoformat(),
                     previous_instance_id='old-instance')
        self.memory['server1'] = state
        cog._server_api = AsyncMock(side_effect=[
            (self.api(self.now + timedelta(minutes=4)), None),
            (self.api(self.now + timedelta(minutes=8)), None)])
        cog._restart = AsyncMock(return_value=True)
        with patch('cogs.region_guard.storage.get_state', self._get), \
             patch('cogs.region_guard.storage.save_state', self._save):
            await cog.tick_server(self.server, self.now + timedelta(minutes=1))
            self.assertEqual(self.memory['server1']['phase'], 'awaiting_api')
            self.assertEqual(self.memory['server1']['restart_at'], self.now.isoformat())
            await cog.tick_server(self.server, self.now + timedelta(minutes=4))
            await cog.tick_server(self.server, self.now + timedelta(minutes=8))
        self.assertTrue(self.memory['server1']['completed'])
        cog._restart.assert_not_awaited()

    async def test_early_release_still_needs_five_minutes_empty(self):
        early = datetime(2026, 9, 22, 0, 0, tzinfo=UTC)  # 02:00 Europe/Berlin
        cog = self.cog(0)
        state = initial_state(berlin_day(early))
        state['early_enabled'] = True
        self.memory['server1'] = state
        cog._restart = AsyncMock(return_value=True)
        cog._server_api = AsyncMock(return_value=(self.api(early, instance_id='old-instance'), None))
        with patch('cogs.region_guard.storage.get_state', self._get), \
             patch('cogs.region_guard.storage.save_state', self._save):
            await cog.tick_server(self.server, early)
            await cog.tick_server(self.server, early + timedelta(minutes=5))
        cog._restart.assert_awaited_once()
        self.assertTrue(restart_allowed(self.memory['server1'], early + timedelta(minutes=5)))

    async def test_old_api_timestamp_is_never_processed(self):
        cog = self.cog(0)
        restart_at = self.now
        state = initial_state(berlin_day(restart_at))
        state.update(phase='awaiting_api', restart_at=restart_at.isoformat(),
                     previous_instance_id='old-instance')
        self.memory['server1'] = state
        cog._server_api = AsyncMock(return_value=(
            self.api(restart_at + timedelta(minutes=3, seconds=59), 'eu-west'), None))
        cog._restart = AsyncMock(return_value=True)
        with patch('cogs.region_guard.storage.get_state', self._get), \
             patch('cogs.region_guard.storage.save_state', self._save):
            await cog.tick_server(self.server, restart_at + timedelta(minutes=4))
        cog._restart.assert_not_awaited()
        self.assertIn('Zeitstempel', self.memory['server1']['last_error'])

    async def test_stale_api_timestamp_is_rechecked_after_thirty_seconds(self):
        cog = self.cog(0)
        restart_at = self.now
        state = initial_state(berlin_day(restart_at))
        state.update(phase='awaiting_api', restart_at=restart_at.isoformat(),
                     previous_instance_id='old-instance')
        self.memory['server1'] = state
        cog._server_api = AsyncMock(return_value=(
            self.api(restart_at + timedelta(minutes=3), 'eu-west'), None))
        with patch('cogs.region_guard.storage.get_state', self._get), \
             patch('cogs.region_guard.storage.save_state', self._save):
            await cog.tick_server(self.server, restart_at + timedelta(minutes=4))
            await cog.tick_server(self.server, restart_at + timedelta(minutes=4, seconds=29))
            await cog.tick_server(self.server, restart_at + timedelta(minutes=4, seconds=30))
        self.assertEqual(cog._server_api.await_count, 2)

    async def test_fresh_target_region_completes_day(self):
        cog = self.cog(0)
        restart_at = self.now
        state = initial_state(berlin_day(restart_at))
        state.update(phase='awaiting_api', restart_at=restart_at.isoformat(),
                     previous_instance_id='old-instance')
        self.memory['server1'] = state
        cog._server_api = AsyncMock(side_effect=[
            (self.api(restart_at + timedelta(minutes=4)), None),
            (self.api(restart_at + timedelta(minutes=8)), None)])
        with patch('cogs.region_guard.storage.get_state', self._get), \
             patch('cogs.region_guard.storage.save_state', self._save):
            await cog.tick_server(self.server, restart_at + timedelta(minutes=4))
            self.assertFalse(self.memory['server1']['completed'])
            await cog.tick_server(self.server, restart_at + timedelta(minutes=8))
        self.assertTrue(self.memory['server1']['completed'])
        self.assertEqual(self.memory['server1']['phase'], 'complete')
        self.assertEqual(self.memory['server1']['verified_instance_id'], 'new-instance')

    async def test_old_instance_cannot_complete_even_with_fresh_central_region(self):
        cog = self.cog(0)
        state = initial_state(berlin_day(self.now))
        state.update(phase='awaiting_api', restart_at=self.now.isoformat(),
                     previous_instance_id='old-instance')
        self.memory['server1'] = state
        cog._server_api = AsyncMock(return_value=(
            self.api(self.now + timedelta(minutes=4), instance_id='old-instance'), None))
        cog._restart = AsyncMock(return_value=True)
        with patch('cogs.region_guard.storage.get_state', self._get), \
             patch('cogs.region_guard.storage.save_state', self._save):
            await cog.tick_server(self.server, self.now + timedelta(minutes=4))
        self.assertFalse(self.memory['server1']['completed'])
        self.assertIsNone(self.memory['server1']['pending_confirmation'])
        self.assertIsNone(self.memory['server1']['last_region'])
        self.assertEqual(self.memory['server1']['restart_at'], self.now.isoformat())
        cog._restart.assert_not_awaited()

    async def test_region_change_resets_confirmation(self):
        cog = self.cog(0)
        state = initial_state(berlin_day(self.now))
        state.update(phase='awaiting_api', restart_at=self.now.isoformat(),
                     previous_instance_id='old-instance')
        self.memory['server1'] = state
        cog._server_api = AsyncMock(side_effect=[
            (self.api(self.now + timedelta(minutes=4), 'eu-central'), None),
            (self.api(self.now + timedelta(minutes=8), 'eu-west'), None),
            (self.api(self.now + timedelta(minutes=12), 'eu-west'), None)])
        with patch('cogs.region_guard.storage.get_state', self._get), \
             patch('cogs.region_guard.storage.save_state', self._save):
            for minute in (4, 8, 12):
                await cog.tick_server(self.server, self.now + timedelta(minutes=minute))
        self.assertFalse(self.memory['server1']['completed'])
        self.assertEqual(self.memory['server1']['phase'], 'waiting_empty')
        self.assertEqual(self.memory['server1']['last_region'], 'eu-west')

    async def test_success_is_reopened_only_after_two_west_observations(self):
        cog = self.cog(0)
        state = initial_state(berlin_day(self.now))
        state.update(completed=True, phase='complete', verified_instance_id='new-instance',
                     last_instance_id='new-instance', last_region='eu-central',
                     last_query_at=self.now.isoformat(), last_api_updated_at=self.now.isoformat())
        self.memory['server1'] = state
        cog._server_api = AsyncMock(side_effect=[
            (self.api(self.now + timedelta(minutes=5), 'eu-west'), None),
            (self.api(self.now + timedelta(minutes=10), 'eu-west'), None)])
        cog._restart = AsyncMock(return_value=True)
        with patch('cogs.region_guard.storage.get_state', self._get), \
             patch('cogs.region_guard.storage.save_state', self._save):
            await cog.tick_server(self.server, self.now + timedelta(minutes=5))
            self.assertTrue(self.memory['server1']['completed'])
            await cog.tick_server(self.server, self.now + timedelta(minutes=10))
        self.assertFalse(self.memory['server1']['completed'])
        self.assertEqual(self.memory['server1']['phase'], 'waiting_empty')
        cog._restart.assert_not_awaited()

    async def test_external_reboot_staying_central_updates_verified_identity(self):
        cog = self.cog(0)
        state = initial_state(berlin_day(self.now))
        state.update(completed=True, phase='complete', verified_instance_id='old-instance',
                     last_instance_id='old-instance', last_region='eu-central',
                     last_query_at=self.now.isoformat(), last_api_updated_at=self.now.isoformat())
        self.memory['server1'] = state
        cog._server_api = AsyncMock(side_effect=[
            (self.api(self.now + timedelta(minutes=5), instance_id='new-instance'), None),
            (self.api(self.now + timedelta(minutes=10), instance_id='new-instance'), None)])
        cog._restart = AsyncMock()
        with patch('cogs.region_guard.storage.get_state', self._get), \
             patch('cogs.region_guard.storage.save_state', self._save):
            await cog.tick_server(self.server, self.now + timedelta(minutes=5))
            await cog.tick_server(self.server, self.now + timedelta(minutes=10))
        self.assertTrue(self.memory['server1']['completed'])
        self.assertEqual(self.memory['server1']['verified_instance_id'], 'new-instance')
        cog._restart.assert_not_awaited()

    async def test_confirmed_state_survives_bot_restart_same_berlin_day(self):
        state = initial_state(berlin_day(self.now))
        state.update(completed=True, phase='complete', verified_instance_id='confirmed-instance')
        self.memory['server1'] = state
        restarted_cog = self.cog(0)
        with patch('cogs.region_guard.storage.get_state', self._get):
            loaded = await restarted_cog.load('server1', self.now + timedelta(minutes=2))
        self.assertTrue(loaded['completed'])
        self.assertEqual(loaded['verified_instance_id'], 'confirmed-instance')

    async def test_missing_pre_restart_identity_fails_closed_after_bot_restart(self):
        cog = self.cog(0)
        state = initial_state(berlin_day(self.now))
        state.update(phase='awaiting_api', restart_at=self.now.isoformat())
        self.memory['server1'] = state
        cog._server_api = AsyncMock()
        cog._restart = AsyncMock()
        with patch('cogs.region_guard.storage.get_state', self._get), \
             patch('cogs.region_guard.storage.save_state', self._save):
            await cog.tick_server(self.server, self.now + timedelta(minutes=4))
        cog._server_api.assert_not_awaited()
        cog._restart.assert_not_awaited()
        self.assertIn('Spielinstanz-ID fehlt', self.memory['server1']['last_error'])

    async def test_restart_continues_api_confirmation_while_rcon_restarts(self):
        cog = self.cog(0)
        restart_at = self.now
        state = initial_state(berlin_day(restart_at))
        state.update(phase='awaiting_api', restart_at=restart_at.isoformat(),
                     previous_instance_id='old-instance')
        self.memory['server1'] = state
        cog.bot.tracker.value = None  # Expected while the server is rebooting.
        cog._server_api = AsyncMock(side_effect=[
            (self.api(restart_at + timedelta(minutes=4)), None),
            (self.api(restart_at + timedelta(minutes=8)), None)])
        with patch('cogs.region_guard.storage.get_state', self._get), \
             patch('cogs.region_guard.storage.save_state', self._save):
            await cog.tick_server(self.server, restart_at + timedelta(minutes=4))
            await cog.tick_server(self.server, restart_at + timedelta(minutes=8))
        self.assertTrue(self.memory['server1']['completed'])

    async def test_fresh_wrong_region_waits_for_new_five_minute_empty_timer(self):
        cog = self.cog(0)
        restart_at = self.now
        state = initial_state(berlin_day(restart_at))
        state.update(phase='awaiting_api', restart_at=restart_at.isoformat(),
                     previous_instance_id='old-instance')
        self.memory['server1'] = state
        cog._server_api = AsyncMock(side_effect=[
            (self.api(restart_at + timedelta(minutes=4), 'eu-west'), None),
            (self.api(restart_at + timedelta(minutes=8), 'eu-west'), None),
            (self.api(restart_at + timedelta(minutes=13), 'eu-west'), None)])
        cog._restart = AsyncMock(return_value=True)
        with patch('cogs.region_guard.storage.get_state', self._get), \
             patch('cogs.region_guard.storage.save_state', self._save):
            await cog.tick_server(self.server, restart_at + timedelta(minutes=4))
            await cog.tick_server(self.server, restart_at + timedelta(minutes=8))
            cog._restart.assert_not_awaited()
            await cog.tick_server(self.server, restart_at + timedelta(minutes=8, seconds=15))
            await cog.tick_server(self.server, restart_at + timedelta(minutes=13, seconds=15))
        cog._restart.assert_awaited_once()

    async def test_pause_persists_when_a_new_day_starts(self):
        cog = self.cog(0)
        state = initial_state('2026-09-21')
        state.update(paused=True, completed=True, link_key='stable-key')
        self.memory['server1'] = state
        with patch('cogs.region_guard.storage.get_state', self._get), \
             patch('cogs.region_guard.storage.save_state', self._save):
            loaded = await cog.load('server1', self.now)
        self.assertTrue(loaded['paused'])
        self.assertFalse(loaded['completed'])
        self.assertEqual(loaded['link_key'], 'stable-key')

    async def test_panel_recovery_reenables_previously_disabled_server(self):
        cog = self.cog(0)
        state = initial_state(berlin_day(self.now))
        state.update(panel_disabled=True, phase='disabled', last_error='Pterodactyl HTTP 403')
        self.memory['server1'] = state
        cog._discover_panel_servers = AsyncMock(return_value={'server1': 'panel-id'})
        with patch('cogs.region_guard.storage.get_state', self._get), \
             patch('cogs.region_guard.storage.save_state', self._save), \
             patch('cogs.region_guard.config.servers', return_value=[self.server]):
            await cog.initialise_panel()
        self.assertFalse(self.memory['server1']['panel_disabled'])
        self.assertEqual(self.memory['server1']['phase'], 'idle')
        self.assertIsNone(self.memory['server1']['last_error'])

    def test_timestamp_requires_full_four_minutes(self):
        self.assertFalse(response_is_new_enough(self.now + timedelta(seconds=239), self.now))
        self.assertTrue(response_is_new_enough(self.now + timedelta(seconds=240), self.now))


if __name__ == '__main__':
    unittest.main()
